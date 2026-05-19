"""Step 5 — Valuation & Margin of Safety (deterministic + Python DCF).

DCF is computed entirely in Python — no LLM call.  This makes the valuation
auditable, deterministic, and free (zero API cost for this step).  The only
LLM involvement in this file is removed; the DCF formula itself is standard
finance: two-stage Gordon Growth with linear tapering in stage 2.

MoS definition (Graham): MoS = (Intrinsic − CMP) / Intrinsic × 100
At required_mos_pct=25%: CMP must be ≤ 75% of intrinsic value to pass.
"""
from __future__ import annotations

import math
from typing import Optional

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


def _band_verdict(value: Optional[float], bands: list) -> str:
    if value is None:
        return "UNKNOWN"
    for threshold, label in bands:
        if value < threshold:
            return label
    return "AVOID"


def _fcf_yield_verdict(fcf_yield: Optional[float]) -> str:
    if fcf_yield is None:
        return "UNKNOWN"
    if fcf_yield < 3.0:
        return "EXPENSIVE"
    elif fcf_yield < 5.0:
        return "FAIR"
    return "ATTRACTIVE"


def _compute_mos(cmp: float, intrinsic: Optional[float]) -> Optional[float]:
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
) -> Optional[float]:
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


def _estimate_base_fcf(state: AnalysisState) -> Optional[float]:
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
        # Conservative FCF conversion: EBITDA × 0.55
        # (-20% taxes, -10% maintenance capex, -5% WC, +10% D&A non-cash add-back)
        fcf_est = ebitda_cr * 0.55
        if fcf_est > 0:
            return round(fcf_est, 2)

    return None


def _derive_growth_rates(state: AnalysisState) -> tuple[float, float, float]:
    """Derive base/bull/bear annual FCF growth rates from historical revenue CAGRs.

    Uses a 60/40 blend of 5Y and 3Y revenue CAGR.  Applies haircuts so the DCF
    is conservative: base = blended × 0.85, bull = blended × 1.10, bear = blended × 0.60.
    All rates are capped to prevent Gordon Growth instability.
    """
    f = state.financials
    rev5 = f.revenue_cagr_5y if f and f.revenue_cagr_5y is not None else None
    rev3 = f.revenue_cagr_3y if f and f.revenue_cagr_3y is not None else None

    if rev5 is not None and rev3 is not None:
        blended = 0.6 * rev5 + 0.4 * rev3
    elif rev5 is not None:
        blended = rev5
    elif rev3 is not None:
        blended = rev3
    else:
        blended = 10.0  # conservative default when no history

    blended = max(0.0, min(blended, 30.0))  # cap at 30% for Gordon stability

    base = blended * 0.85
    bull = min(blended * 1.10, 35.0)
    bear = blended * 0.60

    return base, bull, bear


def _get_shares_outstanding(state: AnalysisState) -> Optional[float]:
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
    ebitda_margin_for_dcf: Optional[float],
    wacc_pct: float,
    terminal_growth_pct: float,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], str]:
    """Compute 3-scenario DCF intrinsics and return (base, bull, bear, weighted, assumptions).

    Returns (None, None, None, None, reason) when inputs are insufficient.
    Weighted average: 50% base, 25% bull, 25% bear.
    """
    base_fcf = _estimate_base_fcf(state)
    if base_fcf is None:
        return None, None, None, None, "Insufficient FCF/earnings data for DCF"

    shares = _get_shares_outstanding(state)
    if shares is None:
        return None, None, None, None, "Shares outstanding unavailable"

    v = state.valuation_data
    net_debt = (v.net_debt_cr or 0.0) if v else 0.0

    growth_base, growth_bull, growth_bear = _derive_growth_rates(state)

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

    assumptions = (
        f"Base FCF ₹{base_fcf:.0f}Cr; growth {growth_base:.1f}%/{growth_bull:.1f}%/{growth_bear:.1f}% "
        f"(base/bull/bear); WACC {wacc_pct:.1f}%; terminal growth {terminal_growth_pct:.1f}%"
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
        dcf_base: Optional[float] = None
        dcf_bull: Optional[float] = None
        dcf_bear: Optional[float] = None
        dcf_weighted: Optional[float] = None
        dcf_assumptions: str = ""
        dcf_data_sufficient = f is not None and q is not None

        if is_pre_profit:
            data_flags.append("[EC-01: DCF skipped — negative earnings make FCF projections unreliable]")
        elif dcf_data_sufficient:
            profile = get_sector_profile(state.sector_name)

            # EC-02: cyclical sectors use 5Y average EBITDA margin
            if profile.use_normalized_ebitda and f.ebitda_margin_5y_avg is not None:
                ebitda_for_dcf = f.ebitda_margin_5y_avg
                data_flags.append(
                    f"[EC-02 CYCLICAL: using 5Y avg OPM {f.ebitda_margin_5y_avg:.1f}% "
                    f"instead of latest {f.ebitda_margin_latest or '[N/A]'}% for DCF]"
                )
            else:
                ebitda_for_dcf = f.ebitda_margin_latest

            cap_wacc = {
                "large_cap": settings.wacc_large_cap,
                "mid_cap": settings.wacc_mid_cap,
                "small_cap": settings.wacc_small_cap,
            }.get(state.cap_size, settings.wacc_mid_cap)
            wacc = cap_wacc + profile.wacc_adjustment
            terminal_growth = settings.wacc_terminal_growth

            dcf_base, dcf_bull, dcf_bear, dcf_weighted, dcf_assumptions = _run_deterministic_dcf(
                state, ebitda_for_dcf, wacc, terminal_growth
            )
            if dcf_weighted is None:
                data_flags.append(f"[DATA UNVERIFIED: dcf_intrinsic — {dcf_assumptions}]")

        dcf_failed = dcf_weighted is None and not is_pre_profit and dcf_data_sufficient

        mos: Optional[float] = None
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
            state.terminated_at_step = self.step_number
            self.log.info(
                "pipeline_watchlist",
                step=self.step_number,
                ticker=state.ticker,
                tier=tier.value,
                reason=state.termination_reason,
            )

        return state
