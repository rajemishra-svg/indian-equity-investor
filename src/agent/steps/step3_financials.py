"""Step 3 — Financial Strength & Consistency Gate (deterministic hard gate).

All threshold overrides are driven by the company's ``SectorProfile`` instead
of ad-hoc keyword matching.  This makes it trivial to add new sectors without
touching this file — just add a profile to ``src/sector/profiles.py``.
"""
from __future__ import annotations

from typing import Optional

import anthropic

from src.agent.steps.base import BaseStep
from src.models import AnalysisState, FinancialGateResult, FinancialMetrics, GateResult
from src.sector.profiles import SectorProfile, get_sector_profile


# ---------------------------------------------------------------------------
# Soft quality checks — non-scoring diagnostics
# ---------------------------------------------------------------------------


def _soft_quality_checks(
    f: FinancialMetrics,
    data_flags: list[str],
    concerns: list[str],
) -> None:
    """Additional non-scoring quality checks that add flags/concerns.

    These do not affect the hurdle score or gate determination, but
    populate the data_flags and concerns lists for analyst review.
    """
    # 3Y CAGR deceleration: a company slowing down significantly is a yellow flag
    if f.revenue_cagr_5y is not None and f.revenue_cagr_3y is not None:
        decel = f.revenue_cagr_5y - f.revenue_cagr_3y
        if decel > 8:
            concerns.append(
                f"Revenue deceleration: 5Y CAGR {f.revenue_cagr_5y:.1f}% vs "
                f"3Y CAGR {f.revenue_cagr_3y:.1f}% (Δ {decel:.1f}pp) — investigate cause."
            )
    elif f.revenue_cagr_3y is None:
        data_flags.append("[DATA UNVERIFIED: revenue_cagr_3y — deceleration check skipped]")

    if f.pat_cagr_5y is not None and f.pat_cagr_3y is not None:
        decel = f.pat_cagr_5y - f.pat_cagr_3y
        if decel > 10:
            concerns.append(
                f"PAT deceleration: 5Y CAGR {f.pat_cagr_5y:.1f}% vs "
                f"3Y CAGR {f.pat_cagr_3y:.1f}% (Δ {decel:.1f}pp) — margin compression or base effect."
            )

    # EBITDA margin: below 10% is structurally thin for most non-financial sectors
    if f.ebitda_margin_latest is not None:
        if f.ebitda_margin_latest < 8:
            flag = (
                f"[WATCH: EBITDA margin very thin at {f.ebitda_margin_latest:.1f}% — "
                "sector benchmark check required]"
            )
            data_flags.append(flag)
            concerns.append(
                f"EBITDA margin very thin at {f.ebitda_margin_latest:.1f}% — "
                "limited operating leverage; check sector benchmarks."
            )
        elif f.ebitda_margin_latest < 10:
            data_flags.append(
                f"[WATCH: EBITDA margin {f.ebitda_margin_latest:.1f}% below 10% threshold]"
            )
    else:
        data_flags.append("[DATA UNVERIFIED: ebitda_margin — sector benchmark check skipped]")

    # ICR data quality: None when company has debt = data gap, not debt-free
    if f.interest_coverage is None and f.debt_to_equity is not None and f.debt_to_equity > 0.1:
        data_flags.append(
            "[DATA UNVERIFIED: interest_coverage — company has debt but ICR not available; "
            "treated as passing hurdle but verify manually]"
        )


