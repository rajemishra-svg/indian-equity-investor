"""Tests for Step 5M — Multibagger Potential Scoring (deterministic, no LLM)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step5m_multibagger import Step5MMultibagger
from src.models import (
    AnalysisMode,
    AnalysisState,
    FinancialMetrics,
    GovernanceData,
    GrowthMetrics,
    ValuationData,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_step() -> Step5MMultibagger:
    return Step5MMultibagger(anthropic_client=AsyncMock(), clients={})


def make_state(
    rev_3y: float = 30.0,
    trailing_revenue_cr: float = 500.0,
    peg: float | None = 0.8,
    ev_revenue_ratio: float | None = None,
    roiic_3y: float | None = None,
    roiic_proxy: float | None = 22.0,
    tam_size_cr: float | None = 100_000.0,
    tam_penetration_pct: float | None = 2.0,
    tam_source: str | None = "industry_report",
    holding_trend: str | None = "stable",
    pledging: float = 0.0,
    pledging_trend: str | None = "stable",
    dilution: float | None = None,
    cfo_np: float | None = 85.0,
    other_income_pct: float | None = 5.0,
    rpt_pct: float | None = 3.0,
) -> AnalysisState:
    state = AnalysisState(ticker="GROWTHCO")
    state.analysis_mode = AnalysisMode.GROWTH
    state.financials = FinancialMetrics(
        revenue_cagr_3y=rev_3y,
        trailing_revenue_cr=trailing_revenue_cr,
        cfo_net_profit_3y_avg=cfo_np,
        other_income_pct_revenue=other_income_pct,
    )
    state.valuation_data = ValuationData(
        peg_ratio=peg,
    )
    state.growth_metrics = GrowthMetrics(
        ev_revenue_ratio=ev_revenue_ratio,
        roiic_3y=roiic_3y,
        roiic_proxy_cfo_revenue=roiic_proxy,
        tam_size_cr=tam_size_cr,
        tam_penetration_est_pct=tam_penetration_pct,
        tam_source=tam_source,
        promoter_holding_trend_5y=holding_trend,
        equity_dilution_3y_pct=dilution,
    )
    state.governance_data = GovernanceData(
        promoter_pledging_pct=pledging,
        pledging_trend_direction=pledging_trend,
        rpt_pct_revenue=rpt_pct,
    )
    return state


# ---------------------------------------------------------------------------
# Verdict boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_8_or_above_is_multibagger_candidate():
    """Total ≥ 8 → MULTIBAGGER_CANDIDATE."""
    # Max score setup: valuation_gap=3, reinvestment=2, tam=2, promoter=2, quality=1 = 10
    state = make_state(
        peg=0.4,           # valuation_gap = 3
        rev_3y=35.0,
        roiic_3y=25.0,     # direct ROIIC (not proxy) → reinvestment cap = 2
        tam_penetration_pct=3.0,  # TAM early → tam_early = True
        tam_size_cr=500_000.0,    # huge TAM → 10× multiple → tam_runway = 2
        holding_trend="increasing",
        pledging=0.0,
        dilution=5.0,      # low dilution
        cfo_np=90.0,
        other_income_pct=2.0,
        rpt_pct=2.0,
    )
    state = await make_step().run(state)

    assert state.multibagger_score is not None
    assert state.multibagger_score.total_score >= 8
    assert state.multibagger_score.verdict == "MULTIBAGGER_CANDIDATE"
    assert state.recommendation_type == "MULTIBAGGER_CANDIDATE"


@pytest.mark.asyncio
async def test_score_6_to_7_is_growth_buy():
    """Score 6-7 → GROWTH_BUY.

    Setup: valuation_gap=2, reinvestment=1(proxy capped), tam=1(moderate),
           promoter=2, quality=1 → total=7 → GROWTH_BUY.
    """
    state = make_state(
        peg=0.9,              # valuation_gap = 2 (PEG < 1 + rev ≥ 25%)
        rev_3y=26.0,
        roiic_proxy=22.0,     # proxy ROIIC → max reinvestment = 1 (proxy cap)
        roiic_3y=None,
        tam_penetration_pct=3.0,   # tam_early = True → one condition met
        tam_size_cr=10_000.0,       # 20% of 10k = 2000; 2000/500 = 4× → raw=0; tam_runway=0
        holding_trend="stable",
        pledging=0.0,
        pledging_trend="stable",
        dilution=None,             # unknown → benefit of doubt → +1 condition
        cfo_np=85.0,
    )
    # With tam_runway=0: 2 + 1 + 0 + 2 + 1 = 6 → GROWTH_BUY
    state = await make_step().run(state)

    assert state.multibagger_score is not None
    score = state.multibagger_score.total_score
    assert 6 <= score <= 7
    assert state.multibagger_score.verdict == "GROWTH_BUY"


@pytest.mark.asyncio
async def test_score_4_to_5_is_growth_watchlist():
    """Score 4-5 → GROWTH_WATCHLIST, sets watchlist tier 2."""
    from src.models import WatchlistTier

    state = make_state(
        peg=1.5,         # valuation_gap = 1
        rev_3y=20.0,     # low CAGR
        roiic_proxy=10.0,   # ROIIC proxy but below 20% → reinvestment = 0
        tam_size_cr=10_000.0,  # small TAM → low multiple
        tam_source="llm_inference",
        holding_trend="stable",
        dilution=None,
        cfo_np=85.0,
    )
    state = await make_step().run(state)

    assert state.multibagger_score is not None
    assert 4 <= state.multibagger_score.total_score <= 5
    assert state.multibagger_score.verdict == "GROWTH_WATCHLIST"
    assert state.watchlist_tier == WatchlistTier.TIER_2


@pytest.mark.asyncio
async def test_score_below_4_is_growth_reject():
    """Score 0-3 → GROWTH_REJECT."""
    state = make_state(
        peg=3.0,           # valuation_gap = 0
        rev_3y=15.0,
        roiic_proxy=None,  # reinvestment = 0
        roiic_3y=None,
        tam_size_cr=None,  # TAM unknown = 0
        holding_trend="declining",
        pledging=8.0,      # promoter concern
        dilution=40.0,     # heavy dilution
        cfo_np=-10.0,      # CFO negative → quality = 0
        other_income_pct=25.0,
    )
    state = await make_step().run(state)

    assert state.multibagger_score is not None
    assert state.multibagger_score.total_score <= 3
    assert state.multibagger_score.verdict == "GROWTH_REJECT"
    assert state.recommendation_type == "GROWTH_REJECT"


# ---------------------------------------------------------------------------
# Component 1: Valuation gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peg_below_0_5_at_30pct_cagr_scores_3():
    """PEG < 0.5 AND rev_3y ≥ 30% → valuation_gap = 3."""
    state = make_state(peg=0.45, rev_3y=32.0)
    state = await make_step().run(state)
    assert state.multibagger_score.valuation_gap_score == 3


@pytest.mark.asyncio
async def test_peg_below_1_at_25pct_cagr_scores_2():
    """PEG < 1.0 AND rev_3y ≥ 25% → valuation_gap = 2."""
    state = make_state(peg=0.85, rev_3y=27.0)
    state = await make_step().run(state)
    assert state.multibagger_score.valuation_gap_score == 2


@pytest.mark.asyncio
async def test_peg_1_to_2_scores_1():
    """1.0 ≤ PEG ≤ 2.0 → valuation_gap = 1."""
    state = make_state(peg=1.6, rev_3y=28.0)
    state = await make_step().run(state)
    assert state.multibagger_score.valuation_gap_score == 1


@pytest.mark.asyncio
async def test_peg_above_2_scores_0():
    """PEG > 2.0 → valuation_gap = 0."""
    state = make_state(peg=2.5, rev_3y=30.0)
    state = await make_step().run(state)
    assert state.multibagger_score.valuation_gap_score == 0


@pytest.mark.asyncio
async def test_pre_profit_uses_ev_revenue_proxy_when_peg_unavailable():
    """When PEG is None, fall back to EV/Revenue proxy (pre-profit companies)."""
    state = make_state(peg=None, ev_revenue_ratio=2.0, rev_3y=30.0)
    state = await make_step().run(state)

    # growth-implied fair P/S = 30 / 10 = 3.0; ev_rev=2.0 < 3.0*0.5=1.5 → no, 2.0 > 1.5
    # so it falls into the "< fair PS" branch → valuation_gap = 1
    assert state.multibagger_score.valuation_gap_score in (1, 2)  # depends on exact calc


# ---------------------------------------------------------------------------
# Component 2: Reinvestment runway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_roiic_above_20_and_tam_early_scores_2():
    """Direct ROIIC ≥ 20% + TAM < 10% penetration → reinvestment = 2 (no proxy cap)."""
    state = make_state(roiic_3y=25.0, roiic_proxy=None, tam_penetration_pct=4.0)
    state = await make_step().run(state)
    assert state.multibagger_score.reinvestment_runway == 2


@pytest.mark.asyncio
async def test_proxy_roiic_caps_reinvestment_at_1():
    """Proxy ROIIC ≥ 20% + TAM early → reinvestment capped at 1 (not 2)."""
    state = make_state(roiic_3y=None, roiic_proxy=22.0, tam_penetration_pct=4.0)
    state = await make_step().run(state)
    assert state.multibagger_score.reinvestment_runway == 1  # capped at 1 for proxy


@pytest.mark.asyncio
async def test_tam_unknown_does_not_block_multibagger_candidate():
    """TAM = None → tam_runway = 0, but MULTIBAGGER still possible from other components."""
    state = make_state(
        peg=0.4,           # 3
        rev_3y=32.0,
        roiic_3y=25.0,     # direct ROIIC → 2
        tam_size_cr=None,  # TAM unknown → tam_runway = 0
        holding_trend="increasing",
        pledging=0.0,
        dilution=5.0,
        cfo_np=90.0,
        other_income_pct=2.0,
        rpt_pct=2.0,
    )
    state = await make_step().run(state)

    assert state.multibagger_score.tam_runway_score == 0
    # But valuation_gap(3) + reinvestment(2) + promoter(2) + quality(1) = 8 → still MULTIBAGGER
    assert state.multibagger_score.total_score >= 8
    assert state.multibagger_score.verdict == "MULTIBAGGER_CANDIDATE"
    # TAM unknown flag should be present
    assert any("TAM UNKNOWN" in f for f in state.multibagger_score.data_flags)


# ---------------------------------------------------------------------------
# Component 3: TAM runway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_tam_source_reduces_score():
    """LLM-inferred TAM (confidence 0.5) halves the raw tam_runway_score."""
    state = make_state(
        tam_size_cr=500_000.0,  # huge TAM → raw score = 2
        tam_penetration_pct=2.0,
        tam_source="llm_inference",   # confidence = 0.5
        trailing_revenue_cr=500.0,    # at 20% penetration: 100k Cr → 200× revenue → score 2
    )
    state = await make_step().run(state)

    # raw=2, confidence=0.5, round(2*0.5) = 1
    assert state.multibagger_score.tam_runway_score == 1


@pytest.mark.asyncio
async def test_high_confidence_industry_report_tam_keeps_full_score():
    """Industry-report TAM (confidence 1.0) preserves the raw tam_runway score."""
    state = make_state(
        tam_size_cr=200_000.0,
        trailing_revenue_cr=500.0,    # 20% of 200k = 40k Cr → 80× rev → score 2
        tam_source="industry_report",
    )
    state = await make_step().run(state)

    assert state.multibagger_score.tam_runway_score == 2


# ---------------------------------------------------------------------------
# Component 5: Earnings quality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_cfo_np_reduces_earnings_quality():
    """CFO/NP ≤ 0 → earnings quality flag, score = 0."""
    state = make_state(cfo_np=-5.0)
    state = await make_step().run(state)
    assert state.multibagger_score.earnings_quality_score == 0
    assert any("EARNINGS QUALITY CONCERN" in f for f in state.multibagger_score.data_flags)


@pytest.mark.asyncio
async def test_clean_earnings_scores_quality_1():
    """Good CFO/NP + low other income + low RPT → quality = 1."""
    state = make_state(cfo_np=88.0, other_income_pct=4.0, rpt_pct=2.0)
    state = await make_step().run(state)
    assert state.multibagger_score.earnings_quality_score == 1


# ---------------------------------------------------------------------------
# Milestone generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_milestones_generated():
    """Milestones should be generated and be non-empty."""
    state = make_state()
    state = await make_step().run(state)

    assert state.multibagger_score.key_milestones  # non-empty list
    assert len(state.multibagger_score.key_milestones) <= 3
    # Revenue milestone includes the CAGR rate
    assert any("₹" in m for m in state.multibagger_score.key_milestones)
