"""Step 7 — Peer Benchmarking (light LLM identification + deterministic comparison).

Cost/quality design: the old implementation ran a Sonnet agentic loop (≤8
iterations) that fetched peer data through tools AND subjectively ranked it.
Peer *identification* is the only part that needs an LLM; metric fetching and
the dominance test are arithmetic.  This version makes one Haiku call to name
3–5 NSE peers, fetches their metrics deterministically (Screener financials +
Yahoo Finance valuation, concurrently), and computes quality/valuation ranks
in Python — cheaper, faster, and auditable.
"""
from __future__ import annotations

import asyncio
import json
import re

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, GateResult, PeerComparisonResult, PeerData

# Official NSE symbols: 1–20 chars, alphanumeric plus & and - (M&M, BAJAJ-AUTO).
_PEER_TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")

# Quality metrics and their direction. Each entity needs ≥2 populated metrics
# to receive a quality rank.
_QUALITY_METRICS: tuple[tuple[str, bool], ...] = (
    ("revenue_cagr_5y", True),   # higher is better
    ("pat_cagr_5y", True),
    ("roe_5y_avg", True),
    ("roce_5y_avg", True),
    ("debt_to_equity", False),   # lower is better
)
_MIN_METRICS_FOR_RANK = 2


def _competition_ranks(scores: list[tuple[str, float]]) -> dict[str, int]:
    """Rank entities by ascending score with competition ranking (ties share a rank)."""
    ordered = sorted(scores, key=lambda kv: kv[1])
    ranks: dict[str, int] = {}
    prev_score: float | None = None
    prev_rank = 0
    for i, (name, score) in enumerate(ordered):
        if prev_score is not None and abs(score - prev_score) < 1e-9:
            ranks[name] = prev_rank
        else:
            ranks[name] = i + 1
            prev_rank = i + 1
            prev_score = score
    return ranks


def _quality_scores(entities: list[tuple[str, dict]]) -> dict[str, float]:
    """Compute a 0–1 composite quality score per entity (lower = better).

    For each metric, entities that have the value are ranked and the rank is
    normalised to [0, 1]; an entity's score is the mean of its normalised ranks.
    Entities with fewer than _MIN_METRICS_FOR_RANK populated metrics are omitted.
    """
    fractions: dict[str, list[float]] = {name: [] for name, _ in entities}

    for metric, higher_better in _QUALITY_METRICS:
        present = [
            (name, data[metric]) for name, data in entities
            if data.get(metric) is not None
        ]
        if len(present) < 2:
            continue  # no comparative signal from a single data point
        # Convert to "ascending = better" scores for ranking
        scored = [
            (name, -value if higher_better else value) for name, value in present
        ]
        ranks = _competition_ranks(scored)
        n = len(present)
        for name, _ in present:
            fractions[name].append((ranks[name] - 1) / (n - 1))

    return {
        name: sum(fr) / len(fr)
        for name, fr in fractions.items()
        if len(fr) >= _MIN_METRICS_FOR_RANK
    }


