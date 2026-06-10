"""Tests for Step 7 — peer identification, deterministic ranking, dominance gate."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.steps.step7_peers import (
    Step7Peers,
    _competition_ranks,
    _quality_scores,
)
from src.models import (
    AnalysisState,
    FinancialMetrics,
    GateResult,
    ValuationData,
)


def _claude_json_response(payload) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = payload if isinstance(payload, str) else json.dumps(payload)
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    return response


def _mock_claude(payload) -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_claude_json_response(payload))
    return client


def _fin(rev, pat, roe, roce, de) -> FinancialMetrics:
    return FinancialMetrics(
        revenue_cagr_5y=rev,
        pat_cagr_5y=pat,
        roe_5y_avg=roe,
        roce_5y_avg=roce,
        debt_to_equity=de,
    )


def _val(pe) -> ValuationData:
    return ValuationData(pe_current=pe)


def _make_clients(financials_by_ticker: dict, valuation_by_ticker: dict) -> dict:
    screener = MagicMock()
    screener.get_financials = AsyncMock(
        side_effect=lambda t: financials_by_ticker.get(t)
    )
    yfinance = MagicMock()
    yfinance.get_valuation_data = AsyncMock(
        side_effect=lambda t: valuation_by_ticker.get(t)
    )
    return {"screener": screener, "yfinance": yfinance}


def _target_state(rev=8.0, pat=7.0, roe=12.0, roce=13.0, de=1.0, pe=40.0) -> AnalysisState:
    state = AnalysisState(ticker="TARGET", company_name="Target Industries")
    state.financials = _fin(rev, pat, roe, roce, de)
    state.valuation_data = _val(pe)
    return state


_TWO_PEER_IDENTIFY = {
    "peers": [
        {"ticker": "DOMPEER", "name": "Dominant Peer Ltd"},
        {"ticker": "WEAKPEER", "name": "Weak Peer Ltd"},
    ]
}


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------


def test_competition_ranks_handles_ties():
    ranks = _competition_ranks([("a", 1.0), ("b", 1.0), ("c", 2.0)])
    assert ranks["a"] == ranks["b"] == 1
    assert ranks["c"] == 3


def test_quality_scores_excludes_sparse_entities():
    entities = [
        ("full", {"revenue_cagr_5y": 10.0, "pat_cagr_5y": 12.0, "roe_5y_avg": 15.0,
                  "roce_5y_avg": 16.0, "debt_to_equity": 0.5}),
        ("also_full", {"revenue_cagr_5y": 8.0, "pat_cagr_5y": 9.0, "roe_5y_avg": 11.0,
                       "roce_5y_avg": 12.0, "debt_to_equity": 1.0}),
        ("sparse", {"revenue_cagr_5y": 20.0, "pat_cagr_5y": None, "roe_5y_avg": None,
                    "roce_5y_avg": None, "debt_to_equity": None}),
    ]
    scores = _quality_scores(entities)
    assert "sparse" not in scores
    assert scores["full"] < scores["also_full"]  # lower = better


# ---------------------------------------------------------------------------
# Dominance gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dominant_peer_triggers_peer_switch():
    """A peer that is strictly better quality AND cheaper must fire PEER_SWITCH."""
    clients = _make_clients(
        financials_by_ticker={
            "DOMPEER": _fin(15.0, 16.0, 22.0, 25.0, 0.2),   # better on everything
            "WEAKPEER": _fin(5.0, 4.0, 8.0, 9.0, 2.0),      # worse on everything
        },
        valuation_by_ticker={
            "DOMPEER": _val(18.0),   # cheaper than target's 40x
            "WEAKPEER": _val(60.0),
        },
    )
    step = Step7Peers(_mock_claude(_TWO_PEER_IDENTIFY), clients)
    state = await step.run(_target_state())

    pc = state.peer_comparison
    assert pc is not None
    assert pc.gate == GateResult.FAIL
    assert pc.dominant_peer == "DOMPEER"
    assert pc.target_quality_rank == 2
    assert pc.target_valuation_rank == 2
    assert state.recommendation_type == "PEER_SWITCH"
    assert state.terminated_at_step == 7


@pytest.mark.asyncio
async def test_best_in_class_target_passes_green():
    clients = _make_clients(
        financials_by_ticker={
            "DOMPEER": _fin(5.0, 4.0, 8.0, 9.0, 2.0),
            "WEAKPEER": _fin(6.0, 5.0, 9.0, 10.0, 1.8),
        },
        valuation_by_ticker={
            "DOMPEER": _val(50.0),
            "WEAKPEER": _val(55.0),
        },
    )
    step = Step7Peers(_mock_claude(_TWO_PEER_IDENTIFY), clients)
    state = await step.run(_target_state(rev=15.0, pat=16.0, roe=22.0, roce=25.0, de=0.3, pe=30.0))

    pc = state.peer_comparison
    assert pc.gate == GateResult.PASS_GREEN
    assert pc.target_quality_rank == 1
    assert pc.dominant_peer is None
    assert state.recommendation_type is None  # unchanged


@pytest.mark.asyncio
async def test_missing_peer_pe_skips_dominance_test():
    """Without valuation data a better-quality peer must NOT force PEER_SWITCH."""
    clients = _make_clients(
        financials_by_ticker={
            "DOMPEER": _fin(15.0, 16.0, 22.0, 25.0, 0.2),
            "WEAKPEER": _fin(5.0, 4.0, 8.0, 9.0, 2.0),
        },
        valuation_by_ticker={},  # yfinance returns None for every peer
    )
    step = Step7Peers(_mock_claude(_TWO_PEER_IDENTIFY), clients)
    state = await step.run(_target_state())

    pc = state.peer_comparison
    assert pc.gate != GateResult.FAIL
    assert pc.dominant_peer is None
    assert any("dominance test skipped" in f for f in pc.data_flags)
    assert state.recommendation_type is None


# ---------------------------------------------------------------------------
# Degraded inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparseable_identification_falls_back_conditional():
    clients = _make_clients({}, {})
    step = Step7Peers(_mock_claude("I could not find any peers, sorry."), clients)
    state = await step.run(_target_state())

    pc = state.peer_comparison
    assert pc.gate == GateResult.PASS_CONDITIONAL
    assert pc.peer_count < 2
    assert state.terminated_at_step is None


@pytest.mark.asyncio
async def test_all_peer_fetches_failing_falls_back_conditional():
    """Hallucinated tickers fail both fetches and are dropped — gate degrades gracefully."""
    clients = _make_clients({}, {})  # every fetch returns None
    step = Step7Peers(_mock_claude(_TWO_PEER_IDENTIFY), clients)
    state = await step.run(_target_state())

    pc = state.peer_comparison
    assert pc.gate == GateResult.PASS_CONDITIONAL
    assert pc.peer_count == 0


@pytest.mark.asyncio
async def test_identify_peers_filters_invalid_and_duplicate_tickers():
    payload = {
        "peers": [
            {"ticker": "TARGET", "name": "Target Industries"},       # the target itself
            {"ticker": "goodco", "name": "Good Co"},                  # lowercase → normalised
            {"ticker": "GOODCO", "name": "Good Co dup"},              # duplicate
            {"ticker": "BAD TICKER!", "name": "Invalid symbol"},      # invalid chars
            {"ticker": "M&M", "name": "Mahindra & Mahindra"},         # & is legal
            "not-a-dict",
        ]
    }
    step = Step7Peers(_mock_claude(payload), _make_clients({}, {}))
    idents = await step._identify_peers(_target_state())

    assert idents == [("GOODCO", "Good Co"), ("M&M", "Mahindra & Mahindra")]
