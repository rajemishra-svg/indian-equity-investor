"""Tests for Step 0 — Quantitative Pre-Screen."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step0_prescreen import Step0PreScreen
from src.models import AnalysisState, FinancialMetrics, GateResult, GovernanceData, StockQuote
from tests.fixtures.sample_data import (
    BAD_GOVERNANCE,
    SAMPLE_FINANCIALS,
    SAMPLE_GOVERNANCE,
    SAMPLE_QUOTE,
    WEAK_FINANCIALS,
)


def make_step(clients=None):
    claude = AsyncMock()
    return Step0PreScreen(anthropic_client=claude, clients=clients or {})


# ---------------------------------------------------------------------------
# Happy path — 9/9
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_9_of_9_pass_green():
    """All 9 metrics passing → PASS_GREEN."""
    state = AnalysisState(ticker="RELIANCE")
    state.quote = SAMPLE_QUOTE
    state.financials = SAMPLE_FINANCIALS
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    assert state.pre_screen is not None
    assert state.pre_screen.score == 9
    assert state.pre_screen.gate == GateResult.PASS_GREEN
    assert not state.is_terminated


# ---------------------------------------------------------------------------
# 7/9 → PASS_GREEN (just above threshold)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_7_of_9_pass_green():
    """Score 7/9 should still be PASS_GREEN."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=22.0,
        roce_5y_avg=25.0,
        cfo_net_profit_3y_avg=92.0,
        debt_to_equity=0.3,
        interest_coverage=12.0,
    )
    g = GovernanceData(
        promoter_holding_pct=51.0,
        promoter_pledging_pct=0.0,  # passes pledging
    )
    q = StockQuote(
        ticker="TEST",
        company_name="Test Co",
        cmp=500.0,
        w52_high=600.0,
        w52_low=400.0,
        market_cap_cr=3000.0,  # >= 2000 → passes
    )

    state = AnalysisState(ticker="TEST")
    state.quote = q
    state.financials = f
    state.governance_data = g

    step = make_step()
    state = await step.run(state)

    assert state.pre_screen.score == 9  # all metrics pass with these values
    assert state.pre_screen.gate == GateResult.PASS_GREEN


# ---------------------------------------------------------------------------
# 6/9 → PASS_CONDITIONAL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_6_of_9_pass_conditional():
    """Score 6/9 → PASS_CONDITIONAL."""
    # 6 metrics pass, 3 fail
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,   # pass
        pat_cagr_5y=20.0,       # pass
        roe_5y_avg=22.0,        # pass
        roce_5y_avg=25.0,       # pass
        cfo_net_profit_3y_avg=92.0,  # pass
        debt_to_equity=0.3,     # pass
        interest_coverage=12.0,
    )
    g = GovernanceData(
        promoter_holding_pct=30.0,   # FAIL: < 40
        promoter_pledging_pct=12.0,  # FAIL: >= 10
    )
    q = StockQuote(
        ticker="TEST",
        company_name="Test Co",
        cmp=500.0,
        w52_high=600.0,
        w52_low=400.0,
        market_cap_cr=1500.0,  # FAIL: < 2000
    )

    state = AnalysisState(ticker="TEST")
    state.quote = q
    state.financials = f
    state.governance_data = g

    step = make_step()
    state = await step.run(state)

    assert state.pre_screen.score == 6
    assert state.pre_screen.gate == GateResult.PASS_CONDITIONAL
    assert not state.is_terminated


# ---------------------------------------------------------------------------
# 4/9 → FAIL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_4_of_9_fail_terminates_pipeline():
    """Score < 5 → FAIL and pipeline terminates."""
    state = AnalysisState(ticker="WEAKCO")
    state.quote = StockQuote(
        ticker="WEAKCO",
        company_name="Weak Co",
        cmp=100.0,
        w52_high=150.0,
        w52_low=80.0,
        market_cap_cr=500.0,  # FAIL
    )
    state.financials = WEAK_FINANCIALS  # 4 out of 7 financial metrics fail
    state.governance_data = GovernanceData(
        promoter_holding_pct=25.0,  # FAIL
        promoter_pledging_pct=15.0,  # FAIL
    )

    step = make_step()
    state = await step.run(state)

    assert state.pre_screen.gate == GateResult.FAIL
    assert state.is_terminated
    assert state.terminated_at_step == 0
    assert state.recommendation_type == "REJECT"


# ---------------------------------------------------------------------------
# Missing data → data flags added
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_financials_adds_data_flags():
    """Missing financial data should add [DATA UNVERIFIED] flags and count as 0."""
    state = AnalysisState(ticker="NODATA")
    state.quote = SAMPLE_QUOTE
    state.financials = None
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    assert any("DATA UNVERIFIED" in f for f in state.all_data_flags)
    # Score should be low (only market cap, promoter metrics available)
    assert state.pre_screen.score <= 3


# ---------------------------------------------------------------------------
# Small cap < 2000 Cr → market cap metric fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_small_cap_market_cap_metric_fails():
    """Market cap < 2000 Cr → market_cap metric = 0."""
    state = AnalysisState(ticker="SMALL")
    state.quote = StockQuote(
        ticker="SMALL",
        company_name="Small Co",
        cmp=50.0,
        w52_high=80.0,
        w52_low=40.0,
        market_cap_cr=1800.0,  # just below threshold
    )
    state.financials = SAMPLE_FINANCIALS
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    assert state.pre_screen.metric_scores.get("market_cap_cr >= 2000") is False
    assert "market_cap_cr >= 2000" in state.pre_screen.failed_metrics
