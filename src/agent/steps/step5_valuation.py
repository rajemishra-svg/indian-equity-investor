"""Step 5 — Valuation & Margin of Safety (deterministic + Python DCF).

DCF is computed entirely in Python — no LLM call.  This makes the valuation
auditable, deterministic, and free (zero API cost for this step).  The only
LLM involvement in this file is removed; the DCF formula itself is standard
finance: two-stage Gordon Growth with linear tapering in stage 2.

MoS definition (Graham): MoS = (Intrinsic − CMP) / Intrinsic × 100
At required_mos_pct=25%: CMP must be ≤ 75% of intrinsic value to pass.
"""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, GateResult, ValuationResult, WatchlistTier
from src.sector.profiles import get_sector_profile

# ---------------------------------------------------------------------------
# Verdict bands
# ---------------------------------------------------------------------------

_PE_PERCENTILE_BANDS = [
    (30, "EXCELLENT"),
    (60, "FAIR"),
    (80, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

_PEG_BANDS = [
    (1.0, "EXCELLENT"),
    (1.3, "FAIR"),
    (1.7, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

# EV/EBITDA bands — lower is cheaper.
_EV_EBITDA_BANDS = [
    (12, "EXCELLENT"),
    (20, "FAIR"),
    (28, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

# P/S bands for pre-profit companies (Damodaran emerging-market norms)
_PS_BANDS = [
    (2.0,  "EXCELLENT"),
    (5.0,  "FAIR"),
    (10.0, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

BUY_ZONE_VERDICTS = {"EXCELLENT", "FAIR", "ATTRACTIVE"}

# Conservative EBITDA → FCF conversion used when FCF must be estimated:
# −20% taxes, −10% maintenance capex, −5% working capital, +10% D&A add-back.
_EBITDA_TO_FCF_CONVERSION = 0.55


def _band_verdict(value: float | None, bands: list) -> str:
    if value is None:
        return "UNKNOWN"
    for threshold, label in bands:
        if value < threshold:
            return label
    return "AVOID"


def _fcf_yield_verdict(fcf_yield: float | None) -> str:
    if fcf_yield is None:
        return "UNKNOWN"
    if fcf_yield < 3.0:
        return "EXPENSIVE"
    elif fcf_yield < 5.0:
        return "FAIR"
    return "ATTRACTIVE"


def _compute_mos(cmp: float, intrinsic: float | None) -> float | None:
    """Graham MoS = (Intrinsic − CMP) / Intrinsic × 100."""
    if intrinsic is None or intrinsic <= 0:
        return None
    return round((intrinsic - cmp) / intrinsic * 100, 2)


# ---------------------------------------------------------------------------
# Deterministic DCF engine  (Fix 2.1 — replaces Claude DCF call)
# ---------------------------------------------------------------------------

def _dcf_single_scenario(
    base_fcf_cr: float,
    growth_rate_pct: float,
    wacc_pct: float,
    terminal_growth_pct: float,
    net_debt_cr: float,
    shares_outstanding_cr: float,
) -> float | None:
    """Two-stage DCF for one growth scenario; returns per-share intrinsic value.

    Stage 1 (years 1-5): FCF grows at full scenario growth rate.
    Stage 2 (years 6-10): growth tapers linearly from scenario rate to terminal rate.
    Terminal value uses Gordon Growth at end of year 10.

    Returns None if inputs are degenerate (zero/negative shares, WACC ≤ terminal growth).
    """
    if shares_outstanding_cr <= 0 or base_fcf_cr <= 0:
        return None

    wacc = wacc_pct / 100
    tg = terminal_growth_pct / 100
    g = growth_rate_pct / 100

    # Guard: terminal growth must stay below WACC for Gordon Growth to be finite.
    if tg >= wacc:
        tg = wacc - 0.02  # enforce 2 pp minimum spread

    pv_sum = 0.0
    fcf = base_fcf_cr

    for year in range(1, 11):
        if year <= 5:
            g_year = g
        else:
            # Linear taper: year 6 → full g, year 10 → terminal g
            fraction = (year - 5) / 5
            g_year = g + fraction * (tg - g)
        fcf = fcf * (1 + g_year)
        pv_sum += fcf / (1 + wacc) ** year

    # Terminal value: FCF in year 11 capitalised at WACC − terminal_growth
    fcf_11 = fcf * (1 + tg)
    terminal_value = fcf_11 / (wacc - tg)
    pv_terminal = terminal_value / (1 + wacc) ** 10

    equity_value_cr = pv_sum + pv_terminal - net_debt_cr
    if equity_value_cr <= 0:
        return None

    # Both equity_value and shares are in "crore" units → result in ₹/share
    return round(equity_value_cr / shares_outstanding_cr, 2)


def _estimate_base_fcf(state: AnalysisState) -> float | None:
    """Estimate the base trailing FCF in crores.

    Priority order:
    1. ValuationData.fcf_latest_cr (direct, most reliable)
    2. Net profit × CFO/NP ratio  (net profit = market_cap / PE)
    3. EBITDA × 0.55 conversion   (EBITDA derived from EV / EV_EBITDA multiple)
    """
    v = state.valuation_data
    f = state.financials
    q = state.quote

    if v and v.fcf_latest_cr is not None and v.fcf_latest_cr > 0:
        return v.fcf_latest_cr

    if q and v and f:
        if v.pe_current and v.pe_current > 0 and f.cfo_net_profit_3y_avg:
            net_profit_cr = q.market_cap_cr / v.pe_current
            fcf_est = net_profit_cr * (f.cfo_net_profit_3y_avg / 100)
            if fcf_est > 0:
                return round(fcf_est, 2)

    if q and v and v.ev_ebitda_current and v.ev_ebitda_current > 0:
        net_debt = (v.net_debt_cr or 0)
        ev = q.market_cap_cr + net_debt
        ebitda_cr = ev / v.ev_ebitda_current
        fcf_est = ebitda_cr * _EBITDA_TO_FCF_CONVERSION
        if fcf_est > 0:
            return round(fcf_est, 2)

    return None


def _normalized_base_fcf(state: AnalysisState) -> float | None:
    """EC-02: mid-cycle base FCF for cyclical sectors.

    Latest-year FCF overstates intrinsic value at cycle peaks (and understates it
    at troughs), so cyclicals are valued on normalized profitability instead:
    trailing revenue × 5Y average EBITDA margin × the standard FCF conversion.
    Returns None when the required inputs are missing — caller falls back to the
    standard latest-FCF estimate and flags the gap.
    """
    f = state.financials
    if f is None or f.trailing_revenue_cr is None or f.ebitda_margin_5y_avg is None:
        return None
    if f.trailing_revenue_cr <= 0 or f.ebitda_margin_5y_avg <= 0:
        return None
    return round(
        f.trailing_revenue_cr * (f.ebitda_margin_5y_avg / 100) * _EBITDA_TO_FCF_CONVERSION,
        2,
    )


def _cagr_blend(five_year: float | None, three_year: float | None) -> float | None:
    """60/40 blend of 5Y and 3Y CAGR; whichever single figure exists otherwise."""
    if five_year is not None and three_year is not None:
        return 0.6 * five_year + 0.4 * three_year
    if five_year is not None:
        return five_year
    return three_year


def _derive_growth_rates(state: AnalysisState) -> tuple[float, float, float, str]:
    """Derive base/bull/bear annual FCF growth rates and an anchor description.

    The anchor is a 60/40 blend of 5Y/3Y revenue CAGR, profitability-checked
    against the same blend of PAT CAGR::

        anchor = min(revenue_blend, (revenue_blend + pat_blend) / 2)

    The DCF projects FCF — a profit-derived quantity — so revenue growth alone
    overstates it when margins are deteriorating (PAT lagging revenue drags the
    anchor down).  The asymmetry is deliberate: margin *expansion* is never
    extrapolated (the min() keeps the anchor at the revenue blend), because
    margin gains are finite while revenue growth can compound.

    Haircuts keep the scenarios conservative: base = anchor × 0.85,
    bull = anchor × 1.10, bear = anchor × 0.60, all capped for Gordon Growth
    stability.
    """
    f = state.financials
    rev_blend = _cagr_blend(
        f.revenue_cagr_5y if f else None, f.revenue_cagr_3y if f else None
    )
    pat_blend = _cagr_blend(
        f.pat_cagr_5y if f else None, f.pat_cagr_3y if f else None
    )

    rev_label = "revenue blend"
    if rev_blend is None:
        rev_blend = 10.0  # conservative default when no revenue history
        rev_label = "default (no revenue history)"

    if pat_blend is not None:
        anchor = min(rev_blend, (rev_blend + pat_blend) / 2)
        if anchor < rev_blend:
            anchor_note = (
                f"PAT-checked ({rev_label} {rev_blend:.1f}% cut to {anchor:.1f}% "
                f"by PAT blend {pat_blend:.1f}%)"
            )
        else:
            anchor_note = f"{rev_label} {rev_blend:.1f}% (PAT confirms)"
    else:
        anchor = rev_blend
        anchor_note = f"{rev_label} {rev_blend:.1f}% (PAT history unavailable)"

    anchor = max(0.0, min(anchor, 30.0))  # cap at 30% for Gordon stability

    base = anchor * 0.85
    bull = min(anchor * 1.10, 35.0)
    bear = anchor * 0.60

    return base, bull, bear, anchor, anchor_note


# ---------------------------------------------------------------------------
# Reverse DCF — implied growth (expectations investing)
# ---------------------------------------------------------------------------

# Bisection search bounds for the implied growth rate (% per year).
_IMPLIED_GROWTH_LOW = -20.0
_IMPLIED_GROWTH_HIGH = 60.0


def _implied_growth_pct(
    state: AnalysisState,
    wacc_pct: float,
    terminal_growth_pct: float,
    normalized_fcf_cr: float | None = None,
) -> float | None:
    """Reverse DCF: the stage-1 FCF growth rate at which intrinsic value == CMP.

    Answers "what is the market pricing in?" — comparing the result against the
    historically delivered growth anchor is one of the highest-signal valuation
    checks available, and it reuses the exact forward-DCF machinery (same base
    FCF including EC-02 normalization, same WACC and terminal growth), so the
    two numbers are directly comparable.

    Returns the clamped bound when CMP falls outside the searchable range
    (≤ -20%: cheaper than even a shrinking-FCF scenario justifies;
    ≥ +60%: extreme expectations).  None when DCF inputs are unavailable.
    """
    q = state.quote
    if q is None or q.cmp <= 0:
        return None
    base_fcf = (
        normalized_fcf_cr if normalized_fcf_cr is not None else _estimate_base_fcf(state)
    )
    shares = _get_shares_outstanding(state)
    if base_fcf is None or shares is None:
        return None
    v = state.valuation_data
    net_debt = (v.net_debt_cr or 0.0) if v else 0.0
    cmp = q.cmp

    def _value_at(growth_pct: float) -> float:
        iv = _dcf_single_scenario(
            base_fcf, growth_pct, wacc_pct, terminal_growth_pct, net_debt, shares
        )
        # Inputs are pre-validated, so None here means equity value ≤ 0
        # (heavy net debt + shrinking FCF) — treat as a zero-value scenario
        # so the bisection stays monotonic.
        return iv if iv is not None else 0.0

    lo, hi = _IMPLIED_GROWTH_LOW, _IMPLIED_GROWTH_HIGH
    if _value_at(lo) >= cmp:
        return lo
    if _value_at(hi) <= cmp:
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if _value_at(mid) < cmp:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.01:
            break
    return round((lo + hi) / 2, 2)


def _get_shares_outstanding(state: AnalysisState) -> float | None:
    """Return shares outstanding in crores; falls back to market_cap / CMP derivation."""
    v = state.valuation_data
    q = state.quote
    if v and v.shares_outstanding_cr and v.shares_outstanding_cr > 0:
        return v.shares_outstanding_cr
    if q and q.cmp and q.cmp > 0 and q.market_cap_cr:
        return round(q.market_cap_cr / q.cmp, 4)
    return None


def _run_deterministic_dcf(
    state: AnalysisState,
    wacc_pct: float,
    terminal_growth_pct: float,
    normalized_fcf_cr: float | None = None,
) -> tuple[float | None, float | None, float | None, float | None, str]:
    """Compute 3-scenario DCF intrinsics and return (base, bull, bear, weighted, assumptions).

    Args:
        normalized_fcf_cr: When set (EC-02 cyclical sectors), used as the base FCF
            instead of the latest-year estimate.

    Returns (None, None, None, None, reason) when inputs are insufficient.
    Weighted average: 50% base, 25% bull, 25% bear.
    """
    base_fcf = normalized_fcf_cr if normalized_fcf_cr is not None else _estimate_base_fcf(state)
    if base_fcf is None:
        return None, None, None, None, "Insufficient FCF/earnings data for DCF"

    shares = _get_shares_outstanding(state)
    if shares is None:
        return None, None, None, None, "Shares outstanding unavailable"

    v = state.valuation_data
    net_debt = (v.net_debt_cr or 0.0) if v else 0.0

    growth_base, growth_bull, growth_bear, _anchor, growth_anchor_note = _derive_growth_rates(state)

    iv_base = _dcf_single_scenario(base_fcf, growth_base, wacc_pct, terminal_growth_pct, net_debt, shares)
    iv_bull = _dcf_single_scenario(base_fcf, growth_bull, wacc_pct, terminal_growth_pct, net_debt, shares)
    iv_bear = _dcf_single_scenario(base_fcf, growth_bear, wacc_pct, terminal_growth_pct, net_debt, shares)

    if iv_base is None and iv_bull is None and iv_bear is None:
        return None, None, None, None, "All DCF scenarios produced negative equity value"

    valid = [(iv_base, 0.50), (iv_bull, 0.25), (iv_bear, 0.25)]
    total_w, weighted = 0.0, 0.0
    for iv, w in valid:
        if iv is not None:
            weighted += iv * w
            total_w += w
    iv_weighted = round(weighted / total_w, 2) if total_w > 0 else None

    fcf_label = "Normalized base FCF (EC-02)" if normalized_fcf_cr is not None else "Base FCF"
    assumptions = (
        f"{fcf_label} ₹{base_fcf:.0f}Cr; growth {growth_base:.1f}%/{growth_bull:.1f}%/{growth_bear:.1f}% "
        f"(base/bull/bear, anchor: {growth_anchor_note}); WACC {wacc_pct:.1f}%; "
        f"terminal growth {terminal_growth_pct:.1f}%"
    )
    return iv_base, iv_bull, iv_bear, iv_weighted, assumptions


# ---------------------------------------------------------------------------
# P/S ratio for pre-profit companies  (Fix 2.2)
# ---------------------------------------------------------------------------

def _ps_ratio_verdict(state: AnalysisState) -> tuple[str, int]:
    """P/S ratio verdict for pre-profit companies.  Returns (verdict, in_buy_zone 0/1)."""
    f = state.financials
    q = state.quote
    if f is None or q is None:
        return "UNKNOWN", 0
    rev = f.trailing_revenue_cr
    if rev is None or rev <= 0:
        return "UNKNOWN", 0
    ps = q.market_cap_cr / rev
    verdict = _band_verdict(ps, _PS_BANDS)
    return verdict, (1 if verdict in BUY_ZONE_VERDICTS else 0)


# ---------------------------------------------------------------------------
# Step class
# ---------------------------------------------------------------------------

class Step5Valuation(BaseStep):
    """Valuation gate — 5 methods, deterministic DCF, deterministic gate."""

    step_number = 5
    step_name = "Valuation & Margin of Safety"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    @staticmethod
    def _detect_pre_profit(state: AnalysisState) -> bool:
        """Return True if EBITDA margin is negative or both PAT CAGRs < –20%."""
        f = state.financials
        if f is None:
            return False
        if f.ebitda_margin_latest is not None and f.ebitda_margin_latest < 0:
            return True
        if (
            f.pat_cagr_5y is not None
            and f.pat_cagr_3y is not None
            and f.pat_cagr_5y < -20
            and f.pat_cagr_3y < -20
        ):
            return True
        return False

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Evaluate all valuation methods and determine gate."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        v = state.valuation_data
        f = state.financials
        q = state.quote
        data_flags: list[str] = []
        methods_in_buy_zone = 0

        cmp = q.cmp if q else 0.0

        # EC-04: conglomerate note
        if getattr(state, "is_conglomerate", False):
            data_flags.append(
                "[EC-04: CONGLOMERATE — standard consolidated DCF likely understates intrinsic value. "
                "Recommended: sum-of-parts (SOTP) valuation per business segment. "
                "Apply a 10–20% holding-company discount to SOTP unless cross-holdings are minimal.]"
            )

        # EC-01: pre-profit guard
        is_pre_profit = self._detect_pre_profit(state)
        if is_pre_profit:
            ec01_flag = (
                "[EC-01: PRE-PROFIT COMPANY — DCF, P/E and PEG methods skipped; "
                "P/S ratio used where data permits; cap suggested allocation ≤ 4%; "
                "treat as high-risk speculative position]"
            )
            data_flags.append(ec01_flag)
            state.add_flag(ec01_flag)

        # --- Method 1: Historical P/E percentile (skipped for pre-profit) ---
        pe_pct_verdict = _band_verdict(v.pe_10y_percentile if v else None, _PE_PERCENTILE_BANDS)
        if is_pre_profit:
            pe_pct_verdict = "N/A"
        elif v is None or v.pe_10y_percentile is None:
            data_flags.append("[DATA UNVERIFIED: pe_10y_percentile]")
        elif pe_pct_verdict in BUY_ZONE_VERDICTS:
            methods_in_buy_zone += 1

        # --- Method 2: PEG ratio (skipped for pre-profit) ---
        peg_v = _band_verdict(v.peg_ratio if v else None, _PEG_BANDS)
        if is_pre_profit:
            peg_v = "N/A"
        elif v is None or v.peg_ratio is None:
            data_flags.append("[DATA UNVERIFIED: peg_ratio]")
        elif peg_v in BUY_ZONE_VERDICTS:
            methods_in_buy_zone += 1

        # --- Method 3: Deterministic DCF (skipped for pre-profit) ---
        dcf_base: float | None = None
        dcf_bull: float | None = None
        dcf_bear: float | None = None
        dcf_weighted: float | None = None
        dcf_assumptions: str = ""
        implied_growth: float | None = None
        growth_anchor: float | None = None
        dcf_data_sufficient = f is not None and q is not None

        if is_pre_profit:
            data_flags.append("[EC-01: DCF skipped — negative earnings make FCF projections unreliable]")
        elif dcf_data_sufficient:
            profile = get_sector_profile(state.sector_name)

            # EC-02: cyclical sectors are valued on mid-cycle (5Y average)
            # profitability — latest-year FCF overvalues at cycle peaks.
            normalized_fcf: float | None = None
            if profile.use_normalized_ebitda:
                normalized_fcf = _normalized_base_fcf(state)
                if normalized_fcf is not None:
                    data_flags.append(
                        f"[EC-02 CYCLICAL: DCF base FCF normalized to ₹{normalized_fcf:.0f} Cr "
                        f"(trailing revenue × 5Y avg OPM {f.ebitda_margin_5y_avg:.1f}% × "
                        f"{_EBITDA_TO_FCF_CONVERSION:.2f} FCF conversion) instead of latest-year FCF]"
                    )
                else:
                    data_flags.append(
                        "[EC-02 CYCLICAL: margin normalization skipped — trailing revenue or "
                        "5Y avg OPM unavailable; DCF uses latest FCF estimate]"
                    )

            cap_wacc = {
                "large_cap": settings.wacc_large_cap,
                "mid_cap": settings.wacc_mid_cap,
                "small_cap": settings.wacc_small_cap,
            }.get(state.cap_size, settings.wacc_mid_cap)
            wacc = cap_wacc + profile.wacc_adjustment
            terminal_growth = settings.wacc_terminal_growth

            dcf_base, dcf_bull, dcf_bear, dcf_weighted, dcf_assumptions = _run_deterministic_dcf(
                state, wacc, terminal_growth, normalized_fcf_cr=normalized_fcf
            )
            if dcf_weighted is None:
                data_flags.append(f"[DATA UNVERIFIED: dcf_intrinsic — {dcf_assumptions}]")

            # --- Reverse DCF (advisory): what growth is the market pricing in? ---
            _, _, _, growth_anchor, _ = _derive_growth_rates(state)
            implied_growth = _implied_growth_pct(
                state, wacc, terminal_growth, normalized_fcf_cr=normalized_fcf
            )
            if implied_growth is not None and growth_anchor is not None:
                expectation_gap = implied_growth - growth_anchor
                if implied_growth >= _IMPLIED_GROWTH_HIGH:
                    data_flags.append(
                        f"[REVERSE DCF: CMP implies ≥{_IMPLIED_GROWTH_HIGH:.0f}% annual FCF "
                        f"growth vs {growth_anchor:.1f}% delivered — extreme expectations; "
                        "price embeds a story far beyond historical execution]"
                    )
                elif expectation_gap > 5.0:
                    data_flags.append(
                        f"[REVERSE DCF: CMP implies {implied_growth:.1f}% annual FCF growth vs "
                        f"{growth_anchor:.1f}% delivered (+{expectation_gap:.1f}pp) — thesis must "
                        "justify acceleration beyond history]"
                    )
                elif expectation_gap < -5.0:
                    data_flags.append(
                        f"[POSITIVE — REVERSE DCF: CMP implies only {implied_growth:.1f}% annual "
                        f"FCF growth vs {growth_anchor:.1f}% delivered ({expectation_gap:.1f}pp); "
                        "market expectations are below demonstrated execution]"
                    )

        dcf_failed = dcf_weighted is None and not is_pre_profit and dcf_data_sufficient

        mos: float | None = None
        if dcf_weighted is not None and cmp > 0:
            mos = _compute_mos(cmp, dcf_weighted)
            if mos is not None and mos >= state.required_mos_pct:
                methods_in_buy_zone += 1

        if is_pre_profit:
            mos = None  # no reliable intrinsic; MoS gate is deliberately unmet

        # --- Method 4: FCF Yield ---
        fcf_yield_v = _fcf_yield_verdict(v.fcf_yield_pct if v else None)
        if v is None or v.fcf_yield_pct is None:
            data_flags.append("[DATA UNVERIFIED: fcf_yield]")
        elif fcf_yield_v in BUY_ZONE_VERDICTS:
            methods_in_buy_zone += 1

        # --- Method 5: EV/EBITDA (sector-conditional) ---
        profile = get_sector_profile(state.sector_name)
        if is_pre_profit:
            ev_ebitda_v = "N/A"
            data_flags.append("[EC-01: EV/EBITDA skipped — negative EBITDA makes ratio meaningless]")
        elif not profile.ev_ebitda_applicable:
            ev_ebitda_v = "N/A"
            data_flags.append(
                f"[SECTOR: EV/EBITDA method skipped — not applicable for {state.sector_name}]"
            )
        else:
            ev_ebitda_v = _band_verdict(v.ev_ebitda_current if v else None, _EV_EBITDA_BANDS)
            if v is None or v.ev_ebitda_current is None or ev_ebitda_v == "UNKNOWN":
                data_flags.append("[DATA UNVERIFIED: ev_ebitda]")
            elif ev_ebitda_v in BUY_ZONE_VERDICTS:
                methods_in_buy_zone += 1

        # --- P/S ratio (pre-profit only, replaces skipped methods) ---
        ps_verdict = "N/A"
        if is_pre_profit:
            ps_verdict, ps_in_zone = _ps_ratio_verdict(state)
            methods_in_buy_zone += ps_in_zone
            if ps_verdict == "UNKNOWN":
                data_flags.append(
                    "[DATA UNVERIFIED: ps_ratio — trailing_revenue_cr not available; "
                    "P/S method skipped]"
                )

        # --- Gate determination ---
        mos_met = mos is not None and mos >= state.required_mos_pct

        # Count verdicts that carry genuine negative signal (not UNKNOWN/N/A)
        evaluated_verdicts = [
            pe_pct_verdict, peg_v, fcf_yield_v, ev_ebitda_v
        ]
        genuine_negative_count = sum(
            1 for vv in evaluated_verdicts if vv in {"EXPENSIVE", "AVOID"}
        )

        if methods_in_buy_zone >= 2 and mos_met:
            gate = GateResult.PASS_GREEN
        elif methods_in_buy_zone >= 1:
            gate = GateResult.PASS_CONDITIONAL
        elif dcf_failed and genuine_negative_count < 2:
            # DCF data gap AND market-based signals are ambiguous (UNKNOWN/N/A) —
            # treat as data gap, not a genuine valuation fail.
            gate = GateResult.PASS_CONDITIONAL
            data_flags.append(
                "[DATA UNVERIFIED: valuation gate uncertain — DCF failed and insufficient "
                "market-based metrics; treat as PASS_CONDITIONAL pending manual DCF]"
            )
        else:
            # ≥2 methods returned EXPENSIVE/AVOID, or no methods passed at all
            gate = GateResult.FAIL

        result = ValuationResult(
            gate=gate,
            dcf_intrinsic_base=dcf_base,
            dcf_intrinsic_bull=dcf_bull,
            dcf_intrinsic_bear=dcf_bear,
            dcf_intrinsic_weighted=dcf_weighted,
            implied_growth_pct=implied_growth,
            growth_anchor_pct=growth_anchor,
            margin_of_safety_pct=mos,
            required_mos_pct=state.required_mos_pct,
            mos_met=mos_met,
            methods_in_buy_zone=methods_in_buy_zone,
            max_methods=5,
            pe_percentile_verdict=pe_pct_verdict,
            peg_verdict=peg_v,
            fcf_yield_verdict=fcf_yield_v,
            ev_ebitda_verdict=ev_ebitda_v,
            data_flags=data_flags,
        )
        state.valuation = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            methods_in_buy_zone=methods_in_buy_zone,
            mos_pct=mos,
            required_mos_pct=state.required_mos_pct,
            dcf_assumptions=dcf_assumptions,
        )

        for flag in data_flags:
            state.add_flag(flag)

        # Valuation FAIL → Watchlist Tier 2; NOT a hard pipeline termination
        if gate == GateResult.FAIL:
            # Determine tier quality: Tier 1 only when all upstream gates are PASS_GREEN
            # and moat durability is at least Medium (high-quality company, just expensive).
            upstream_all_green = (
                state.pre_screen and state.pre_screen.gate == GateResult.PASS_GREEN
                and state.governance and state.governance.gate == GateResult.PASS_GREEN
                and state.financial_gate and state.financial_gate.gate == GateResult.PASS_GREEN
                and state.moat and state.moat.moat_durability in ("High", "Medium")
            )
            tier = WatchlistTier.TIER_1 if upstream_all_green else WatchlistTier.TIER_2
            state.recommendation_type = "WATCHLIST"
            state.watchlist_tier = tier
            state.termination_reason = (
                f"Valuation not in buy zone: {methods_in_buy_zone} methods in zone, "
                f"MoS {mos:.1f}% vs required {state.required_mos_pct:.1f}%"
                if mos is not None
                else "Valuation not in buy zone: insufficient data"
            )
            # NOT a termination: terminated_at_step stays None so Steps 6–8 still run.
            # A WATCHLIST entry needs technical entry levels, peer comparison (which
            # may upgrade to PEER_SWITCH) and a premortem — watchlist-alerts acts on
            # this analysis later without re-running the pipeline.
            self.log.info(
                "pipeline_watchlist",
                step=self.step_number,
                ticker=state.ticker,
                tier=tier.value,
                reason=state.termination_reason,
            )

        return state
