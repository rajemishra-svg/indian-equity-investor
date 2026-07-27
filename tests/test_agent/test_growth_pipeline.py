"""Tests for compute_growth_metrics — specifically promoter_holding_trend_5y sourcing."""
from __future__ import annotations

from src.agent.growth_pipeline import compute_growth_metrics
from src.models import AnalysisState, FinancialMetrics, GovernanceData


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
