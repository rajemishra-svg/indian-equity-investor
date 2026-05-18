"""Tests for sector-aware Step 0 pre-screen scoring."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step0_prescreen import Step0PreScreen
from src.models import AnalysisState, FinancialMetrics, GateResult, GovernanceData, StockQuote


def make_step():
    return Step0PreScreen(anthropic_client=AsyncMock(), clients={})


def _base_quote(company_name: str, ticker: str) -> StockQuote:
    return StockQuote(
        ticker=ticker,
        company_name=company_name,
        cmp=500.0,
        w52_high=600.0,
        w52_low=350.0,
        market_cap_cr=5000.0,
    )


def _make_state(ticker: str, company_name: str) -> AnalysisState:
    """Create AnalysisState with company_name pre-set (mirrors pipeline._prefetch_data behaviour)."""
    state = AnalysisState(ticker=ticker)
    state.company_name = company_name
    state.quote = _base_quote(company_name, ticker)
    return state


def _base_governance(pledging: float = 0.0, holding: float = 55.0) -> GovernanceData:
    return GovernanceData(
        promoter_holding_pct=holding,
        promoter_pledging_pct=pledging,
    )


# ---------------------------------------------------------------------------
# Financial services sector — D/E and ICR waived
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_financial_services_de_ratio_waived():
    """High D/E should NOT fail a bank/NBFC at Step 0."""
    state = _make_state("BAJFINANCE", "Bajaj Finance Limited")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=22.0,
        roe_5y_avg=18.0,
        roce_5y_avg=None,           # banks don't report ROCE
        cfo_net_profit_3y_avg=None,  # NBFCs: meaningless
        debt_to_equity=8.0,         # VERY high — but waived for financial sector
        interest_coverage=None,     # waived
    )
    state.governance_data = _base_governance()

    state = await make_step().run(state)

    # Sector should be classified as financial_services
    assert state.sector_name == "financial_services"
    # D/E should NOT appear in failed metrics
    failed = state.pre_screen.failed_metrics or []
    assert not any("debt_to_equity" in m for m in failed)
    # Should not terminate on Step 0
    assert not state.is_terminated


@pytest.mark.asyncio
async def test_financial_services_cfo_np_waived():
    """CFO/NP should be waived for financial sector companies."""
    state = _make_state("CANHLIFE", "Canara HSBC Life Insurance")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=12.0,
        pat_cagr_5y=15.0,
        roe_5y_avg=14.0,
        cfo_net_profit_3y_avg=10.0,  # would fail standard 70% threshold
    )
    state.governance_data = _base_governance()

    state = await make_step().run(state)

    assert state.sector_name == "financial_services"
    failed = state.pre_screen.failed_metrics or []
    assert not any("cfo" in m.lower() for m in failed)


# ---------------------------------------------------------------------------
# Defence sector — CFO/NP threshold relaxed to 40%
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_defence_cfo_np_threshold_relaxed():
    """BEL with 45% CFO/NP should PASS Step 0 with defence_govt profile."""
    state = _make_state("BEL", "Bharat Electronics Limited")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=18.0,
        roce_5y_avg=20.0,
        cfo_net_profit_3y_avg=45.0,  # fails standard 70%, passes defence 40%
        debt_to_equity=0.2,
        interest_coverage=15.0,
    )
    state.governance_data = _base_governance(holding=51.0)

    state = await make_step().run(state)

    assert state.sector_name == "defence_govt"
    # CFO/NP at 45% should pass the defence threshold of 40%
    failed = state.pre_screen.failed_metrics or []
    assert not any("cfo" in m.lower() for m in failed)


@pytest.mark.asyncio
async def test_defence_cfo_np_still_fails_below_40():
    """Defence CFO/NP of 30% should still FAIL even with relaxed threshold."""
    state = _make_state("BEL", "Bharat Electronics Limited")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=10.0,
        pat_cagr_5y=12.0,
        roe_5y_avg=18.0,
        roce_5y_avg=20.0,
        cfo_net_profit_3y_avg=30.0,  # below defence 40% threshold
        debt_to_equity=0.2,
        interest_coverage=15.0,
    )
    state.governance_data = _base_governance(holding=51.0)

    state = await make_step().run(state)

    assert state.sector_name == "defence_govt"
    failed = state.pre_screen.failed_metrics or []
    assert any("cfo" in m.lower() for m in failed)


# ---------------------------------------------------------------------------
# Recently listed sector — 5Y metrics waived
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recently_listed_5y_metrics_waived():
    """LGEINDIA (0.5 years listed) should not be penalised for missing 5Y data."""
    state = AnalysisState(ticker="LGEINDIA")
    state.quote = _base_quote("LG Electronics India", "LGEINDIA")
    # 5Y metrics not available
    state.financials = FinancialMetrics(
        revenue_cagr_5y=None,
        pat_cagr_5y=None,
        roe_5y_avg=None,
        roce_5y_avg=None,
        cfo_net_profit_3y_avg=75.0,
        debt_to_equity=0.5,
        interest_coverage=10.0,
    )
    state.governance_data = _base_governance(holding=66.0)

    # Inject listing_years so classifier picks recently_listed
    # We simulate this by pre-setting sector_name
    state.sector_name = "recently_listed"

    state = await make_step().run(state)

    # 5Y metrics should be waived — not in failed list
    failed = state.pre_screen.failed_metrics or []
    assert not any("revenue_cagr_5y" in m for m in failed)
    assert not any("pat_cagr_5y" in m for m in failed)
    assert not any("roe_5y" in m for m in failed)
    assert not any("roce_5y" in m for m in failed)


# ---------------------------------------------------------------------------
# Infrastructure / utility — D/E threshold relaxed to 3.0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_infrastructure_de_threshold_relaxed():
    """Tata Power with D/E=2.5 should PASS Step 0 (infra threshold = 3.0)."""
    state = _make_state("TATAPOWER", "Tata Power Company")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=12.0,
        pat_cagr_5y=15.0,
        roe_5y_avg=12.0,
        roce_5y_avg=14.0,
        cfo_net_profit_3y_avg=70.0,
        debt_to_equity=2.5,      # > standard 1.0 but < infra 3.0
        interest_coverage=4.0,   # above infra min of 3.0
    )
    state.governance_data = _base_governance(holding=46.0)

    state = await make_step().run(state)

    assert state.sector_name == "infrastructure_utility"
    failed = state.pre_screen.failed_metrics or []
    assert not any("debt_to_equity" in m for m in failed)


# ---------------------------------------------------------------------------
# Sector override note logged in flags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sector_override_logged_in_flags():
    """When a sector override is applied, a SECTOR flag should be logged."""
    state = _make_state("BAJFINANCE", "Bajaj Finance Limited")
    state.financials = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=22.0,
        roe_5y_avg=18.0,
        debt_to_equity=8.0,
    )
    state.governance_data = _base_governance()

    state = await make_step().run(state)

    assert any("SECTOR" in f for f in state.all_data_flags)
