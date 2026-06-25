"""Tests for Step 0G — Growth Pre-Screen (deterministic, no LLM)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step0_growth_prescreen import Step0GrowthPreScreen
from src.models import (
    AnalysisMode,
    AnalysisState,
    FinancialMetrics,
    GateResult,
    GovernanceData,
    GrowthMetrics,
    StockQuote,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_step() -> Step0GrowthPreScreen:
    return Step0GrowthPreScreen(anthropic_client=AsyncMock(), clients={})


def make_state(
    rev_3y: float = 30.0,
    rev_1y: float | None = None,
    gross_margin_trend: str | None = "stable",
    cash_runway_months: float | None = None,
    burn_rate_cr_month: float | None = None,
    de: float = 0.5,
    market_cap_cr: float = 5_000.0,
    pledging: float = 0.0,
    promoter_holding: float = 60.0,
    holding_trend: str | None = "stable",
    avg_daily_value_cr: float = 5.0,
    fcf_cr: float | None = None,
) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO")
    state.analysis_mode = AnalysisMode.GROWTH
    state.financials = FinancialMetrics(
        revenue_cagr_3y=rev_3y,
        debt_to_equity=de,
    )
    state.growth_metrics = GrowthMetrics(
        revenue_cagr_1y=rev_1y,
        gross_margin_trend=gross_margin_trend,
        cash_runway_months=cash_runway_months,
        burn_rate_cr_month=burn_rate_cr_month,
        promoter_holding_trend_5y=holding_trend,
    )
    state.governance_data = GovernanceData(
        promoter_holding_pct=promoter_holding,
        promoter_pledging_pct=pledging,
        sebi_record_clean=True,
        sebi_record_checked=True,
    )
    state.quote = StockQuote(
        ticker="TESTCO",
        company_name="Test Growth Co",
        cmp=500.0,
        w52_high=600.0,
        w52_low=300.0,
        market_cap_cr=market_cap_cr,
        avg_daily_value_cr=avg_daily_value_cr,
    )
    return state


# ---------------------------------------------------------------------------
# Passing cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_criteria_pass_gives_pass_green():
    """9/9 criteria → PASS_GREEN, no termination."""
    state = make_state(
        rev_3y=35.0,
        rev_1y=38.0,  # accelerating
        gross_margin_trend="expanding",
        cash_runway_months=24.0,
        burn_rate_cr_month=5.0,
        de=0.3,
    )
    state = await make_step().run(state)

    assert state.pre_screen is not None
    assert state.pre_screen.gate == GateResult.PASS_GREEN
    assert state.recommendation_type != "GROWTH_REJECT"
    assert state.terminated_at_step is None


@pytest.mark.asyncio
async def test_exactly_6_pass_conditional():
    """6 criteria passing → PASS_CONDITIONAL (not FAIL).

    G1 passes when rev_3y ≥ 25 (OR condition), so we use pledging/runway/liquidity
    as the three failing criteria to land at exactly 6/9.
    """
    # Fail G7 (pledging ≥ 5%), G4 (no runway), G9 (low liquidity)
    # Pass G1 (rev_3y≥25 → auto-pass), G2 (rev≥25), G3 (stable gm), G5 (de≤1), G6, G8
    state = make_state(
        rev_3y=28.0,
        rev_1y=26.0,              # G1: slightly decel but rev_3y=28≥25 → G1 passes
        gross_margin_trend="stable",  # G3: pass
        cash_runway_months=None,      # G4: no data → fail
        burn_rate_cr_month=None,
        pledging=6.0,              # G7: > 5% threshold → fail
        avg_daily_value_cr=1.5,    # G9: below 2Cr → fail
    )
    state = await make_step().run(state)

    assert state.pre_screen is not None
    assert state.pre_screen.gate == GateResult.PASS_CONDITIONAL
    assert state.terminated_at_step is None
    assert state.recommendation_type != "GROWTH_REJECT"


@pytest.mark.asyncio
async def test_fcf_positive_auto_passes_runway_check():
    """FCF-positive companies auto-pass the cash runway gate (G4)."""
    from src.models import ValuationData

    state = make_state(
        cash_runway_months=None,   # no runway data
        burn_rate_cr_month=None,
    )
    # FCF positive → G4 should auto-pass
    state.valuation_data = ValuationData(fcf_latest_cr=500.0)
    # Need 6 passing to avoid FAIL; set favourable values
    state.growth_metrics.revenue_cagr_1y = 32.0  # G1: accelerating

    state = await make_step().run(state)

    # G4 auto-passed, so it should contribute to score
    assert state.pre_screen is not None
    assert "cash_runway" in state.pre_screen.metric_scores
    assert state.pre_screen.metric_scores["cash_runway"] is True
    # Check flag for auto-pass
    assert any("FCF POSITIVE" in e for e in state.pre_screen.conditional_exceptions)


# ---------------------------------------------------------------------------
# Failing cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_revenue_cagr_fails_g2():
    """Revenue CAGR 3Y < 25% → G2 fails."""
    state = make_state(rev_3y=18.0)
    # Make enough other criteria fail that total < 6
    state.growth_metrics.cash_runway_months = None
    state.growth_metrics.burn_rate_cr_month = None
    state.growth_metrics.revenue_cagr_1y = None  # G1 also fails
    state.quote.avg_daily_value_cr = 1.0          # G9 fails
    state = await make_step().run(state)

    assert state.pre_screen.metric_scores["revenue_cagr_3y >= 25"] is False
    assert state.recommendation_type == "GROWTH_REJECT"
    assert state.terminated_at_step == 0


@pytest.mark.asyncio
async def test_pledging_above_5pct_fails_g7():
    """Pledging ≥ 5% → governance basics (G7) fails."""
    state = make_state(
        rev_3y=30.0,
        pledging=6.0,  # above 5% threshold
        # Keep other criteria minimal so total falls below 6
        rev_1y=None,
        cash_runway_months=None,
        avg_daily_value_cr=1.0,
    )
    state = await make_step().run(state)

    assert state.pre_screen.metric_scores["governance_basics"] is False
    assert any("pledging" in f for f in state.pre_screen.data_flags)


@pytest.mark.asyncio
async def test_score_below_6_terminates_growth_reject():
    """Score < 6 → GROWTH_REJECT and termination at step 0."""
    # Fail G1, G2, G4, G9 — only 5 pass at best
    state = make_state(
        rev_3y=15.0,    # G2 fail
        rev_1y=10.0,    # G1 fail (decel)
        cash_runway_months=None,  # G4 fail
        avg_daily_value_cr=0.5,   # G9 fail
    )
    state = await make_step().run(state)

    assert state.recommendation_type == "GROWTH_REJECT"
    assert state.terminated_at_step == 0
    assert state.termination_reason is not None
    assert "Growth pre-screen FAILED" in state.termination_reason


# ---------------------------------------------------------------------------
# Large-cap flag (EC-G1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_cap_adds_flag_but_does_not_penalise_score():
    """Market cap ≥ ₹20,000 Cr → EC-G1 flag added but G6 still awards the point."""
    state = make_state(
        rev_3y=30.0,
        rev_1y=33.0,
        market_cap_cr=25_000.0,
        gross_margin_trend="expanding",
        cash_runway_months=30.0,
        burn_rate_cr_month=10.0,
    )
    state = await make_step().run(state)

    # G6 still scores True (informational only)
    assert state.pre_screen.metric_scores["market_cap_informational"] is True
    # EC-G1 flag is present
    assert any("EC-G1" in f for f in state.all_data_flags)


# ---------------------------------------------------------------------------
# Service company (no COGS) — G3 conditional pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_gross_margin_gives_conditional_pass():
    """No gross margin data → G3 conditional pass with explanatory note."""
    state = make_state(
        rev_3y=28.0,
        rev_1y=30.0,
        gross_margin_trend=None,  # service company
        cash_runway_months=24.0,
        burn_rate_cr_month=2.0,
    )
    state = await make_step().run(state)

    assert state.pre_screen.metric_scores["gross_margin_not_contracting"] is True
    assert any("GROSS MARGIN UNKNOWN" in e for e in state.pre_screen.conditional_exceptions)
