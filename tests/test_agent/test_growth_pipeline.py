"""Tests for compute_growth_metrics — specifically promoter_holding_trend_5y sourcing."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agent.growth_pipeline import GrowthPipeline, compute_growth_metrics
from src.agent.pipeline import InvestmentPipeline
from src.models import AnalysisState, FinancialMetrics, GovernanceData, MarketMode


def _state(governance_data: GovernanceData | None) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO")
    state.financials = FinancialMetrics(revenue_cagr_3y=30.0)
    state.governance_data = governance_data
    return state


def test_real_bse_trend_increasing_sets_increasing():
    """≥2 BSE quarters with rising holding → 'increasing', independent of insider proxy."""
    g = GovernanceData(promoter_holding_trend=[45.0, 47.0, 50.0])
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y == "increasing"


def test_real_bse_trend_declining_sets_declining():
    g = GovernanceData(promoter_holding_trend=[55.0, 52.0, 48.0])
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y == "declining"


def test_real_bse_trend_stable_sets_stable():
    g = GovernanceData(promoter_holding_trend=[50.0, 50.0, 50.0])
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y == "stable"


def test_insufficient_bse_quarters_falls_back_to_insider_proxy_buying():
    """Fewer than 2 BSE quarters → fall back to the (correctly-cased) insider signal."""
    g = GovernanceData(promoter_holding_trend=[50.0], insider_net_buying_3m="NET_BUYING")
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y == "increasing"


def test_insufficient_bse_quarters_falls_back_to_insider_proxy_selling():
    g = GovernanceData(promoter_holding_trend=[], insider_net_buying_3m="NET_SELLING")
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y == "declining"


def test_insider_proxy_lowercase_no_longer_matches_bug_regression():
    """Regression guard: the old bug compared against lowercase 'buying'/'selling',
    which never matched the actual 'NET_BUYING'/'NET_SELLING' values Step 1 writes.
    Confirms the comparison now uses the real uppercase values."""
    g = GovernanceData(promoter_holding_trend=[], insider_net_buying_3m="NET_BUYING")
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y is not None


def test_no_signal_at_all_leaves_trend_none():
    g = GovernanceData(promoter_holding_trend=[], insider_net_buying_3m="NEUTRAL")
    state = _state(g)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y is None


def test_no_governance_data_leaves_trend_none():
    state = _state(None)
    compute_growth_metrics(state)

    assert state.growth_metrics.promoter_holding_trend_5y is None


class _StopAfterPrefetchError(Exception):
    pass


@pytest.mark.asyncio
async def test_growth_pipeline_passes_same_clients_as_value_pipeline():
    """_prefetch_data falls back NSE → Breeze → yfinance; GrowthPipeline once
    omitted "breeze" from its clients dict, so every NSE miss raised KeyError."""
    seen: dict[str, set[str]] = {}

    def capture(label):
        async def fake_prefetch(self, state, clients):
            seen[label] = set(clients)
            raise _StopAfterPrefetchError

        return fake_prefetch

    with patch("src.agent.growth_pipeline.detect_mode", AsyncMock(return_value=MarketMode.NORMAL)), \
         patch("src.agent.pipeline.detect_mode", AsyncMock(return_value=MarketMode.NORMAL)):
        for label, cls in (("growth", GrowthPipeline), ("value", InvestmentPipeline)):
            with patch.object(cls, "_prefetch_data", capture(label)):
                with pytest.raises(_StopAfterPrefetchError):
                    await cls(claude=AsyncMock()).analyze("TESTCO")

    assert "breeze" in seen["growth"]
    assert seen["growth"] == seen["value"]
