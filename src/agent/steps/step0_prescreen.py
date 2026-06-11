"""Step 0 — Quantitative Pre-Screen (deterministic, no Claude needed)."""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.models import AnalysisState, GateResult, PreScreenResult
from src.sector.classifier import classify_sector
from src.sector.profiles import get_sector_profile


class Step0PreScreen(BaseStep):
    """Quantitative pre-screen — all deterministic, no LLM call."""

    step_number = 0
    step_name = "Quantitative Pre-Screen"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Score all quantitative metrics with sector-aware thresholds and set gate result."""
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

        # Classify sector if not already set by pipeline prefetch
        if not state.sector_name:
            state.sector_name = classify_sector(
                company_name=state.company_name or "",
                ticker=state.ticker,
            )

        profile = get_sector_profile(state.sector_name)

        if profile.sector_override_note:
            state.add_flag(f"[SECTOR: {state.sector_name} — {profile.sector_override_note}]")

        metric_scores: dict[str, bool] = {}
        failed_metrics: list[str] = []
        data_flags: list[str] = []
        conditional_exceptions: list[str] = []

        # ------------------------------------------------------------------
        # Helper: evaluate one metric with a sector-aware threshold.
        # ``threshold=None`` means the check is waived for this sector.
        # ------------------------------------------------------------------

        def _eval(
            metric_name: str,
            value: float | None,
            threshold: float | None,
            op: str = ">=",
        ) -> None:
            """Record pass/fail for one metric into the shared dicts."""
            # Sector profile waives this metric
            if threshold is None:
                metric_scores[metric_name] = True
                conditional_exceptions.append(
                    f"[SECTOR OVERRIDE: {metric_name} waived — {profile.name}]"
                )
                return

            if value is None:
                metric_scores[metric_name] = False
                failed_metrics.append(metric_name)
                flag = f"[DATA UNVERIFIED: {metric_name}]"
                data_flags.append(flag)
                state.add_flag(flag)
                return

            passed = (value >= threshold) if op == ">=" else (value < threshold)
            metric_scores[metric_name] = passed
            if not passed:
                failed_metrics.append(metric_name)

        # ------------------------------------------------------------------
        # 1. Market cap — absolute minimum, no sector override
        # ------------------------------------------------------------------
        mc = (q.market_cap_cr if q else None) or (
            f.market_cap_cr if f and f.market_cap_cr is not None else None
        )
        passed_mc = mc is not None and mc >= 2000
        metric_scores["market_cap_cr >= 2000"] = passed_mc
        if not passed_mc:
            failed_metrics.append("market_cap_cr >= 2000")

        # ------------------------------------------------------------------
        # 2–7. Sector-aware financial metrics
        # ------------------------------------------------------------------
        _eval(
            "revenue_cagr_5y >= 12",
            f.revenue_cagr_5y if f else None,
            profile.min_revenue_cagr_5y,
        )
        _eval(
            "pat_cagr_5y >= 15",
            f.pat_cagr_5y if f else None,
            profile.min_pat_cagr_5y,
        )
        _eval(
            "roe_5y_avg >= 15",
            f.roe_5y_avg if f else None,
            profile.min_roe_5y_avg,
        )
        _eval(
            "roce_5y_avg >= 18",
            f.roce_5y_avg if f else None,
            profile.min_roce_5y_avg,
        )
        _eval(
            "debt_to_equity < 1.0",
            f.debt_to_equity if f else None,
            profile.max_de_ratio,
            op="<",
        )
        _eval(
            "cfo_net_profit_3y_avg >= 70",
            f.cfo_net_profit_3y_avg if f else None,
            profile.min_cfo_np_pct,
        )

        # ------------------------------------------------------------------
        # 8. Promoter holding — waived for MNCs and professionally-managed companies
        #    (EC-06).  For such companies the controlling shareholder (foreign parent,
        #    diversified institutional base) holds via FPI/FDI routes and is NOT
        #    classified as "promoter" in Indian filings, so a low promoter holding %
        #    is structurally expected and should NOT penalise the screen.
        # ------------------------------------------------------------------
        promoter_holding_threshold = profile.min_promoter_holding
        if g is not None and g.is_mnc:
            promoter_holding_threshold = None  # waive entirely
            mnc_flag = (
                "[EC-06: MNC/professionally-managed — promoter holding >= 40% gate waived; "
                "verify controlling shareholder structure before investing]"
            )
            conditional_exceptions.append(mnc_flag)
            state.add_flag(mnc_flag)

        _eval(
            "promoter_holding >= 40",
            g.promoter_holding_pct if g else None,
            promoter_holding_threshold,
        )

        # ------------------------------------------------------------------
        # 9. Pledging — always checked; immediate trigger fires regardless of sector
        # ------------------------------------------------------------------
        pledging = g.promoter_pledging_pct if g else None
        if pledging is None:
            metric_scores["promoter_pledging < 10"] = False
            failed_metrics.append("promoter_pledging < 10")
            flag = "[DATA UNVERIFIED: promoter_pledging < 10]"
            data_flags.append(flag)
            state.add_flag(flag)
        else:
            passed_pl = pledging < 10
            metric_scores["promoter_pledging < 10"] = passed_pl
            if not passed_pl:
                failed_metrics.append("promoter_pledging < 10")

        score = sum(metric_scores.values())

        # ------------------------------------------------------------------
        # EC-11: Liquidity guard (non-scoring, soft flag only)
        # Stocks trading < ₹5 Cr/day on average cannot be entered or exited
        # without meaningful market impact — flag but don't fail the gate.
        # ------------------------------------------------------------------
        avg_val = q.avg_daily_value_cr if q else None
        if avg_val is not None and avg_val < 5.0 and mc is not None and mc < 10_000:
            liq_flag = (
                f"[EC-11: LOW LIQUIDITY — avg daily traded value ₹{avg_val:.1f} Cr "
                f"< ₹5 Cr threshold; position sizing must be ≤ 1-day-volume × 10%; "
                "spread cost may erode returns for small positions]"
            )
            data_flags.append(liq_flag)
            state.add_flag(liq_flag)

        # Gate determination
        if score >= 7:
            gate = GateResult.PASS_GREEN
        elif score >= 5:
            gate = GateResult.PASS_CONDITIONAL
            conditional_exceptions.append(
                f"Pre-screen score {score}/9 — conditional pass; "
                "additional scrutiny required in Steps 1–3"
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
            data_flags=data_flags,
        )

        if gate == GateResult.FAIL:
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Pre-screen FAILED: score {score}/9, "
                f"failed metrics: {', '.join(failed_metrics)}"
            )
            state.recommendation_type = "REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )

        return state
