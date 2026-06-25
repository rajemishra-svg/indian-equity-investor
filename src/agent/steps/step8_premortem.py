"""Step 8 — Premortem Risk Analysis (Haiku single-call, no agentic loop).

Cost optimisation: by Step 8, the pipeline has already gathered moat type, tailwind
classification, financial gate results, governance scores, and valuation data.
All risk-relevant context is in state — no web searches are needed.  We pass the
full context to Haiku in a single call for a ~5× cost reduction vs a Sonnet
agentic loop.
"""
from __future__ import annotations

import json

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, PremortRisk


class Step8Premortem(BaseStep):
    """Premortem — synthesise risks from existing state using a single Haiku call."""

    step_number = 8
    step_name = "Premortem Risk Analysis"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Synthesise premortem risks from pipeline context. No web tools needed."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        system = self._build_system_prompt(state)
        message = self._build_message(state)

        try:
            # Single Haiku call — all context is already in state from prior steps.
            response_text = await self._call_claude(
                system=system,
                messages=[{"role": "user", "content": message}],
                model=settings.model_light,
                max_tokens=settings.max_tokens_short,
            )
            risk = self._parse_response(response_text, state)
        except Exception as exc:
            self.log.warning(
                "premortem_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            state.add_error(f"STEP_{self.step_number}_ERROR")
            risk = PremortRisk(
                primary_risk="[NOT AVAILABLE]",
                secondary_risk="[NOT AVAILABLE]",
                tertiary_risk="[NOT AVAILABLE]",
                risk_type="CYCLICAL_MANAGEABLE",
                proceed=True,  # default: proceed with caution
                data_flags=["[DATA UNVERIFIED: premortem_assessment]"],
            )

        state.premortem = risk
        for flag in risk.data_flags:
            state.add_flag(flag)

        self.log.info(
            "premortem_complete",
            ticker=state.ticker,
            proceed=risk.proceed,
            risk_type=risk.risk_type,
            primary_risk=risk.primary_risk,
        )

        if not risk.proceed:
            state.recommendation_type = "REJECT"
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Premortem: STRUCTURAL_UNHEDGEABLE risk identified — "
                f"{risk.primary_risk}"
            )
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )

        return state

    def _build_system_prompt(self, state: AnalysisState | None = None) -> str:
        is_growth = state is not None and state.analysis_mode == "growth"

        base_categories = (
            "Categories to evaluate from the context:\n"
            "1. Business model disruption (check moat durability)\n"
            "2. Regulatory / policy reversal (check tailwind type)\n"
            "3. Promoter / governance failure (check governance flags)\n"
            "4. Balance sheet deterioration (check D/E, interest coverage)\n"
            "5. Competitive displacement (check moat and headwinds)\n"
            "6. Macro / currency / sector collapse (check cycle position)"
        )

        growth_categories = (
            "GROWTH MODE — evaluate growth-specific risks first:\n"
            "1. FAILS TO SCALE: unit economics don't hold at 5× current revenue — "
            "gross margin compressing, CAC rising, ROIIC falling\n"
            "2. COMPETITIVE DISRUPTION: better-funded player enters the TAM; "
            "company's growth rate decelerates sharply before reaching scale\n"
            "3. RE-RATING RISK: if growth slows even 5pp below expectations, "
            "the high P/E multiple compresses 40-60% even if business is fine\n"
            "4. PROMOTER DILUTION: equity raises erode per-share returns "
            "even as headline revenue grows\n"
            "5. TAM MIRAGE: addressable market is smaller than estimated; "
            "penetration ceiling hit earlier than modelled\n"
            "6. CASH RUNWAY: if FCF-negative, next equity raise may happen at "
            "distressed terms or dilute existing holders heavily"
        )

        categories = growth_categories if is_growth else base_categories
        question = (
            "\nPerform a premortem: if this growth stock falls 60% in 2–3 years "
            "(growth stocks de-rate sharply), what are the three most likely causes? "
            "Return the JSON."
            if is_growth else
            "\nPerform a premortem: if this stock falls 50% in 2–3 years, "
            "what are the three most likely causes based on the above context? "
            "Return the JSON."
        )

        return (
            "You are a risk analyst specialising in Indian equities. "
            "Perform a premortem analysis using ONLY the context provided — do not search the web.\n\n"
            "RISK CLASSIFICATION:\n"
            "- CYCLICAL_MANAGEABLE: temporary downturn, business intact, proceed=true\n"
            "- STRUCTURAL_UNHEDGEABLE: permanent impairment, proceed=false\n\n"
            + categories
            + "\n\nRULES:\n"
            "1. Use ONLY the provided context. No web searches.\n"
            "2. NEVER fabricate risks not supported by the evidence.\n"
            "3. Return ONLY valid JSON:\n"
            "{\n"
            '  "primary_risk": "<description>",\n'
            '  "secondary_risk": "<description>",\n'
            '  "tertiary_risk": "<description>",\n'
            '  "risk_type": "<CYCLICAL_MANAGEABLE|STRUCTURAL_UNHEDGEABLE>",\n'
            '  "proceed": <true|false>,\n'
            '  "data_flags": [<list of [DATA UNVERIFIED] flags>]\n'
            "}"
            + f"\n\n{question}"
        )

    def _build_message(self, state: AnalysisState) -> str:
        """Assemble full pipeline context so Haiku can reason without web tools."""
        parts = [f"Company: {state.ticker}"]
        if state.company_name:
            parts.append(f"Name: {state.company_name}")

        # Governance signals
        if state.governance:
            g = state.governance
            parts.append(
                f"Governance: score={g.score}/15, gate={g.gate.value}, "
                f"immediate_triggers={g.immediate_triggers}, concerns={g.concerns}"
            )
        if state.governance_data:
            gd = state.governance_data
            parts.append(
                f"Pledging: {gd.promoter_pledging_pct}%, "
                f"trend={gd.pledging_trend_direction}"
            )

        # Moat signals — use short narrative to save tokens (full narrative not needed for risk analysis)
        if state.moat:
            parts.append(f"Moat: {state.moat.moat_type.value} ({state.moat.moat_durability})")
            moat_ctx = state.moat.moat_narrative_short or state.moat.moat_narrative
            parts.append(f"Business: {moat_ctx}")
            parts.append(f"Working capital: {state.moat.working_capital_flag}")

        # Tailwind / sector signals
        if state.tailwind:
            tw = state.tailwind
            parts.append(f"Sector: {tw.sector}, tailwind={tw.tailwind_type.value}, cycle={tw.cycle_position.value}")
            if tw.headwind_flags:
                parts.append(f"Known headwinds: {', '.join(tw.headwind_flags)}")

        # Financial risk signals
        if state.financials:
            f = state.financials
            parts.append(
                f"Balance sheet: D/E={f.debt_to_equity}, ICR={f.interest_coverage}, "
                f"CFO/NP={f.cfo_net_profit_3y_avg}%"
            )
        if state.financial_gate:
            parts.append(
                f"Financial gate: {state.financial_gate.gate.value}, "
                f"hard triggers={state.financial_gate.hard_triggers_fired}"
            )

        # Valuation context
        if state.valuation:
            v = state.valuation
            parts.append(
                f"Valuation: {v.methods_in_buy_zone}/4 methods in buy zone, "
                f"MoS={v.margin_of_safety_pct}%"
            )

        # Growth mode: add growth-specific context
        if state.analysis_mode == "growth" and state.growth_metrics:
            gm = state.growth_metrics
            parts.append(
                f"Growth metrics: rev CAGR 3Y={getattr(state.financials, 'revenue_cagr_3y', None)}%, "
                f"gross margin trend={gm.gross_margin_trend}, "
                f"Rule of 40={gm.rule_of_40_score}, "
                f"cash runway={gm.cash_runway_months} months, "
                f"ROIIC={gm.roiic_3y or gm.roiic_proxy_cfo_revenue}%, "
                f"TAM penetration={gm.tam_penetration_est_pct}%"
            )
            if state.multibagger_score:
                ms = state.multibagger_score
                parts.append(
                    f"Multibagger score: {ms.total_score}/10 ({ms.verdict}); "
                    f"valuation gap={ms.valuation_gap_score}/3"
                )

        parts.append(
            "\nReturn the JSON with your premortem risk assessment."
        )
        return "\n".join(parts)

    def _parse_response(
        self, response_text: str, state: AnalysisState
    ) -> PremortRisk:
        try:
            data = self._parse_json_response(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.warning(
                "premortem_json_parse_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            return PremortRisk(
                primary_risk=response_text[:200],
                secondary_risk="[NOT AVAILABLE]",
                tertiary_risk="[NOT AVAILABLE]",
                risk_type="CYCLICAL_MANAGEABLE",
                proceed=True,
                data_flags=["[DATA UNVERIFIED: premortem_json_parse_failed]"],
            )

        risk_type = data.get("risk_type", "CYCLICAL_MANAGEABLE").upper()
        if risk_type not in ("CYCLICAL_MANAGEABLE", "STRUCTURAL_UNHEDGEABLE"):
            risk_type = "CYCLICAL_MANAGEABLE"

        return PremortRisk(
            primary_risk=data.get("primary_risk", "[NOT AVAILABLE]"),
            secondary_risk=data.get("secondary_risk", "[NOT AVAILABLE]"),
            tertiary_risk=data.get("tertiary_risk", "[NOT AVAILABLE]"),
            risk_type=risk_type,
            proceed=bool(data.get("proceed", True)),
            data_flags=data.get("data_flags", []),
        )
