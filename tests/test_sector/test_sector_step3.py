"""Tests for sector-aware Step 3 financial scoring."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agent.steps.step3_financials import Step3Financials
from src.models import AnalysisState, FinancialMetrics, GateResult, StockQuote
from tests.fixtures.sample_data import SAMPLE_QUOTE


def make_step():
    return Step3Financials(anthropic_client=AsyncMock(), clients={})


def _state_with_sector(ticker: str, company: str, sector: str, financials: FinancialMetrics) -> AnalysisState:
    state = AnalysisState(ticker=ticker)
    state.quote = StockQuote(
        ticker=ticker, company_name=company, cmp=500.0,
        w52_high=600.0, w52_low=350.0, market_cap_cr=5000.0
    )
    state.financials = financials
    state.sector_name = sector
    return state


# ---------------------------------------------------------------------------
# Financial services — D/E, ICR, CFO/NP hard triggers all waived
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_financial_hard_triggers_all_waived():
    """NBFC with D/E=7, ICR=None, CFO/NP=10% should NOT trigger hard gate."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=18.0,
        roce_5y_avg=None,
        cfo_net_profit_3y_avg=10.0,   # absurdly low — should be waived
        debt_to_equity=7.0,            # extreme — should be waived
        interest_coverage=None,        # waived
    )
    state = _state_with_sector("BAJFINANCE", "Bajaj Finance Limited", "financial_services", f)

    state = await make_step().run(state)

    assert not state.is_terminated, f"Terminated: {state.termination_reason}"
    assert state.financial_gate is not None
    # D/E and ICR hard triggers should not fire
    triggers = state.financial_gate.hard_triggers_fired or []
    assert not any("D/E" in t or "debt" in t.lower() for t in triggers)
    assert not any("ICR" in t or "coverage" in t.lower() for t in triggers)
    assert not any("CFO" in t for t in triggers)


# ---------------------------------------------------------------------------
# Defence sector — CFO/NP hard trigger at 25% (not 50%)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_defence_cfo_np_hard_trigger_at_25():
    """Defence company with 30% CFO/NP should NOT trigger (25% threshold)."""
    f = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=15.0,
        roce_5y_avg=18.0,
        cfo_net_profit_3y_avg=30.0,   # between 25% (hard trigger) and 40% (hurdle) → hurdle fail, no trigger
        debt_to_equity=0.3,
        interest_coverage=15.0,
    )
    state = _state_with_sector("BEL", "Bharat Electronics Limited", "defence_govt", f)

    state = await make_step().run(state)

    # Should NOT be terminated (CFO/NP at 30% is above the 25% hard trigger)
    assert not state.is_terminated
    triggers = state.financial_gate.hard_triggers_fired or []
    assert not any("CFO" in t for t in triggers)


@pytest.mark.asyncio
async def test_defence_cfo_np_hard_trigger_fires_below_25():
    """Defence company with 20% CFO/NP SHOULD trigger (below 25% hard trigger)."""
    f = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=15.0,
        roce_5y_avg=18.0,
        cfo_net_profit_3y_avg=20.0,   # below 25% defence hard trigger
        debt_to_equity=0.3,
        interest_coverage=15.0,
    )
    state = _state_with_sector("BEL", "Bharat Electronics Limited", "defence_govt", f)

    state = await make_step().run(state)

    assert state.is_terminated
    triggers = state.financial_gate.hard_triggers_fired
    assert any("CFO" in t for t in triggers)


# ---------------------------------------------------------------------------
# Infrastructure — D/E hard trigger at 5.0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_infrastructure_de_hard_trigger_at_5():
    """Infra company with D/E=4.0 should NOT trigger (hard trigger at 5.0)."""
    f = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=12.0,
        roce_5y_avg=14.0,
        cfo_net_profit_3y_avg=65.0,
        debt_to_equity=4.0,    # > max_de_ratio (3.0) but < hard trigger (5.0)
        interest_coverage=2.5,  # above hard trigger (2.0) but below hurdle (3.0)
    )
    state = _state_with_sector("TATAPOWER", "Tata Power Company", "infrastructure_utility", f)

    state = await make_step().run(state)

    # Not terminated — D/E and ICR above their hard triggers
    assert not state.is_terminated
    triggers = state.financial_gate.hard_triggers_fired or []
    assert not any("D/E" in t or "debt" in t.lower() for t in triggers)


@pytest.mark.asyncio
async def test_infrastructure_de_hard_trigger_fires_above_5():
    """Infra company with D/E=6.0 SHOULD trigger (above 5.0 hard trigger)."""
    f = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=12.0,
        roce_5y_avg=14.0,
        cfo_net_profit_3y_avg=65.0,
        debt_to_equity=6.0,
        interest_coverage=3.0,
    )
    state = _state_with_sector("TATAPOWER", "Tata Power Company", "infrastructure_utility", f)

    state = await make_step().run(state)

    assert state.is_terminated
    triggers = state.financial_gate.hard_triggers_fired
    assert any("D/E" in t or "debt" in t.lower() for t in triggers)


# ---------------------------------------------------------------------------
# Default profile — standard triggers apply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_cfo_np_hard_trigger_at_50():
    """Non-sector company with 40% CFO/NP should trigger (50% standard trigger)."""
    f = FinancialMetrics(
        revenue_cagr_5y=14.0,
        pat_cagr_5y=16.0,
        roe_5y_avg=18.0,
        roce_5y_avg=20.0,
        cfo_net_profit_3y_avg=40.0,   # below standard 50% hard trigger
        debt_to_equity=0.5,
        interest_coverage=10.0,
    )
    state = _state_with_sector("SOMESTOCK", "Some Manufacturing Co", "default", f)

    state = await make_step().run(state)

    assert state.is_terminated
    triggers = state.financial_gate.hard_triggers_fired
    assert any("CFO" in t for t in triggers)


# ---------------------------------------------------------------------------
# Recently listed — 5Y hurdles waived, but CFO/NP hurdle still applies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recently_listed_5y_hurdles_waived():
    """Recently listed company — 5Y metrics not penalised."""
    f = FinancialMetrics(
        revenue_cagr_5y=None,    # not available
        pat_cagr_5y=None,        # not available
        roe_5y_avg=None,         # not available
        roce_5y_avg=None,        # not available
        cfo_net_profit_3y_avg=80.0,
        debt_to_equity=0.8,
        interest_coverage=12.0,
    )
    state = _state_with_sector("LGEINDIA", "LG Electronics India", "recently_listed", f)

    state = await make_step().run(state)

    assert not state.is_terminated
    score = state.financial_gate.score
    # None metrics should not count against the score
    assert score >= 3  # CFO, D/E, ICR all pass → at least 3 points
