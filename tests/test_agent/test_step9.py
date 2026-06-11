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
