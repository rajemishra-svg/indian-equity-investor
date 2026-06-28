"""Step 3G — Growth Financial Gate (deterministic, no Claude).

Replaces the value-mode Step 3 for growth companies.  Profitability gates are
replaced with growth momentum gates.  Hard triggers still terminate immediately;
soft scoring checks capital efficiency and earnings quality.

Pass threshold: 4/7 soft score (no hard triggers fired).
"""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.models import (
    AnalysisState,
    FinancialGateResult,
    FinancialMetrics,
    GateResult,
    GrowthMetrics,
)


def _compute_roiic(
    f: FinancialMetrics,
    gm: GrowthMetrics,
) -> tuple[float | None, str]:
    """Return (roiic_pct, method) using capex data if available else CFO proxy.

    Primary:  ROIIC = ΔEBIT(3Y) / (cumulative capex 3Y + ΔWC 3Y)
    Fallback: CFO/Revenue efficiency — if CFO growing faster than revenue the
              company generates incremental returns above sustaining capex.

    Returns (value_or_None, "direct" | "cfo_proxy" | "unavailable").
    """
    # Primary: need capex series and EBIT values
    capex_series = f.capex_cr_3y if f.capex_cr_3y else []
    ebit_latest = f.ebit_cr_latest

    if len(capex_series) >= 2 and ebit_latest is not None:
        cumulative_capex = sum(capex_series)
        # ΔWC proxy: Δ(receivables) — simplification since payables not extracted
        dw_rec = None
        # Use EBIT current vs implied prior (revenue_cagr_3y back-calculate)
        if f.revenue_cagr_3y and f.ebitda_margin_latest and ebit_latest:
            rev_3y_ago = (f.trailing_revenue_cr or 0) / ((1 + f.revenue_cagr_3y / 100) ** 3)
            ebit_3y_ago = rev_3y_ago * (f.ebitda_margin_latest / 100) * 0.7  # rough D&A adj
            delta_ebit = ebit_latest - ebit_3y_ago
            denominator = cumulative_capex + (dw_rec or 0)
            if denominator > 0:
                roiic = (delta_ebit / denominator) * 100
                return round(roiic, 1), "direct"

    # Fallback: CFO/Revenue efficiency
    if gm.roiic_proxy_cfo_revenue is not None:
        return gm.roiic_proxy_cfo_revenue, "cfo_proxy"

    return None, "unavailable"


