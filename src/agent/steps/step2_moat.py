"""Step 2 — Business Quality & Moat (Claude-powered qualitative analysis)."""
from __future__ import annotations

import json

import anthropic

from src.agent.steps.base import BaseStep
from src.agent.tools import TOOLS
from src.config import settings
from src.models import AnalysisState, MoatAssessment, MoatType


class Step2Moat(BaseStep):
    """Qualitative moat assessment using an agentic Claude loop with web tools."""

    step_number = 2
    step_name = "Business Quality & Moat"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Use Claude with tools to assess moat type and quality."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        system = self._build_system_prompt(state)
        initial_message = self._build_initial_message(state)

        try:
            response_text = await self._agentic_loop(
                system=system,
                initial_message=initial_message,
                tools=TOOLS,
                model=settings.model_heavy,
                max_tokens=settings.max_tokens,
                max_iterations=6,
            )
            moat = self._parse_moat_response(response_text, state)
        except Exception as exc:
            self.log.warning(
                "moat_assessment_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            state.add_error(f"STEP_{self.step_number}_MOAT_ERROR")
            moat = MoatAssessment(
                moat_type=MoatType.NONE,
                moat_durability="Unknown",
                market_position="[NOT AVAILABLE]",
                market_share_trend="[NOT AVAILABLE]",
                working_capital_flag="[NOT AVAILABLE]",
                moat_narrative="[DATA UNVERIFIED — moat assessment could not be completed]",
                data_flags=["[DATA UNVERIFIED: moat_assessment]"],
            )

        state.moat = moat
        for flag in moat.data_flags:
            state.add_flag(flag)

        self.log.info(
            "moat_assessed",
            ticker=state.ticker,
            moat_type=moat.moat_type.value,
            durability=moat.moat_durability,
        )
        return state

    def _build_system_prompt(self, state: AnalysisState) -> str:
        return (
            "You are a senior Indian equity research analyst specialising in "
            "long-term quality investing. Your task is to assess the competitive moat "
            "of an Indian listed company using publicly available information.\n\n"
            "CRITICAL RULES:\n"
            "1. Use the available tools to gather current information before responding.\n"
            "2. NEVER hallucinate financial numbers. If data is not available, use "
            "[NOT AVAILABLE].\n"
            "3. After gathering data, return ONLY a valid JSON object — no markdown "
            "code blocks, no explanation outside the JSON.\n\n"
            "RESEARCH GUIDANCE:\n"
            "• Market position: search '[company] market share rank segment site:screener.in OR site:moneycontrol.com'\n"
            "• Moat sources: search '[company] competitive advantage moat durable'\n"
            "• P3-1 Management quality: search '[company] concall transcript management guidance 2024' "
            "and '[company] investor presentation capital allocation'. Look for: "
            "(a) whether management guidance has been consistently met over 2–3 years, "
            "(b) quality of capital allocation commentary (ROCE targets, acquisition discipline), "
            "(c) transparency on challenges (not just positives).\n\n"
            "JSON schema:\n"
            "{\n"
            '  "moat_type": "<one of: brand|network_effect|cost_leadership|'
            'switching_costs|regulatory|scale|ip_patents|none>",\n'
            '  "moat_durability": "<High|Medium|Low>",\n'
            '  "market_position": "<string: e.g. Rank 1 in X segment>",\n'
            '  "market_share_trend": "<Growing|Stable|Declining>",\n'
            '  "tam_multiple": <float or null — TAM / current revenue>,\n'
            '  "working_capital_flag": "<Clean|Stretched|Deteriorating>",\n'
            '  "moat_narrative": "<2-3 sentence narrative explaining the moat>",\n'
            '  "management_guidance_reliability": "<High|Medium|Low|null — '
            'High = met guidance in 3+ of last 4 quarters, Low = missed guidance repeatedly>",\n'
            '  "concall_quality_note": "<1 sentence on management communication quality, or null>",\n'
            '  "data_flags": [<list of [DATA UNVERIFIED] flags for missing items>]\n'
            "}"
        )

    def _build_initial_message(self, state: AnalysisState) -> str:
        parts = [
            f"Analyse the competitive moat of {state.ticker}",
        ]
        if state.company_name:
            parts.append(f"({state.company_name})")
        parts.append("using the following context:")
        context_lines = []
        if state.quote:
            context_lines.append(f"- CMP: ₹{state.quote.cmp}, Market Cap: ₹{state.quote.market_cap_cr:.0f} Cr")
        if state.financials:
            f = state.financials
            context_lines.append(
                f"- Revenue CAGR 5Y: {f.revenue_cagr_5y}%, EBITDA Margin: {f.ebitda_margin_latest}%"
            )

        message = " ".join(parts)
        if context_lines:
            message += "\n\n" + "\n".join(context_lines)
        message += (
            "\n\nUse web_search and web_fetch to gather information about "
            "market position, competitive dynamics, and working capital trends. "
            "Then return the structured JSON."
        )
        return message

    def _parse_moat_response(self, response_text: str, state: AnalysisState) -> MoatAssessment:
        """Parse Claude's JSON response into MoatAssessment."""
        try:
            data = self._parse_json_response(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.warning(
                "moat_json_parse_failed",
                ticker=state.ticker,
                error=str(exc),
                raw=response_text[:200],
            )
            return MoatAssessment(
                moat_type=MoatType.NONE,
                moat_durability="Unknown",
                market_position="[NOT AVAILABLE]",
                market_share_trend="[NOT AVAILABLE]",
                working_capital_flag="[NOT AVAILABLE]",
                moat_narrative=response_text[:300],
                data_flags=["[DATA UNVERIFIED: moat_json_parse_failed]"],
            )

        # Validate moat_type enum
        raw_moat = data.get("moat_type", "none").lower().replace("-", "_").replace(" ", "_")
        try:
            moat_type = MoatType(raw_moat)
        except ValueError:
            moat_type = MoatType.NONE

        # P3-1: Extract management quality / concall signals
        mgmt_reliability = data.get("management_guidance_reliability")
        if isinstance(mgmt_reliability, str) and mgmt_reliability.lower() in (
            "high", "medium", "low"
        ):
            mgmt_reliability = mgmt_reliability.capitalize()
        else:
            mgmt_reliability = None

        concall_note = data.get("concall_quality_note")
        if not isinstance(concall_note, str) or not concall_note.strip():
            concall_note = None

        return MoatAssessment(
            moat_type=moat_type,
            moat_durability=data.get("moat_durability", "Unknown"),
            market_position=data.get("market_position", "[NOT AVAILABLE]"),
            market_share_trend=data.get("market_share_trend", "[NOT AVAILABLE]"),
            tam_multiple=data.get("tam_multiple"),
            working_capital_flag=data.get("working_capital_flag", "[NOT AVAILABLE]"),
            moat_narrative=data.get("moat_narrative", "[NOT AVAILABLE]"),
            management_guidance_reliability=mgmt_reliability,
            concall_quality_note=concall_note,
            data_flags=data.get("data_flags", []),
        )
