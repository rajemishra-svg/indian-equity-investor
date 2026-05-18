"""Step 7 — Peer Benchmarking (Claude-powered with tools)."""
from __future__ import annotations

import json

import anthropic

from src.agent.steps.base import BaseStep
from src.agent.tools import TOOLS
from src.config import settings
from src.models import AnalysisState, GateResult, PeerComparisonResult, PeerData


class Step7Peers(BaseStep):
    """Peer benchmarking — identify 3–5 peers and compare quality vs valuation."""

    step_number = 7
    step_name = "Peer Benchmarking"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Ask Claude to fetch peer data and compare."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        system = self._build_system_prompt()
        initial_message = self._build_initial_message(state)

        try:
            response_text = await self._agentic_loop(
                system=system,
                initial_message=initial_message,
                tools=TOOLS,
                model=settings.model_heavy,
                max_tokens=settings.max_tokens,
                max_iterations=8,
            )
            peer_result = self._parse_response(response_text, state)
        except Exception as exc:
            self.log.warning(
                "peer_comparison_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            state.add_error(f"STEP_{self.step_number}_ERROR")
            peer_result = PeerComparisonResult(
                gate=GateResult.PASS_CONDITIONAL,
                peer_count=0,
                data_flags=["[DATA UNVERIFIED: peer_comparison]"],
            )

        state.peer_comparison = peer_result
        for flag in peer_result.data_flags:
            state.add_flag(flag)

        self.log.info(
            "peer_comparison_done",
            ticker=state.ticker,
            gate=peer_result.gate.value,
            peer_count=peer_result.peer_count,
            dominant_peer=peer_result.dominant_peer,
            quality_rank=peer_result.target_quality_rank,
        )

        # Peer dominance — set recommendation to PEER_SWITCH
        if peer_result.gate == GateResult.FAIL and peer_result.dominant_peer:
            state.recommendation_type = "PEER_SWITCH"
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Peer dominance detected: {peer_result.dominant_peer} "
                "offers higher quality at lower valuation"
            )
            self.log.info(
                "pipeline_peer_switch",
                step=self.step_number,
                ticker=state.ticker,
                dominant_peer=peer_result.dominant_peer,
            )

        return state

    def _build_system_prompt(self) -> str:
        return (
            "You are an Indian equity research analyst. "
            "Your task: identify 3–5 direct listed peers on NSE/BSE for a given company "
            "and compare their financial quality and valuation.\n\n"
            "RULES:\n"
            "1. Use get_financial_data and web_search to fetch peer metrics.\n"
            "2. Score quality (0–10) and valuation (0–10, lower P/E = higher score) for each peer.\n"
            "3. A peer DOMINATES the target if it has HIGHER quality score AND LOWER valuation "
            "score (i.e. cheaper). If dominant peer exists, set gate to FAIL.\n"
            "4. Return ONLY valid JSON — no markdown code blocks.\n\n"
            "JSON schema:\n"
            "{\n"
            '  "gate": "<pass_green|pass_conditional|fail>",\n'
            '  "target_quality_rank": <int — 1=best>,\n'
            '  "target_valuation_rank": <int — 1=cheapest>,\n'
            '  "peer_count": <int>,\n'
            '  "dominant_peer": "<ticker or null>",\n'
            '  "peers": [\n'
            "    {\n"
            '      "ticker": "<str>",\n'
            '      "name": "<str>",\n'
            '      "revenue_cagr_5y": <float or null>,\n'
            '      "pat_cagr_5y": <float or null>,\n'
            '      "ebitda_margin": <float or null>,\n'
            '      "roe_5y_avg": <float or null>,\n'
            '      "roce_5y_avg": <float or null>,\n'
            '      "debt_to_equity": <float or null>,\n'
            '      "forward_pe": <float or null>,\n'
            '      "ev_ebitda_forward": <float or null>,\n'
            '      "promoter_holding": <float or null>,\n'
            '      "pledging_pct": <float or null>\n'
            "    }\n"
            "  ],\n"
            '  "data_flags": [<list of [DATA UNVERIFIED] flags>]\n'
            "}"
        )

    def _build_initial_message(self, state: AnalysisState) -> str:
        context = [
            f"Target company: {state.ticker}",
        ]
        if state.company_name:
            context.append(f"Name: {state.company_name}")
        if state.moat:
            context.append(f"Sector: {state.tailwind.sector if state.tailwind else 'unknown'}")
            context.append(f"Market position: {state.moat.market_position}")
        if state.financials:
            f = state.financials
            context.append(
                f"Target metrics: ROE={f.roe_5y_avg}%, ROCE={f.roce_5y_avg}%, "
                f"Revenue CAGR 5Y={f.revenue_cagr_5y}%"
            )
        if state.valuation_data and state.valuation_data.pe_current:
            context.append(f"Target P/E: {state.valuation_data.pe_current}x")

        return "\n".join(context) + (
            "\n\nIdentify 3–5 direct peers and fetch their financial metrics "
            "using get_financial_data and web_search. Compare quality and valuation. "
            "Return the structured JSON."
        )

    def _parse_response(
        self, response_text: str, state: AnalysisState
    ) -> PeerComparisonResult:
        try:
            data = self._parse_json_response(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.warning(
                "peer_json_parse_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            return PeerComparisonResult(
                gate=GateResult.PASS_CONDITIONAL,
                peer_count=0,
                data_flags=["[DATA UNVERIFIED: peer_json_parse_failed]"],
            )

        raw_gate = data.get("gate", "pass_conditional").lower().replace(" ", "_")
        try:
            gate = GateResult(raw_gate)
        except ValueError:
            gate = GateResult.PASS_CONDITIONAL

        peers: list[PeerData] = []
        for p in data.get("peers", []):
            try:
                peers.append(PeerData(**p))
            except Exception:
                pass

        return PeerComparisonResult(
            gate=gate,
            target_quality_rank=data.get("target_quality_rank"),
            target_valuation_rank=data.get("target_valuation_rank"),
            peer_count=data.get("peer_count", len(peers)),
            peers=peers,
            dominant_peer=data.get("dominant_peer"),
            data_flags=data.get("data_flags", []),
        )
