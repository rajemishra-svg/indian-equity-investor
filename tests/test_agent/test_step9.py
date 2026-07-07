"""Tests for Step 9 — thesis-call gating by recommendation type."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.steps.step9_output import Step9Output
from src.models import (
    AnalysisMode,
    AnalysisState,
    ConvictionLevel,  # used in P3 conviction assertions below
    FinancialMetrics,
    GrowthMetrics,
    MoatAssessment,
    MoatType,
    MultibaggerScore,
)

# ---------------------------------------------------------------------------
# P3 helpers — growth conviction formula tests
# ---------------------------------------------------------------------------


def _growth_state(
    rtype: str = "MULTIBAGGER_CANDIDATE",
    moat_durability: str = "High",
    moat_type: str = "network_effect",
    rev_1y: float | None = 35.0,
    rev_3y: float | None = 35.0,
    roiic: float | None = 25.0,
    tam_runway_score: int = 2,
) -> AnalysisState:
    state = AnalysisState(ticker="GROWCO", recommendation_type=rtype)
    state.analysis_mode = AnalysisMode.GROWTH
    state.financials = FinancialMetrics(revenue_cagr_3y=rev_3y)
    state.growth_metrics = GrowthMetrics(
        revenue_cagr_1y=rev_1y,
        roiic_proxy_cfo_revenue=roiic,
    )
    state.moat = MoatAssessment(
        moat_type=MoatType(moat_type),
        moat_durability=moat_durability,
        moat_narrative="Strong network effects.",
        moat_narrative_short="Strong network effects.",
        market_position="leader",
        market_share_trend="stable",
        working_capital_flag="clean",
    )
    ms = MultibaggerScore()
    ms.tam_runway_score = tam_runway_score
    ms.total_score = 8
    ms.verdict = rtype
    state.multibagger_score = ms
    return state


def _thesis_claude() -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "A durable compounding thesis."
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _refusing_claude() -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=AssertionError("Claude must not be called for this outcome")
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rtype, terminated_at",
    [("REJECT", 1), ("PEER_SWITCH", 7)],
)
async def test_no_thesis_call_for_non_actionable_outcomes(rtype, terminated_at):
    state = AnalysisState(
        ticker="REJECTCO",
        recommendation_type=rtype,
        terminated_at_step=terminated_at,
        termination_reason="gate failure",
    )
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.investment_thesis is None
    assert state.formatted_output is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("rtype", ["BUY", "WATCHLIST"])
async def test_thesis_built_for_actionable_outcomes(rtype):
    state = AnalysisState(ticker="GOODCO", recommendation_type=rtype)
    step = Step9Output(_thesis_claude(), {})
    state = await step.run(state)

    assert state.investment_thesis == "A durable compounding thesis."


# ---------------------------------------------------------------------------
# Volatility-aware position sizing
# ---------------------------------------------------------------------------


def _yf_with_vol(vol):
    client = MagicMock()
    client.get_annualized_volatility = AsyncMock(return_value=vol)
    return client


def _buy_state() -> AnalysisState:
    return AnalysisState(ticker="GOODCO", recommendation_type="BUY")


@pytest.mark.asyncio
async def test_high_volatility_halves_allocation():
    # Empty gate scores → LOW conviction → base allocation 2.0%
    yf = _yf_with_vol(50.0)  # 2× the 25% target → factor 0.5
    step = Step9Output(_thesis_claude(), {"yfinance": yf})
    state = await step.run(_buy_state())

    assert state.suggested_allocation_pct == pytest.approx(1.0)
    assert any("VOLATILITY SIZING" in f and "2.0% → 1.0%" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_low_volatility_keeps_full_allocation():
    yf = _yf_with_vol(18.0)  # below target — no cut
    step = Step9Output(_thesis_claude(), {"yfinance": yf})
    state = await step.run(_buy_state())

    assert state.suggested_allocation_pct == pytest.approx(2.0)
    assert any("allocation unchanged" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_extreme_volatility_clamped_at_min_factor():
    yf = _yf_with_vol(200.0)  # raw factor 0.125 → clamped to 0.4 → 0.8 → floor 1.0
    step = Step9Output(_thesis_claude(), {"yfinance": yf})
    state = await step.run(_buy_state())

    assert state.suggested_allocation_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_missing_volatility_flags_and_keeps_allocation():
    yf = _yf_with_vol(None)
    step = Step9Output(_thesis_claude(), {"yfinance": yf})
    state = await step.run(_buy_state())

    assert state.suggested_allocation_pct == pytest.approx(2.0)
    assert any("not risk-adjusted" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_no_volatility_fetch_for_non_buy():
    yf = _yf_with_vol(50.0)
    state = AnalysisState(
        ticker="REJECTCO",
        recommendation_type="REJECT",
        terminated_at_step=1,
        termination_reason="gate failure",
    )
    step = Step9Output(_refusing_claude(), {"yfinance": yf})
    state = await step.run(state)

    yf.get_annualized_volatility.assert_not_awaited()
    assert state.suggested_allocation_pct is None


# ---------------------------------------------------------------------------
# P3: 4-factor growth conviction formula
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_moat_high_momentum_gives_high_conviction():
    """High moat + accelerating revenue + strong ROIIC + full TAM → HIGH conviction."""
    state = _growth_state(
        moat_durability="High",
        rev_1y=42.0,   # accelerating vs 35% CAGR → momentum > 1
        rev_3y=35.0,
        roiic=30.0,    # ≥ 30 → roiic_score = 1.0
        tam_runway_score=2,
    )
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.conviction == ConvictionLevel.HIGH
    assert state.suggested_allocation_pct == 2.0


@pytest.mark.asyncio
async def test_medium_moat_stable_momentum_gives_medium_conviction():
    """Medium moat + stable revenue (YoY == CAGR) + moderate ROIIC → MEDIUM conviction."""
    state = _growth_state(
        moat_durability="Medium",
        rev_1y=30.0,   # exactly at 3Y CAGR → momentum score = 1.0/1.2
        rev_3y=30.0,
        roiic=15.0,    # 10-20 range → 0.50
        tam_runway_score=1,
    )
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.conviction == ConvictionLevel.MEDIUM


@pytest.mark.asyncio
async def test_low_moat_decelerating_gives_low_conviction():
    """Low moat + decelerating revenue + weak ROIIC + no TAM → LOW conviction."""
    state = _growth_state(
        moat_durability="Low",
        rev_1y=10.0,   # far below 3Y CAGR → momentum ≈ 0.24
        rev_3y=35.0,
        roiic=9.0,     # just above hard gate (8%) → 0.25
        tam_runway_score=0,
    )
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.conviction == ConvictionLevel.LOW


@pytest.mark.asyncio
async def test_growth_conviction_flag_shows_formula_breakdown():
    """Conviction flag must contain the weighted breakdown for auditability."""
    state = _growth_state(moat_durability="High", rev_1y=35.0, rev_3y=35.0, roiic=25.0)
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    flags = " ".join(state.all_data_flags)
    assert "GROWTH CONVICTION" in flags
    assert "40%" in flags or "×40%" in flags


@pytest.mark.asyncio
async def test_growth_watchlist_gets_zero_allocation():
    """GROWTH_WATCHLIST always → 0% allocation regardless of other scores."""
    state = _growth_state(rtype="GROWTH_WATCHLIST", moat_durability="High")
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.suggested_allocation_pct == 0.0


@pytest.mark.asyncio
async def test_growth_buy_high_conviction_allocation():
    """GROWTH_BUY + HIGH conviction → 1.5% (not 2.0% which is MULTIBAGGER)."""
    state = _growth_state(
        rtype="GROWTH_BUY",
        moat_durability="High",
        rev_1y=42.0,
        rev_3y=35.0,
        roiic=30.0,
        tam_runway_score=2,
    )
    step = Step9Output(_refusing_claude(), {})
    state = await step.run(state)

    assert state.conviction == ConvictionLevel.HIGH
    assert state.suggested_allocation_pct == 1.5
