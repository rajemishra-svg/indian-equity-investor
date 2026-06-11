"""Tests for Step 9 — thesis-call gating by recommendation type."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.steps.step9_output import Step9Output
from src.models import AnalysisState


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
