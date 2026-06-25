"""Step 0G — Growth Pre-Screen (deterministic, no Claude needed).

9 criteria tuned for high-growth / pre-profit companies.  Pass threshold is
6/9 (vs 5/9 for value).  The emphasis shifts from profitability quality to
revenue momentum, cash health, and governance basics.
"""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.models import AnalysisState, GateResult, GrowthMetrics, PreScreenResult
from src.sector.classifier import classify_sector


class Step0GrowthPreScreen(BaseStep):
    """Growth-mode quantitative pre-screen — deterministic, no LLM call."""

    step_number = 0
    step_name = "Growth Pre-Screen"

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
        q = state.quote
        g = state.governance_data
        gm = state.growth_metrics or GrowthMetrics()

        if not state.sector_name:
            state.sector_name = classify_sector(
                company_name=state.company_name or "",
                ticker=state.ticker,
            )

        metric_scores: dict[str, bool] = {}
        failed_metrics: list[str] = []
        data_flags: list[str] = []
        conditional_exceptions: list[str] = []

        # ------------------------------------------------------------------
        # G1 — Revenue acceleration (latest YoY > 3Y CAGR, or 3Y CAGR ≥ 25%)
        # Growth stories should be speeding up, not decelerating.
        # ------------------------------------------------------------------
        rev_1y = gm.revenue_cagr_1y
        rev_3y = f.revenue_cagr_3y if f else None

        if rev_1y is not None and rev_3y is not None:
            passed_g1 = (rev_1y >= rev_3y) or (rev_3y >= 25.0)
            metric_scores["revenue_acceleration"] = passed_g1
            if not passed_g1:
                failed_metrics.append("revenue_acceleration")
                data_flags.append(
                    f"[GROWTH CONCERN: revenue decelerating — "
                    f"1Y {rev_1y:.1f}% < 3Y CAGR {rev_3y:.1f}%]"
                )
        elif rev_3y is not None:
            passed_g1 = rev_3y >= 25.0
            metric_scores["revenue_acceleration"] = passed_g1
            if not passed_g1:
                failed_metrics.append("revenue_acceleration")
        else:
            metric_scores["revenue_acceleration"] = False
            failed_metrics.append("revenue_acceleration")
            data_flags.append("[DATA UNVERIFIED: revenue_cagr — acceleration check skipped]")

        # ------------------------------------------------------------------
        # G2 — Revenue CAGR 3Y ≥ 25% (core growth gate)
        # ------------------------------------------------------------------
        if rev_3y is not None:
            passed_g2 = rev_3y >= 25.0
            metric_scores["revenue_cagr_3y >= 25"] = passed_g2
            if not passed_g2:
                failed_metrics.append("revenue_cagr_3y >= 25")
        else:
            metric_scores["revenue_cagr_3y >= 25"] = False
            failed_metrics.append("revenue_cagr_3y >= 25")
            data_flags.append("[DATA UNVERIFIED: revenue_cagr_3y]")

        # ------------------------------------------------------------------
        # G3 — Gross margin not contracting
        # Expanding or stable → pass.  Contracting > 3pp in latest 2 years → fail.
        # Service companies with no COGS data get a conditional pass with flag.
        # ------------------------------------------------------------------
        gm_trend = gm.gross_margin_trend
        if gm_trend is not None:
            passed_g3 = gm_trend in ("expanding", "stable")
            metric_scores["gross_margin_not_contracting"] = passed_g3
            if not passed_g3:
                failed_metrics.append("gross_margin_not_contracting")
                data_flags.append("[GROWTH CONCERN: gross margin contracting — unit economics deteriorating]")
        else:
            # No COGS data — possible service company; give conditional pass
            metric_scores["gross_margin_not_contracting"] = True
            conditional_exceptions.append(
                "[GROSS MARGIN UNKNOWN: COGS not available — possible service company; "
                "verify margin structure manually]"
            )

        # ------------------------------------------------------------------
        # G4 — Cash runway ≥ 18 months (if FCF-negative)
        # Auto-pass when FCF is positive (self-funded growth).
        # ------------------------------------------------------------------
        runway = gm.cash_runway_months
        fcf = state.valuation_data.fcf_latest_cr if state.valuation_data else None

        if fcf is not None and fcf > 0:
            metric_scores["cash_runway"] = True
            conditional_exceptions.append("[FCF POSITIVE: cash runway check auto-passed]")
        elif runway is not None:
            passed_g4 = runway >= 18.0
            metric_scores["cash_runway"] = passed_g4
            if not passed_g4:
                failed_metrics.append("cash_runway")
                data_flags.append(
                    f"[GROWTH RISK: cash runway {runway:.0f} months < 18-month threshold — "
                    "dilutive raise or distress likely within 18 months]"
                )
        else:
            # Runway not computable; be conservative
            metric_scores["cash_runway"] = False
            failed_metrics.append("cash_runway")
            data_flags.append("[DATA UNVERIFIED: cash_runway — burn rate or cash balance unavailable]")

        # ------------------------------------------------------------------
        # G5 — Debt / Equity ≤ 1.0 (equity-funded preferred)
        # Growth companies should NOT be debt-funded for operating losses.
        # ------------------------------------------------------------------
        de = f.debt_to_equity if f else None
        if de is not None:
            passed_g5 = de <= 1.0
            metric_scores["debt_to_equity <= 1.0"] = passed_g5
            if not passed_g5:
                failed_metrics.append("debt_to_equity <= 1.0")
        else:
            metric_scores["debt_to_equity <= 1.0"] = False
            failed_metrics.append("debt_to_equity <= 1.0")
            data_flags.append("[DATA UNVERIFIED: debt_to_equity]")

        # ------------------------------------------------------------------
        # G6 — Market cap ceiling (non-scoring flag if ≥ ₹20,000 Cr)
        # Large caps are already "discovered" — multibagger room is limited.
        # This is informational; it does not penalise the score.
        # ------------------------------------------------------------------
        mc = (q.market_cap_cr if q else None) or (
            f.market_cap_cr if f and f.market_cap_cr is not None else None
        )
        if mc is not None and mc >= 20_000:
            flag = (
                f"[EC-G1: LARGE CAP — market cap ₹{mc:,.0f} Cr ≥ ₹20,000 Cr; "
                "market has likely priced in part of the growth story; "
                "multibagger upside from here requires sustained re-rating]"
            )
            data_flags.append(flag)
            state.add_flag(flag)
        # Still award the point — size alone doesn't fail growth screen
        metric_scores["market_cap_informational"] = True

        # ------------------------------------------------------------------
        # G7 — Governance basics: pledging < 5%, no SEBI fraud
        # Tighter pledging threshold than value (5% vs 10%) — growth cos
        # often have founder-promoters where any pledge is a yellow flag.
        # ------------------------------------------------------------------
        pledging = g.promoter_pledging_pct if g else None
        sebi_clean = (not g or g.sebi_record_clean or not g.sebi_record_checked)

        if pledging is not None:
            passed_pledge = pledging < 5.0
            passed_sebi = sebi_clean
            passed_g7 = passed_pledge and passed_sebi
            metric_scores["governance_basics"] = passed_g7
            if not passed_g7:
                failed_metrics.append("governance_basics")
                if not passed_pledge:
                    data_flags.append(f"[GOVERNANCE: promoter pledging {pledging:.1f}% ≥ 5% threshold]")
                if not passed_sebi:
                    data_flags.append("[GOVERNANCE: SEBI adverse order on record]")
        else:
            metric_scores["governance_basics"] = False
            failed_metrics.append("governance_basics")
            data_flags.append("[DATA UNVERIFIED: promoter_pledging — governance basics check incomplete]")

        # ------------------------------------------------------------------
        # G8 — Promoter holding not collapsing
        # Significant recent selling (> 10pp drop in 2Y) is a conviction killer.
        # ------------------------------------------------------------------
        holding_trend = gm.promoter_holding_trend_5y
        current_holding = g.promoter_holding_pct if g else None

        if holding_trend == "declining" and current_holding is not None and current_holding < 30.0:
            metric_scores["promoter_conviction"] = False
            failed_metrics.append("promoter_conviction")
            data_flags.append(
                f"[GROWTH CONCERN: promoter holding declining (now {current_holding:.1f}%) — "
                "promoter may be exiting; investigate reason for stake reduction]"
            )
        elif holding_trend in ("increasing", "stable") or holding_trend is None:
            metric_scores["promoter_conviction"] = True
            if holding_trend is None:
                conditional_exceptions.append("[PROMOTER TREND UNKNOWN: 5Y history not available]")
        else:
            metric_scores["promoter_conviction"] = True  # declining but holding still adequate

        # ------------------------------------------------------------------
        # G9 — Liquidity: avg daily traded value ≥ ₹2 Cr
        # Lower bar than value (₹5 Cr) to accommodate small/mid growth cos.
        # Below ₹2 Cr = genuinely illiquid; position impact too high.
        # ------------------------------------------------------------------
        avg_val = q.avg_daily_value_cr if q else None
        if avg_val is not None:
            passed_g9 = avg_val >= 2.0
            metric_scores["liquidity >= 2cr"] = passed_g9
            if not passed_g9:
                failed_metrics.append("liquidity >= 2cr")
                data_flags.append(
                    f"[EC-11: LOW LIQUIDITY — avg daily traded value ₹{avg_val:.1f} Cr "
                    "< ₹2 Cr threshold; position sizing and exit may be difficult]"
                )
        else:
            metric_scores["liquidity >= 2cr"] = True
            conditional_exceptions.append("[LIQUIDITY UNKNOWN: avg_daily_value unavailable]")

        score = sum(metric_scores.values())

        # Gate: 7+ green = PASS_GREEN, 6 = PASS_CONDITIONAL, <6 = FAIL
        if score >= 7:
            gate = GateResult.PASS_GREEN
        elif score >= 6:
            gate = GateResult.PASS_CONDITIONAL
            conditional_exceptions.append(
                f"Growth pre-screen score {score}/9 — conditional pass; "
                "additional scrutiny required in Steps 3G and 5G"
            )
        else:
            gate = GateResult.FAIL

        result = PreScreenResult(
            score=score,
            max_score=9,
            gate=gate,
            metric_scores=metric_scores,
            failed_metrics=failed_metrics,
            conditional_exceptions=conditional_exceptions,
            data_flags=data_flags,
        )
        state.pre_screen = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            score=score,
            max_score=9,
            sector=state.sector_name,
            failed_metrics=failed_metrics,
        )

        if gate == GateResult.FAIL:
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Growth pre-screen FAILED: score {score}/9, "
                f"failed: {', '.join(failed_metrics)}"
            )
            state.recommendation_type = "GROWTH_REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )

        return state
