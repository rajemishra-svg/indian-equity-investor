"""Tests for Step 3G — Growth Financial Gate, specifically the Phase-1 ROIIC hard gate."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step3_growth_financials import Step3GrowthFinancials
from src.models import AnalysisMode, AnalysisState, FinancialMetrics, GrowthMetrics


def make_step() -> Step3GrowthFinancials:
    return Step3GrowthFinancials(anthropic_client=AsyncMock(), clients={})


def make_state(
    rev_3y: float = 32.0,
    rev_1y: float = 30.0,
    roiic_proxy: float | None = None,
    de: float = 0.5,
    cash_runway_months: float | None = None,
    burn_rate_cr_month: float | None = None,
) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO")
    state.analysis_mode = AnalysisMode.GROWTH
    state.financials = FinancialMetrics(
        revenue_cagr_3y=rev_3y,
        debt_to_equity=de,
    )
    state.growth_metrics = GrowthMetrics(
        revenue_cagr_1y=rev_1y,
        roiic_proxy_cfo_revenue=roiic_proxy,
        cash_runway_months=cash_runway_months,
        burn_rate_cr_month=burn_rate_cr_month,
    )
    return state


# ---------------------------------------------------------------------------
# Phase 1 Gate 2: ROIIC hard gate (HT-G5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_roiic_triggers_hard_gate():
    """KAYNES scenario: ROIIC -29% → HT-G5 hard trigger → GROWTH_REJECT at Step 3."""
    state = make_state(rev_3y=48.0, rev_1y=44.0, roiic_proxy=-29.0)
    state = await make_step().run(state)

    assert state.recommendation_type == "GROWTH_REJECT"
    assert state.terminated_at_step == 3
    assert any("HT-G5" in t for t in state.financial_gate.hard_triggers_fired)
    assert any("ROIIC" in t and "-29" in t for t in state.financial_gate.hard_triggers_fired)


@pytest.mark.asyncio
async def test_roiic_below_8_triggers_hard_gate():
    """ROIIC 5% (below 8% floor) → HT-G5 fires."""
    state = make_state(rev_3y=30.0, rev_1y=28.0, roiic_proxy=5.0)
    state = await make_step().run(state)

    assert state.recommendation_type == "GROWTH_REJECT"
    assert state.terminated_at_step == 3
    assert any("HT-G5" in t for t in state.financial_gate.hard_triggers_fired)


@pytest.mark.asyncio
async def test_roiic_exactly_8_passes_hard_gate():
    """ROIIC exactly at threshold 8% → does NOT trigger HT-G5 (boundary inclusive)."""
    state = make_state(rev_3y=30.0, rev_1y=28.0, roiic_proxy=8.0)
    state = await make_step().run(state)

    assert not any("HT-G5" in t for t in state.financial_gate.hard_triggers_fired)
    assert state.terminated_at_step is None or state.terminated_at_step != 3


@pytest.mark.asyncio
async def test_roiic_above_20_gets_full_soft_score():
    """ROIIC ≥ 20% → soft score gets full 1 pt and POSITIVE flag."""
    state = make_state(rev_3y=35.0, rev_1y=33.0, roiic_proxy=25.0)
    state = await make_step().run(state)

    assert not any("HT-G5" in t for t in state.financial_gate.hard_triggers_fired)
    assert state.financial_gate.hurdles_met.get("roiic") is True
    assert any("POSITIVE" in f and "ROIIC" in f for f in state.financial_gate.data_flags)


@pytest.mark.asyncio
async def test_roiic_unavailable_does_not_trigger_hard_gate():
    """When ROIIC can't be computed (capex unavailable), HT-G5 is skipped."""
    state = make_state(rev_3y=30.0, rev_1y=28.0, roiic_proxy=None)
    state = await make_step().run(state)

    assert not any("HT-G5" in t for t in state.financial_gate.hard_triggers_fired)
    assert state.recommendation_type != "GROWTH_REJECT" or state.terminated_at_step != 3


@pytest.mark.asyncio
async def test_existing_hard_triggers_still_fire_alongside_roiic():
    """Severe deceleration (HT-G1) + negative ROIIC (HT-G5) both reported."""
    state = make_state(
        rev_3y=40.0,
        rev_1y=5.0,   # HT-G1: 3Y=40, YoY=5 → decel > 15pp and YoY < 15
        roiic_proxy=-15.0,  # HT-G5: < 8%
    )
    state = await make_step().run(state)

    trigger_texts = " ".join(state.financial_gate.hard_triggers_fired)
    assert "HT-G1" in trigger_texts
    assert "HT-G5" in trigger_texts
    assert state.recommendation_type == "GROWTH_REJECT"
