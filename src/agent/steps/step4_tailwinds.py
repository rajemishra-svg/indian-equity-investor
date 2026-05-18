"""Step 4 — Industry & Structural Tailwinds (Haiku single-call, no agentic loop).

Cost optimisation: Step 2 moat analysis already surfaced the sector and competitive
context. We pass that rich context directly to Haiku so it can classify tailwind type,
cycle position and growth runway without needing to call any web tools.
"""
from __future__ import annotations

import json

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import (
    AnalysisState,
    CyclePosition,
    TailwindAssessment,
    TailwindType,
)


class Step4Tailwinds(BaseStep):
    """Sector tailwind classification — single Haiku call using Step 2 context."""

    step_number = 4
    step_name = "Industry & Structural Tailwinds"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Classify tailwind type using existing context. No web tools required."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        system = self._build_system_prompt()
        message = self._build_message(state)

        try:
            # Single Haiku call — no agentic loop; all required context is in state.
            response_text = await self._call_claude(
                system=system,
                messages=[{"role": "user", "content": message}],
                model=settings.model_light,
                max_tokens=settings.max_tokens_short,
            )
            assessment = self._parse_response(response_text, state)
        except Exception as exc:
            self.log.warning(
                "tailwind_assessment_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            state.add_error(f"STEP_{self.step_number}_ERROR")
            assessment = TailwindAssessment(
                sector="[NOT AVAILABLE]",
                tailwind_type=TailwindType.STRUCTURAL,
                cycle_position=CyclePosition.MID,
                growth_runway_years="[NOT AVAILABLE]",
                headwind_flags=["[DATA UNVERIFIED: tailwind_assessment]"],
                tailwind_narrative="[DATA UNVERIFIED — tailwind assessment could not be completed]",
                data_flags=["[DATA UNVERIFIED: tailwind_assessment]"],
            )

        state.tailwind = assessment
        for flag in assessment.data_flags:
            state.add_flag(flag)

        self.log.info(
            "tailwind_assessed",
            ticker=state.ticker,
            sector=assessment.sector,
            tailwind_type=assessment.tailwind_type.value,
            cycle_position=assessment.cycle_position.value,
        )
        return state

    def _build_system_prompt(self) -> str:
        return (
            "You are a senior Indian macro and sector analyst. "
            "Classify the structural tailwinds for an Indian listed company based on the "
            "context provided. Do NOT call any tools — use only the data given.\n\n"
            "RULES:\n"
            "1. Use ONLY the context provided. Do not search or fetch any URLs.\n"
            "2. NEVER hallucinate growth forecasts. Use [NOT AVAILABLE] if data is missing.\n"
            "3. Return ONLY valid JSON — no markdown code blocks, no text outside JSON.\n\n"
            "JSON schema:\n"
            "{\n"
            '  "sector": "<primary sector name>",\n'
            '  "tailwind_type": "<structural|policy_driven|cyclical>",\n'
            '  "cycle_position": "<early|mid|late>",\n'
            '  "growth_runway_years": "<e.g. 7-10 years or [NOT AVAILABLE]>",\n'
            '  "headwind_flags": [<list of risk strings, may be empty>],\n'
            '  "tailwind_narrative": "<2-3 sentence narrative on sector tailwinds>",\n'
            '  "data_flags": [<list of [DATA UNVERIFIED] flags if any data was missing>]\n'
            "}"
        )

    def _build_message(self, state: AnalysisState) -> str:
        """Build a rich context message so Haiku can answer without web calls."""
        lines = [
            f"Company: {state.ticker}",
        ]
        if state.company_name:
            lines.append(f"Name: {state.company_name}")
        if state.moat:
            lines.append(f"Moat type: {state.moat.moat_type.value}")
            lines.append(f"Market position: {state.moat.market_position}")
            lines.append(f"Moat narrative: {state.moat.moat_narrative}")
        if state.financials:
            f = state.financials
            lines.append(
                f"Financials: Revenue CAGR 5Y={f.revenue_cagr_5y}%, "
                f"EBITDA margin={f.ebitda_margin_latest}%"
            )
        if state.quote:
            lines.append(f"Market cap: ₹{state.quote.market_cap_cr:.0f} Cr")

        lines.append(
            "\nBased solely on this context, classify the sector tailwind "
            "and return the JSON. No web searches needed."
        )
        return "\n".join(lines)

    def _parse_response(self, response_text: str, state: AnalysisState) -> TailwindAssessment:
        try:
            data = self._parse_json_response(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.warning(
                "tailwind_json_parse_failed",
                ticker=state.ticker,
                error=str(exc),
                raw=response_text[:200],
            )
            return TailwindAssessment(
                sector="[NOT AVAILABLE]",
                tailwind_type=TailwindType.STRUCTURAL,
                cycle_position=CyclePosition.MID,
                growth_runway_years="[NOT AVAILABLE]",
                headwind_flags=[],
                tailwind_narrative=response_text[:300],
                data_flags=["[DATA UNVERIFIED: tailwind_json_parse_failed]"],
            )

        raw_tt = data.get("tailwind_type", "structural").lower()
        try:
            tailwind_type = TailwindType(raw_tt)
        except ValueError:
            tailwind_type = TailwindType.STRUCTURAL

        raw_cp = data.get("cycle_position", "mid").lower()
        try:
            cycle_pos = CyclePosition(raw_cp)
        except ValueError:
            cycle_pos = CyclePosition.MID

        return TailwindAssessment(
            sector=data.get("sector", "[NOT AVAILABLE]"),
            tailwind_type=tailwind_type,
            cycle_position=cycle_pos,
            growth_runway_years=data.get("growth_runway_years", "[NOT AVAILABLE]"),
            headwind_flags=data.get("headwind_flags", []),
            tailwind_narrative=data.get("tailwind_narrative", "[NOT AVAILABLE]"),
            data_flags=data.get("data_flags", []),
        )