class Step3GrowthFinancials(BaseStep):
    """Growth financial gate — momentum, capital efficiency, earnings quality."""

    step_number = 3
    step_name = "Growth Financial Gate"

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
        gm = state.growth_metrics or GrowthMetrics()

        hard_triggers: list[str] = []
        hurdles_met: dict[str, bool] = {}
        data_flags: list[str] = []

        # ==================================================================
        # HARD TRIGGERS — terminate immediately
        # ==================================================================

        # HT-G1: Revenue deceleration for 3 consecutive years
        # (3Y CAGR much higher than what recent data suggests is a warning sign;
        #  we detect this when 1Y rev CAGR < 3Y CAGR by a significant margin)
        rev_1y = gm.revenue_cagr_1y
        rev_3y = f.revenue_cagr_3y if f else None
        if rev_1y is not None and rev_3y is not None:
            decel = rev_3y - rev_1y
            if decel > 15 and rev_1y < 15:
                hard_triggers.append(
                    f"[HT-G1: REVENUE DECELERATION — 3Y CAGR {rev_3y:.1f}% vs "
                    f"latest YoY {rev_1y:.1f}% (Δ {decel:.1f}pp); growth story may be breaking]"
                )

        # HT-G2: Cash runway < 6 months (existential risk)
        runway = gm.cash_runway_months
        burn = gm.burn_rate_cr_month
        if runway is not None and burn is not None and burn > 0 and runway < 6:
            hard_triggers.append(
                f"[HT-G2: CASH RUNWAY {runway:.0f} MONTHS — existential risk; "
                "forced dilution or debt raise imminent]"
            )

        # HT-G3: Gross margin contracting > 5pp in 2Y (unit economics collapse)
        gm_series = f.gross_profit_margin_series if f else []
        if len(gm_series) >= 2:
            recent_gm = gm_series[-1]
            prior_gm = gm_series[-2]
            if prior_gm - recent_gm > 5:
                hard_triggers.append(
                    f"[HT-G3: GROSS MARGIN COLLAPSE — contracted {prior_gm - recent_gm:.1f}pp "
                    f"from {prior_gm:.1f}% to {recent_gm:.1f}%; unit economics deteriorating]"
                )

        # HT-G4: D/E > 2 (over-leveraged for a growth company)
        de = f.debt_to_equity if f else None
        if de is not None and de > 2.0:
            hard_triggers.append(
                f"[HT-G4: D/E {de:.2f} > 2.0 — growth company over-leveraged; "
                "debt amplifies downside risk during growth deceleration]"
            )

        # HT-G5: ROIIC < 8% — unit economics too weak to justify growth premium
        # Negative or sub-8% ROIIC means the company destroys value on each
        # incremental rupee invested; at scale this becomes fatal.
        roiic_val, roiic_method = _compute_roiic(f, gm)
        if roiic_method != "unavailable" and roiic_val is not None and roiic_val < 8.0:
            hard_triggers.append(
                f"[HT-G5: ROIIC {roiic_val:.1f}% < 8% (method: {roiic_method}) — "
                "unit economics insufficient; company cannot scale profitably; "
                "growth premium is unjustified at current capital efficiency]"
            )

        if hard_triggers:
            for t in hard_triggers:
                state.add_flag(t)

            result = FinancialGateResult(
                score=0,
                gate=GateResult.FAIL,
                hard_triggers_fired=hard_triggers,
                hurdles_met=hurdles_met,
                data_flags=data_flags,
            )
            state.financial_gate = result
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Growth financials FAILED — hard triggers: {'; '.join(hard_triggers)}"
            )
            state.recommendation_type = "GROWTH_REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                triggers=hard_triggers,
            )
            return state

        # ==================================================================
        # SOFT SCORING — 7 hurdles, pass threshold 4/7
        # ==================================================================
        score = 0.0

        # G3-1: Revenue CAGR 3Y  (0-2 pts — highest weight)
        if rev_3y is not None:
            if rev_3y >= 40:
                hurdles_met["revenue_cagr_3y"] = True
                score += 2
                data_flags.append(f"[POSITIVE: revenue CAGR 3Y {rev_3y:.1f}% — excellent growth]")
            elif rev_3y >= 25:
                hurdles_met["revenue_cagr_3y"] = True
                score += 1
            else:
                hurdles_met["revenue_cagr_3y"] = False
        else:
            hurdles_met["revenue_cagr_3y"] = False
            data_flags.append("[DATA UNVERIFIED: revenue_cagr_3y]")

        # G3-2: Gross margin expanding (1 pt)
        gm_trend = gm.gross_margin_trend
        if gm_trend == "expanding":
            hurdles_met["gross_margin_expanding"] = True
            score += 1
            data_flags.append("[POSITIVE: gross margin expanding — unit economics improving at scale]")
        elif gm_trend == "stable":
            hurdles_met["gross_margin_expanding"] = True
            score += 0.5
        elif gm_trend == "contracting":
            hurdles_met["gross_margin_expanding"] = False
        else:
            hurdles_met["gross_margin_expanding"] = True  # unknown → neutral
            data_flags.append("[DATA UNVERIFIED: gross_margin_trend]")
            score += 0.5

        # G3-3: Rule of 40 (1 pt)
        r40 = gm.rule_of_40_score
        if r40 is not None:
            if r40 >= 40:
                hurdles_met["rule_of_40"] = True
                score += 1
                data_flags.append(f"[POSITIVE: Rule of 40 score {r40:.0f} ≥ 40 — healthy growth-margin balance]")
            elif r40 >= 25:
                hurdles_met["rule_of_40"] = True
                score += 0.5
            else:
                hurdles_met["rule_of_40"] = False
        else:
            hurdles_met["rule_of_40"] = True  # unknown → neutral (waived)
            data_flags.append("[DATA UNVERIFIED: rule_of_40 — computed from revenue CAGR + EBITDA margin]")
            score += 0.5

        # G3-4: ROIIC soft score (0-1 pt) — only reached if ROIIC ≥ 8% (HT-G5 above
        # already hard-terminated anything below 8%, so this scores the 8-20% range)
        if roiic_method != "unavailable" and roiic_val is not None:
            if roiic_method == "cfo_proxy":
                data_flags.append(
                    f"[ROIIC PROXY: CFO/Revenue efficiency used (capex unavailable); "
                    f"proxy value {roiic_val:.1f}%]"
                )
            if roiic_val >= 20:
                hurdles_met["roiic"] = True
                score += 1
                data_flags.append(f"[POSITIVE: ROIIC {roiic_val:.1f}% ≥ 20% — high-quality reinvestment]")
            elif roiic_val >= 10:
                hurdles_met["roiic"] = True
                score += 0.5
            else:
                hurdles_met["roiic"] = False
        else:
            hurdles_met["roiic"] = True  # waived when unavailable
            data_flags.append("[DATA UNVERIFIED: ROIIC — capex and D&A not available; check scored neutral]")
            score += 0.5

        # G3-5: Earnings quality — CFO > 0 indicates real cash generation
        cfo_np = f.cfo_net_profit_3y_avg if f else None
        if cfo_np is not None:
            if cfo_np > 0:
                hurdles_met["earnings_quality"] = True
                score += 1
                if cfo_np >= 80:
                    data_flags.append(f"[POSITIVE: CFO/NP {cfo_np:.0f}% — strong cash conversion]")
            else:
                hurdles_met["earnings_quality"] = False
                data_flags.append(
                    "[EARNINGS QUALITY CONCERN: CFO/NP negative — "
                    "cash from operations is negative; verify burn rate timing vs accounting]"
                )
        else:
            hurdles_met["earnings_quality"] = True  # waived — acceptable for pre-profit
            data_flags.append("[DATA UNVERIFIED: cfo_net_profit — earnings quality check waived]")
            score += 0.5

        # G3-6: Working capital not blowing out (0.5 pt)
        dd_latest = f.debtor_days_latest if f else None
        dd_3y = f.debtor_days_3y_ago if f else None
        if dd_latest is not None and dd_3y is not None and dd_3y > 0:
            deterioration = (dd_latest - dd_3y) / dd_3y * 100
            if deterioration > 30:
                hurdles_met["working_capital"] = False
                data_flags.append(
                    f"[P1-3: WC DETERIORATING — debtor days up {deterioration:.0f}% "
                    f"({dd_3y:.0f} → {dd_latest:.0f} days); "
                    "revenue quality may be declining (collection risk)]"
                )
            else:
                hurdles_met["working_capital"] = True
                score += 0.5
        else:
            hurdles_met["working_capital"] = True
            score += 0.5

        # G3-7: Equity dilution controlled (0.5 pt)
        dilution = gm.equity_dilution_3y_pct
        if dilution is not None:
            if dilution < 15:
                hurdles_met["equity_dilution"] = True
                score += 0.5
            elif dilution < 30:
                hurdles_met["equity_dilution"] = True
                score += 0.25
                data_flags.append(
                    f"[DILUTION NOTE: shares outstanding grew {dilution:.1f}% over 3Y — "
                    "moderate dilution; ensure revenue grew proportionally]"
                )
            else:
                hurdles_met["equity_dilution"] = False
                data_flags.append(
                    f"[DILUTION CONCERN: shares outstanding grew {dilution:.1f}% over 3Y — "
                    "significant dilution; per-share returns may lag headline revenue growth]"
                )
        else:
            hurdles_met["equity_dilution"] = True
            score += 0.5
            data_flags.append("[DATA UNVERIFIED: equity_dilution_3y — shares outstanding history unavailable]")

        score_int = round(score)

        if score_int >= 5:
            gate = GateResult.PASS_GREEN
        elif score_int >= 4:
            gate = GateResult.PASS_CONDITIONAL
        else:
            gate = GateResult.FAIL

        result = FinancialGateResult(
            score=score_int,
            gate=gate,
            hard_triggers_fired=[],
            hurdles_met=hurdles_met,
            data_flags=data_flags,
        )
        state.financial_gate = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            score=score_int,
        )

        if gate == GateResult.FAIL:
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Growth financials gate FAILED: score {score_int}/7; "
                "momentum or capital efficiency below minimum thresholds"
            )
            state.recommendation_type = "GROWTH_REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )
        elif gate == GateResult.PASS_CONDITIONAL:
            state.add_flag(
                f"[GROWTH FINANCIALS CONDITIONAL: score {score_int}/7 — "
                "proceed with heightened scrutiny in valuation step]"
            )

        return state