class Step7Peers(BaseStep):
    """Peer benchmarking — LLM names peers, Python compares them."""

    step_number = 7
    step_name = "Peer Benchmarking"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Identify peers via one light Claude call, then compare deterministically."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        try:
            peer_idents = await self._identify_peers(state)
            peers = await self._fetch_peer_metrics(peer_idents)
            peer_result = self._rank_and_compare(state, peers)
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

    # ------------------------------------------------------------------
    # Phase 1 — LLM peer identification (single light call, no tools)
    # ------------------------------------------------------------------

    async def _identify_peers(self, state: AnalysisState) -> list[tuple[str, str]]:
        """Ask Claude to name 3–5 direct NSE-listed peers. Returns (ticker, name) pairs."""
        system = (
            "You are an Indian equity research analyst. Given a target company "
            "listed on NSE, identify its 3-5 closest DIRECT listed competitors on "
            "NSE — same primary business, comparable end markets. Use official NSE "
            "ticker symbols (e.g. TATAMOTORS, M&M, BAJAJ-AUTO). Do NOT include the "
            "target itself, unlisted companies, subsidiaries of the target, or "
            "foreign listings.\n\n"
            "Return ONLY a JSON object, no markdown:\n"
            '{"peers": [{"ticker": "<NSE symbol>", "name": "<company name>"}]}'
        )

        context = [f"Target company: {state.ticker}"]
        if state.company_name:
            context.append(f"Name: {state.company_name}")
        sector = (
            state.tailwind.sector if state.tailwind else None
        ) or state.sector_name
        if sector:
            context.append(f"Sector: {sector}")
        if state.moat:
            context.append(f"Market position: {state.moat.market_position}")
        if state.quote:
            context.append(f"Market cap: ₹{state.quote.market_cap_cr:,.0f} Cr")

        response_text = await self._call_claude(
            system=system,
            messages=[{"role": "user", "content": "\n".join(context)}],
            model=settings.model_light,
            max_tokens=settings.max_tokens_short,
        )

        try:
            data = self._parse_json_response(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.warning(
                "peer_identify_parse_failed",
                ticker=state.ticker,
                error=str(exc),
                raw=response_text[:200],
            )
            return []

        idents: list[tuple[str, str]] = []
        seen: set[str] = set()
        for p in data.get("peers", []):
            if not isinstance(p, dict):
                continue
            ticker = str(p.get("ticker", "")).upper().strip()
            if (
                not _PEER_TICKER_RE.match(ticker)
                or ticker == state.ticker
                or ticker in seen
            ):
                continue
            seen.add(ticker)
            name = str(p.get("name", "")).strip() or ticker
            idents.append((ticker, name))
        return idents[:5]

    # ------------------------------------------------------------------
    # Phase 2 — deterministic metric fetch (Screener + Yahoo Finance)
    # ------------------------------------------------------------------

    async def _fetch_peer_metrics(
        self, idents: list[tuple[str, str]]
    ) -> list[PeerData]:
        """Fetch financials + valuation for each peer concurrently.

        A hallucinated/invalid ticker simply fails both fetches and is dropped —
        this doubles as validation of the LLM's peer list.
        """
        screener = self.clients.get("screener")
        yfinance = self.clients.get("yfinance")

        async def _none() -> None:
            return None

        async def _one(ticker: str, name: str) -> PeerData | None:
            fin, val = await asyncio.gather(
                screener.get_financials(ticker) if screener else _none(),
                yfinance.get_valuation_data(ticker) if yfinance else _none(),
                return_exceptions=True,
            )
            if isinstance(fin, Exception):
                self.log.debug("peer_financials_failed", peer=ticker, error=str(fin))
                fin = None
            if isinstance(val, Exception):
                self.log.debug("peer_valuation_failed", peer=ticker, error=str(val))
                val = None
            if fin is None and val is None:
                return None
            return PeerData(
                ticker=ticker,
                name=name,
                revenue_cagr_5y=fin.revenue_cagr_5y if fin else None,
                pat_cagr_5y=fin.pat_cagr_5y if fin else None,
                ebitda_margin=fin.ebitda_margin_latest if fin else None,
                roe_5y_avg=fin.roe_5y_avg if fin else None,
                roce_5y_avg=fin.roce_5y_avg if fin else None,
                debt_to_equity=fin.debt_to_equity if fin else None,
                pe_current=val.pe_current if val else None,
                ev_ebitda_forward=val.ev_ebitda_current if val else None,
            )

        results = await asyncio.gather(*(_one(t, n) for t, n in idents))
        return [p for p in results if p is not None]

    # ------------------------------------------------------------------
    # Phase 3 — deterministic ranking and dominance test
    # ------------------------------------------------------------------

    _TARGET_KEY = "__TARGET__"

    def _rank_and_compare(
        self, state: AnalysisState, peers: list[PeerData]
    ) -> PeerComparisonResult:
        """Rank target + peers on quality and valuation; apply the dominance rule.

        A peer DOMINATES the target when it is strictly better ranked on BOTH
        quality and valuation (cheaper by trailing P/E).
        """
        data_flags: list[str] = []

        if len(peers) < 2:
            data_flags.append(
                "[DATA UNVERIFIED: peer_comparison — fewer than 2 peers with "
                "usable data; comparison inconclusive]"
            )
            return PeerComparisonResult(
                gate=GateResult.PASS_CONDITIONAL,
                peer_count=len(peers),
                peers=peers,
                data_flags=data_flags,
            )

        f = state.financials
        target_metrics = {
            "revenue_cagr_5y": f.revenue_cagr_5y if f else None,
            "pat_cagr_5y": f.pat_cagr_5y if f else None,
            "roe_5y_avg": f.roe_5y_avg if f else None,
            "roce_5y_avg": f.roce_5y_avg if f else None,
            "debt_to_equity": f.debt_to_equity if f else None,
        }
        entities: list[tuple[str, dict]] = [(self._TARGET_KEY, target_metrics)]
        for p in peers:
            entities.append(
                (
                    p.ticker,
                    {
                        "revenue_cagr_5y": p.revenue_cagr_5y,
                        "pat_cagr_5y": p.pat_cagr_5y,
                        "roe_5y_avg": p.roe_5y_avg,
                        "roce_5y_avg": p.roce_5y_avg,
                        "debt_to_equity": p.debt_to_equity,
                    },
                )
            )

        # --- Quality ranks ---
        scores = _quality_scores(entities)
        if self._TARGET_KEY not in scores:
            data_flags.append(
                "[DATA UNVERIFIED: peer_comparison — target has insufficient "
                "metrics for quality ranking]"
            )
            return PeerComparisonResult(
                gate=GateResult.PASS_CONDITIONAL,
                peer_count=len(peers),
                peers=peers,
                data_flags=data_flags,
            )
        quality_ranks = _competition_ranks(list(scores.items()))
        target_q_rank = quality_ranks[self._TARGET_KEY]

        dropped = [name for name, _ in entities[1:] if name not in scores]
        if dropped:
            data_flags.append(
                f"[PEER DATA GAP: {', '.join(dropped)} excluded from ranking — "
                "fewer than 2 comparable metrics]"
            )

        # --- Valuation ranks (trailing P/E, lower = cheaper) ---
        target_pe = (
            state.valuation_data.pe_current
            if state.valuation_data and state.valuation_data.pe_current
            and state.valuation_data.pe_current > 0
            else None
        )
        pe_entities = [
            (p.ticker, p.pe_current)
            for p in peers
            if p.ticker in scores and p.pe_current is not None and p.pe_current > 0
        ]
        valuation_ranks: dict[str, int] = {}
        target_v_rank: int | None = None
        if target_pe is not None and pe_entities:
            valuation_ranks = _competition_ranks(
                [(self._TARGET_KEY, target_pe)] + pe_entities
            )
            target_v_rank = valuation_ranks[self._TARGET_KEY]
        else:
            data_flags.append(
                "[DATA UNVERIFIED: peer valuation ranks — P/E unavailable for "
                "target or all peers; dominance test skipped]"
            )

        # --- Dominance test ---
        dominant_peer: str | None = None
        if target_v_rank is not None:
            dominators = [
                ticker
                for ticker, _ in pe_entities
                if quality_ranks[ticker] < target_q_rank
                and valuation_ranks[ticker] < target_v_rank
            ]
            if dominators:
                dominant_peer = min(
                    dominators, key=lambda t: (quality_ranks[t], valuation_ranks[t])
                )

        ranked_count = len(scores)  # target + ranked peers
        if dominant_peer:
            gate = GateResult.FAIL
            data_flags.append(
                f"[PEER DOMINANCE: {dominant_peer} ranks higher on quality "
                f"(#{quality_ranks[dominant_peer]} vs #{target_q_rank}) AND is "
                f"cheaper (P/E rank #{valuation_ranks[dominant_peer]} vs "
                f"#{target_v_rank})]"
            )
        elif target_q_rank <= 2:
            gate = GateResult.PASS_GREEN
        else:
            gate = GateResult.PASS_CONDITIONAL
            data_flags.append(
                f"[PEER: target ranks #{target_q_rank} of {ranked_count} on quality — "
                "not best-in-class; verify the quality gap is priced in]"
            )

        return PeerComparisonResult(
            gate=gate,
            target_quality_rank=target_q_rank,
            target_valuation_rank=target_v_rank,
            peer_count=len(peers),
            peers=peers,
            dominant_peer=dominant_peer,
            data_flags=data_flags,
        )
