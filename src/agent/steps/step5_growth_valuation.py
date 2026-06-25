"""Step 5G — Growth Valuation (deterministic, no Claude).

Five valuation methods calibrated for high-growth / pre-profit companies.
No DCF margin-of-safety requirement — growth stocks don't trade at intrinsic
discounts.  The gate tests relative value vs the delivered growth rate.

Gate: ≥ 2 of 5 methods in buy zone → PASS_GREEN (proceed to BUY consideration)
      1 of 5 → PASS_CONDITIONAL → GROWTH_WATCHLIST
      0 of 5 → FAIL → GROWTH_WATCHLIST (expensive, not rejected)

Step 5M (multibagger scoring) runs after this regardless of the gate outcome.
"""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, GateResult, GrowthMetrics, ValuationResult, WatchlistTier
from src.sector.profiles import get_sector_profile

# ---------------------------------------------------------------------------
# EV / Revenue sector bands (Damodaran emerging-market norms adjusted for India)
# ---------------------------------------------------------------------------
_EV_REVENUE_BANDS: dict[str, tuple[float, float]] = {
    # (fair_max, expensive_above)
    "saas_tech":      (8.0, 15.0),
    "fintech":        (6.0, 12.0),
    "consumer_brand": (4.0,  8.0),
    "healthcare":     (5.0, 10.0),
    "ecomm_platform": (5.0, 10.0),
    "default":        (3.0,  6.0),
}

_GROWTH_SECTOR_MAP: dict[str, str] = {
    "financial_services": "fintech",
    "default":            "default",
}

BUY_ZONE_VERDICTS = {"EXCELLENT", "FAIR", "ATTRACTIVE"}


def _band_verdict(value: float, bands: list[tuple[float, str]]) -> str:
    for threshold, label in bands:
        if value <= threshold:
            return label
    return bands[-1][1]


def _ev_revenue_band(ev_rev: float, sector_key: str) -> str:
    fair_max, exp_above = _EV_REVENUE_BANDS.get(sector_key, _EV_REVENUE_BANDS["default"])
    if ev_rev <= fair_max:
        return "EXCELLENT"
    if ev_rev <= exp_above:
        return "FAIR"
    return "EXPENSIVE"


def _forward_revenue_dcf(
    trailing_revenue_cr: float,
    revenue_cagr_3y_pct: float,
    wacc_pct: float,
    terminal_ps_multiple: float = 3.0,
    projection_years: int = 7,
) -> float:
    """Project revenue at 3Y CAGR for `projection_years`, then apply terminal P/S.

    Returns present value (intrinsic value) per share in ₹ Crore (total company).
    Caller divides by shares outstanding to get per-share value.
    """
    g = revenue_cagr_3y_pct / 100
    r = wacc_pct / 100
    # Cap growth at realistic bounds
    g = min(g, 0.60)
    g = max(g, 0.0)

    # Terminal revenue at end of projection period
    terminal_revenue = trailing_revenue_cr * ((1 + g) ** projection_years)
    # Terminal value = terminal revenue × P/S multiple, discounted back
    terminal_value = terminal_revenue * terminal_ps_multiple / ((1 + r) ** projection_years)
    return terminal_value


