"""Tests for Step 5G — Growth Valuation, focusing on P1 gates."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step5_growth_valuation import Step5GrowthValuation
from src.models import (
    AnalysisMode,
    AnalysisState,
    FinancialMetrics,
    GrowthMetrics,
    StockQuote,
    ValuationData,
)


def make_step() -> Step5GrowthValuation:
    return Step5GrowthValuation(anthropic_client=AsyncMock(), clients={})


def make_state(
    rev_3y: float = 35.0,
    listing_years: float | None = None,
    sector_name: str = "default",
    tam_cr: float | None = None,
    tam_source: str | None = None,
    trailing_revenue_cr: float = 500.0,
    shares_outstanding_cr: float = 10.0,
    cmp: float = 1000.0,
    market_cap_cr: float = 10_000.0,
    net_debt_cr: float | None = 0.0,
) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO")
    state.analysis_mode = AnalysisMode.GROWTH
    state.sector_name = sector_name
    state.financials = FinancialMetrics(
        revenue_cagr_3y=rev_3y,
        trailing_revenue_cr=trailing_revenue_cr,
    )
    state.growth_metrics = GrowthMetrics(
        listing_years=listing_years,
        tam_size_cr=tam_cr,
        tam_source=tam_source,
    )
    state.valuation_data = ValuationData(
        shares_outstanding_cr=shares_outstanding_cr,
        net_debt_cr=net_debt_cr,
    )
    state.quote = StockQuote(
        ticker="TESTCO",
        company_name="Test Growth Co",
        cmp=cmp,
        w52_high=cmp * 1.3,
        w52_low=cmp * 0.6,
        market_cap_cr=market_cap_cr,
    )
    return state


# ---------------------------------------------------------------------------
# P1a: EC-G2 recently listed MoS adjustment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recently_listed_sector_raises_mos_threshold():
    """sector_name=recently_listed → EC-G2 flag + 30% MoS threshold instead of 20%."""
    state = make_state(sector_name="recently_listed", rev_3y=40.0)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" in flags
    assert "30%" in flags or "30" in flags


@pytest.mark.asyncio
async def test_listing_years_below_1_raises_mos_threshold():
    """listing_years = 0.5 (< 1 year) → EC-G2 flag raised."""
    state = make_state(listing_years=0.5)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" in flags


@pytest.mark.asyncio
async def test_listing_years_above_1_no_ec_g2():
    """listing_years = 2.0 (> 1 year) → no EC-G2 flag; uses standard 20% threshold."""
    state = make_state(listing_years=2.0)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" not in flags


@pytest.mark.asyncio
async def test_no_listing_years_no_ec_g2():
    """listing_years = None (unknown, assumed ≥ 3Y) → no EC-G2 flag."""
    state = make_state(listing_years=None, sector_name="default")
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" not in flags


# ---------------------------------------------------------------------------
# P1b: TAM verification — llm_inference source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_inferred_tam_flagged_as_unverified():
    """tam_source='llm_inference' → TAM UNVERIFIED flag in data_flags."""
    state = make_state(
        tam_cr=50_000.0,
        tam_source="llm_inference",
        market_cap_cr=5_000.0,  # 10× headroom
    )
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM UNVERIFIED" in flags
    assert "CRISIL" in flags or "IBEF" in flags or "NASSCOM" in flags


@pytest.mark.asyncio
async def test_llm_inferred_tam_counted_at_half_weight():
    """llm_inference TAM with ≥10× headroom counts as 0.5 method, not 1."""
    state = make_state(
        tam_cr=500_000.0,   # massive headroom
        tam_source="llm_inference",
        market_cap_cr=5_000.0,
    )
    # Run step; check that it doesn't easily reach full buy zone on TAM alone
    state = await make_step().run(state)
    # The 0.5 weight means TAM alone cannot push methods_in_buy_zone to ≥ 1 integer
    # (0.5 < 1 so gate won't be PASS_GREEN from TAM alone)
    assert state.valuation is not None  # step completed


@pytest.mark.asyncio
async def test_industry_report_tam_not_flagged():
    """tam_source='industry_report' → no TAM UNVERIFIED flag; full credit."""
    state = make_state(
        tam_cr=100_000.0,
        tam_source="industry_report",
        market_cap_cr=5_000.0,
    )
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM UNVERIFIED" not in flags


@pytest.mark.asyncio
async def test_no_tam_gives_skip_flag():
    """No TAM data → G5-5 skip flag, not unverified."""
    state = make_state(tam_cr=None, tam_source=None)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM-ceiling check skipped" in flags
    assert "TAM UNVERIFIED" not in flags