class Step3Financials(BaseStep):
    """Financial strength and consistency gate — deterministic, hard rejection."""

    step_number = 3
    step_name = "Financial Strength & Consistency"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Evaluate 7 minimum hurdles and hard triggers against FinancialMetrics."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        f = state.financials
        data_flags: list[str] = []
        concerns: list[str] = []
        sector_overrides: list[str] = []

        if f is None:
            result = FinancialGateResult(
                score=0,
                gate=GateResult.FAIL,
                hard_triggers_fired=["NO_FINANCIAL_DATA"],
                hurdles_met={},
                data_flags=["[DATA UNVERIFIED: all_financials]"],
            )
            state.financial_gate = result
            state.terminated_at_step = self.step_number
            state.termination_reason = "Financial data unavailable — cannot evaluate Step 3 gate"
            state.recommendation_type = "REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )
            return state

        # Load the sector profile.  If Step 2 has already run (moat narrative
        # available) we can refine the sector classification.
        from src.sector.classifier import classify_sector  # local import avoids circularity

        if not state.sector_name:
            moat_narrative = state.moat.moat_narrative if state.moat else ""
            state.sector_name = classify_sector(
                company_name=state.company_name or "",
                ticker=state.ticker,
                moat_narrative=moat_narrative,
            )

        profile = get_sector_profile(state.sector_name)

        # Log sector note if not already added
        if profile.sector_override_note and not any(
            state.sector_name in flag for flag in state.all_data_flags
        ):
            state.add_flag(f"[SECTOR: {state.sector_name} — {profile.sector_override_note}]")

        # ------------------------------------------------------------------
        # Helper to evaluate a single hurdle with profile threshold
        # ------------------------------------------------------------------
        def _eval_hurdle(
            hurdle_name: str,
            value: Optional[float],
            threshold: Optional[float],
            op: str = ">=",
        ) -> bool:
            """Return True if hurdle passes (including when waived by sector profile)."""
            if threshold is None:
                sector_overrides.append(
                    f"[SECTOR OVERRIDE: {hurdle_name} waived — {profile.name}]"
                )
                return True
            if value is None:
                data_flags.append(f"[DATA UNVERIFIED: {hurdle_name}]")
                return False
            return (value >= threshold) if op == ">=" else (value < threshold)

        # ------------------------------------------------------------------
        # 7 minimum hurdles
        # ------------------------------------------------------------------
        hurdles_met: dict[str, bool] = {}

        hurdles_met["revenue_cagr_5y >= 12"] = _eval_hurdle(
            "revenue_cagr_5y >= 12", f.revenue_cagr_5y, profile.min_revenue_cagr_5y
        )
        hurdles_met["pat_cagr_5y >= 15"] = _eval_hurdle(
            "pat_cagr_5y >= 15", f.pat_cagr_5y, profile.min_pat_cagr_5y
        )
        hurdles_met["roe_5y_avg >= 15"] = _eval_hurdle(
            "roe_5y_avg >= 15", f.roe_5y_avg, profile.min_roe_5y_avg
        )
        hurdles_met["roce_5y_avg >= 18"] = _eval_hurdle(
            "roce_5y_avg >= 18", f.roce_5y_avg, profile.min_roce_5y_avg
        )
        hurdles_met["cfo_net_profit_3y_avg >= 80"] = _eval_hurdle(
            "cfo_net_profit_3y_avg >= 80", f.cfo_net_profit_3y_avg, profile.min_cfo_np_pct_s3
        )
        hurdles_met["debt_to_equity < 1.0"] = _eval_hurdle(
            "debt_to_equity < 1.0", f.debt_to_equity, profile.max_de_ratio, op="<"
        )

        # Interest coverage: None means debt-free → passes unless sector waives
        if profile.min_icr is None:
            hurdles_met["interest_coverage > 6"] = True
            sector_overrides.append(
                f"[SECTOR OVERRIDE: interest_coverage > 6 waived — {profile.name}]"
            )
        else:
            # None = debt-free → passes; explicit check against sector threshold
            if f.interest_coverage is None:
                hurdles_met["interest_coverage > 6"] = True  # treat as debt-free
            else:
                hurdles_met["interest_coverage > 6"] = f.interest_coverage > profile.min_icr

        score = sum(hurdles_met.values())

        # ------------------------------------------------------------------
        # Hard triggers — sector profile controls which apply and at what level
        # ------------------------------------------------------------------
        hard_triggers_fired: list[str] = []

        # CFO/NP
        if (
            profile.hard_trigger_cfo_np_min is not None
            and f.cfo_net_profit_3y_avg is not None
            and f.cfo_net_profit_3y_avg < profile.hard_trigger_cfo_np_min
        ):
            hard_triggers_fired.append(
                f"CFO/Net Profit 3Y avg < {profile.hard_trigger_cfo_np_min:.0f}% "
                f"({f.cfo_net_profit_3y_avg:.1f}%)"
            )
        elif profile.hard_trigger_cfo_np_min is None and f.cfo_net_profit_3y_avg is not None:
            # Explicitly waived
            sector_overrides.append(
                f"[SECTOR OVERRIDE: CFO/NP hard trigger waived — {profile.name}]"
            )

        # D/E
        if (
            profile.hard_trigger_de_max is not None
            and f.debt_to_equity is not None
            and f.debt_to_equity > profile.hard_trigger_de_max
        ):
            hard_triggers_fired.append(
                f"Debt/Equity > {profile.hard_trigger_de_max:.1f} ({f.debt_to_equity:.2f}x)"
            )
        elif profile.hard_trigger_de_max is None and f.debt_to_equity is not None:
            sector_overrides.append(
                f"[SECTOR OVERRIDE: D/E hard trigger waived — {profile.name}]"
            )

        # Interest coverage
        if (
            profile.hard_trigger_icr_min is not None
            and f.interest_coverage is not None
            and f.interest_coverage < profile.hard_trigger_icr_min
        ):
            hard_triggers_fired.append(
                f"Interest coverage < {profile.hard_trigger_icr_min:.0f}x ({f.interest_coverage:.1f}x)"
            )
        elif profile.hard_trigger_icr_min is None and f.interest_coverage is not None:
            sector_overrides.append(
                f"[SECTOR OVERRIDE: ICR hard trigger waived — {profile.name}]"
            )

        # Carry forward data flags from FinancialMetrics itself
        data_flags.extend(f.data_flags)

        # Soft quality checks
        _soft_quality_checks(f, data_flags, concerns)

        # Gate determination
        if hard_triggers_fired:
            gate = GateResult.FAIL
        elif score == 7:
            gate = GateResult.PASS_GREEN
        elif score >= 5:
            gate = GateResult.PASS_CONDITIONAL
        else:
            gate = GateResult.FAIL

        result = FinancialGateResult(
            score=score,
            gate=gate,
            hard_triggers_fired=hard_triggers_fired,
            hurdles_met=hurdles_met,
            sector_overrides=sector_overrides,
            data_flags=data_flags,
        )
        state.financial_gate = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            score=score,
            max_score=7,
            sector=state.sector_name,
            hard_triggers=hard_triggers_fired,
            hurdles_met=hurdles_met,
            sector_overrides=sector_overrides,
            concerns=concerns,
        )

        for flag in data_flags:
            state.add_flag(flag)
        for override in sector_overrides:
            state.add_flag(override)

        if gate == GateResult.FAIL:
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Financials FAILED: score {score}/7, "
                f"triggers={hard_triggers_fired}"
            )
            state.recommendation_type = "REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )

        return state