class Step5GrowthValuation(BaseStep):
    """Growth valuation gate — five revenue/growth-relative methods."""

    step_number = 5
    step_name = "Growth Valuation"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:  # noqa: C901
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        f = state.financials
        v = state.valuation_data
        q = state.quote
        gm = state.growth_metrics or GrowthMetrics()
        profile = get_sector_profile("high_growth")  # always use growth profile here

        cmp = q.cmp if q else None
        mc = (q.market_cap_cr if q else None) or (f.market_cap_cr if f else None)
        data_flags: list[str] = []
        methods_in_buy_zone = 0
        max_methods = 0

        # Sector key for EV/Revenue bands
        sector_key = _GROWTH_SECTOR_MAP.get(state.sector_name or "", "default")

        # WACC for forward DCF (base + growth adjustment from profile)
        cap_size = state.cap_size
        wacc_base = {
            "large_cap": settings.wacc_large_cap,
            "mid_cap":   settings.wacc_mid_cap,
            "small_cap": settings.wacc_small_cap,
        }.get(cap_size, settings.wacc_mid_cap)
        wacc = wacc_base + profile.wacc_adjustment

        result = ValuationResult(gate=GateResult.NOT_RUN)
        result.max_methods = 5

        # ==================================================================
        # Method G5-1 — PEG ratio
        # Growth co PEG acceptable range: 1–3× (vs <1 for value)
        # ==================================================================
        peg = v.peg_ratio if v else None
        rev_3y = f.revenue_cagr_3y if f else None  # use revenue growth as "g" for pre-profit

        if peg is not None:
            max_methods += 1
            if peg <= 1.0:
                result.peg_verdict = "EXCELLENT"
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[POSITIVE G5-1: PEG {peg:.2f} ≤ 1.0 — "
                    "market not yet pricing in full growth; potential multibagger signal]"
                )
            elif peg <= 2.0:
                result.peg_verdict = "FAIR"
                methods_in_buy_zone += 1
                data_flags.append(f"[G5-1: PEG {peg:.2f} — fair; growth partially priced in]")
            elif peg <= 3.0:
                result.peg_verdict = "EXPENSIVE"
                data_flags.append(f"[G5-1: PEG {peg:.2f} — expensive; paying full premium for growth]")
            else:
                result.peg_verdict = "AVOID"
                data_flags.append(f"[G5-1: PEG {peg:.2f} > 3.0 — growth significantly overpriced]")
        else:
            data_flags.append("[G5-1: PEG unavailable — PE may be negative (pre-profit); skipped]")

        # ==================================================================
        # Method G5-2 — EV / Revenue (sector bands)
        # ==================================================================
        ev_rev = gm.ev_revenue_ratio
        if ev_rev is None and mc is not None and f and f.trailing_revenue_cr:
            # Compute from available data: EV ≈ market cap + net debt
            net_debt = v.net_debt_cr if v and v.net_debt_cr is not None else 0.0
            ev = mc + net_debt
            ev_rev = round(ev / f.trailing_revenue_cr, 2)
            gm.ev_revenue_ratio = ev_rev

        if ev_rev is not None:
            max_methods += 1
            verdict = _ev_revenue_band(ev_rev, sector_key)
            result.ev_ebitda_verdict = f"EV/Rev {ev_rev:.1f}× — {verdict}"
            if verdict in BUY_ZONE_VERDICTS:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[POSITIVE G5-2: EV/Revenue {ev_rev:.1f}× in buy zone "
                    f"for {sector_key} sector]"
                )
            else:
                data_flags.append(f"[G5-2: EV/Revenue {ev_rev:.1f}× — {verdict}]")
        else:
            data_flags.append("[G5-2: EV/Revenue unavailable — trailing_revenue_cr missing]")

        # ==================================================================
        # Method G5-3 — Forward Revenue DCF
        # Project at 3Y CAGR for 7 years, terminal P/S = 2-4× (sector-dependent)
        # ==================================================================
        rev_cr = f.trailing_revenue_cr if f else None
        shares = v.shares_outstanding_cr if v else None

        if rev_cr and rev_3y is not None and cmp and shares and shares > 0:
            max_methods += 1
            # Terminal P/S: higher for high-quality moats, lower for commoditised
            moat = state.moat
            if moat and moat.moat_type.value in ("network_effect", "switching_costs", "brand"):
                terminal_ps = 4.0
            elif moat and moat.moat_type.value in ("regulatory", "scale"):
                terminal_ps = 3.0
            else:
                terminal_ps = 2.5

            intrinsic_total_cr = _forward_revenue_dcf(
                trailing_revenue_cr=rev_cr,
                revenue_cagr_3y_pct=rev_3y,
                wacc_pct=wacc,
                terminal_ps_multiple=terminal_ps,
                projection_years=7,
            )
            intrinsic_per_share_rs = intrinsic_total_cr / shares * 10  # ₹ (Cr → Rs conversion)

            mos_pct = (intrinsic_per_share_rs - cmp) / intrinsic_per_share_rs * 100 if intrinsic_per_share_rs > 0 else 0

            result.dcf_intrinsic_weighted = round(intrinsic_per_share_rs, 2)
            result.margin_of_safety_pct = round(mos_pct, 1)

            if mos_pct >= 20:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[POSITIVE G5-3: Forward Revenue DCF — intrinsic ₹{intrinsic_per_share_rs:.0f}, "
                    f"CMP ₹{cmp:.0f}, MoS {mos_pct:.1f}%]"
                )
            elif mos_pct >= 0:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[G5-3: Forward Revenue DCF — intrinsic ₹{intrinsic_per_share_rs:.0f}, "
                    f"CMP ₹{cmp:.0f}, MoS {mos_pct:.1f}% (thin but positive)]"
                )
            else:
                data_flags.append(
                    f"[G5-3: Forward Revenue DCF — intrinsic ₹{intrinsic_per_share_rs:.0f}, "
                    f"CMP ₹{cmp:.0f}; CMP {abs(mos_pct):.1f}% above intrinsic]"
                )
        else:
            data_flags.append(
                "[G5-3: Forward Revenue DCF skipped — "
                "trailing_revenue_cr, revenue_cagr_3y, or shares_outstanding unavailable]"
            )

        # ==================================================================
        # Method G5-4 — Rule of 40 premium check
        # Rule of 40 ≥ 50 justifies a 30% premium to sector median EV/Revenue.
        # If the company scores ≥ 50 AND EV/Rev ≤ sector_fair × 1.3, it's in zone.
        # ==================================================================
        r40 = gm.rule_of_40_score
        if r40 is not None and ev_rev is not None:
            max_methods += 1
            fair_max, _ = _EV_REVENUE_BANDS.get(sector_key, _EV_REVENUE_BANDS["default"])
            premium_ceiling = fair_max * 1.3

            if r40 >= 50 and ev_rev <= premium_ceiling:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[POSITIVE G5-4: Rule of 40 = {r40:.0f} ≥ 50 qualifies for 30% EV/Rev premium; "
                    f"EV/Rev {ev_rev:.1f}× ≤ ceiling {premium_ceiling:.1f}×]"
                )
            elif r40 >= 40 and ev_rev <= fair_max:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[G5-4: Rule of 40 = {r40:.0f}, EV/Rev {ev_rev:.1f}× within fair band]"
                )
            else:
                data_flags.append(
                    f"[G5-4: Rule of 40 = {r40:.0f}; EV/Rev {ev_rev:.1f}× — no additional premium warranted]"
                )
        else:
            data_flags.append("[G5-4: Rule of 40 premium check skipped — r40 or EV/Rev unavailable]")

        # ==================================================================
        # Method G5-5 — TAM-ceiling check
        # Current market cap < 10% of (TAM × 20% penetration × terminal P/S)
        # This tests whether there is still > 10× headroom in absolute terms.
        # ==================================================================
        tam_cr = gm.tam_size_cr
        if tam_cr is not None and mc is not None:
            max_methods += 1
            terminal_ps_5 = 3.0  # conservative terminal multiple for TAM check
            tam_ceiling = tam_cr * 0.20 * terminal_ps_5  # max value at 20% penetration
            headroom_multiple = tam_ceiling / mc if mc > 0 else 0

            if headroom_multiple >= 10:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[POSITIVE G5-5: TAM-ceiling — {headroom_multiple:.0f}× headroom "
                    f"(TAM ₹{tam_cr:,.0f} Cr; 20% penetration value ₹{tam_ceiling:,.0f} Cr "
                    f"vs market cap ₹{mc:,.0f} Cr)]"
                )
            elif headroom_multiple >= 5:
                methods_in_buy_zone += 1
                data_flags.append(
                    f"[G5-5: TAM-ceiling — {headroom_multiple:.1f}× headroom (moderate)]"
                )
            else:
                data_flags.append(
                    f"[G5-5: TAM-ceiling — only {headroom_multiple:.1f}× headroom; "
                    "market cap already substantial relative to addressable market]"
                )
        else:
            if gm.tam_size_cr is None:
                data_flags.append(
                    "[G5-5: TAM-ceiling check skipped — TAM not estimated in Step 2; "
                    "re-run with growth mode to populate TAM data]"
                )

        # ==================================================================
        # Gate determination
        # ==================================================================
        result.methods_in_buy_zone = methods_in_buy_zone
        result.max_methods = max_methods if max_methods > 0 else 5
        result.data_flags = data_flags

        if max_methods == 0:
            # No valuation data at all
            result.gate = GateResult.FAIL
            state.add_flag("[GROWTH VALUATION: no valuation methods could be evaluated — data insufficient]")
        elif methods_in_buy_zone >= 2:
            result.gate = GateResult.PASS_GREEN
        elif methods_in_buy_zone == 1:
            result.gate = GateResult.PASS_CONDITIONAL
        else:
            result.gate = GateResult.FAIL

        state.valuation = result

        for flag in data_flags:
            state.add_flag(flag)

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=result.gate.value,
            methods_in_buy_zone=methods_in_buy_zone,
            max_methods=max_methods,
            wacc=wacc,
        )

        # Growth valuation fail → GROWTH_WATCHLIST (not REJECT — the business may be fine)
        if result.gate in (GateResult.FAIL, GateResult.PASS_CONDITIONAL):
            state.recommendation_type = "GROWTH_WATCHLIST"
            state.watchlist_tier = WatchlistTier.TIER_2
            state.add_flag(
                "[GROWTH VALUATION: insufficient methods in buy zone — "
                "adding to GROWTH_WATCHLIST; monitor for valuation re-entry]"
            )

        return state
