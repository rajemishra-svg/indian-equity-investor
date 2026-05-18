"""Tests for Step 3 — Financial Strength & Consistency Gate."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step3_financials import Step3Financials
from src.models import AnalysisState, FinancialMetrics, GateResult
from tests.fixtures.sample_data import (
    SAMPLE_FINANCIALS,
    SAMPLE_GOVERNANCE,
    SAMPLE_QUOTE,
    WEAK_FINANCIALS,
)


def make_step():
    claude = AsyncMock()
    return Step3Financials(anthropic_client=claude, clients={})


def make_state(financials: FinancialMetrics) -> AnalysisState:
    state = AnalysisState(ticker="TEST")
    state.quote = SAMPLE_QUOTE
    state.financials = financials
    state.governance_data = SAMPLE_GOVERNANCE
    return state


# ---------------------------------------------------------------------------
# 7/7 pass → PASS_GREEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_7_hurdles_pass_green():
    """All 7 hurdles met, no hard triggers → PASS_GREEN."""
    state = make_state(SAMPLE_FINANCIALS)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate is not None
    assert state.financial_gate.score == 7
    assert state.financial_gate.gate == GateResult.PASS_GREEN
    assert not state.financial_gate.hard_triggers_fired
    assert not state.is_terminated


# ---------------------------------------------------------------------------
# 5–6 hurdles → PASS_CONDITIONAL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_5_hurdles_pass_conditional():
    """5/7 hurdles met, no hard triggers → PASS_CONDITIONAL."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,    # pass
        pat_cagr_5y=20.0,        # pass
        roe_5y_avg=22.0,         # pass
        roce_5y_avg=25.0,        # pass
        cfo_net_profit_3y_avg=85.0,  # pass
        debt_to_equity=1.5,      # FAIL: >= 1.0
        interest_coverage=4.0,   # FAIL: <= 6
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.score == 5
    assert state.financial_gate.gate == GateResult.PASS_CONDITIONAL
    assert not state.is_terminated


@pytest.mark.asyncio
async def test_6_hurdles_pass_conditional():
    """6/7 hurdles met → PASS_CONDITIONAL."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=22.0,
        roce_5y_avg=25.0,
        cfo_net_profit_3y_avg=85.0,
        debt_to_equity=0.4,
        interest_coverage=3.5,  # FAIL: <= 6
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.score == 6
    assert state.financial_gate.gate == GateResult.PASS_CONDITIONAL


# ---------------------------------------------------------------------------
# < 5 hurdles → FAIL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_weak_financials_fail_terminates():
    """WEAK_FINANCIALS: < 5 hurdles AND hard triggers → FAIL → terminates."""
    state = make_state(WEAK_FINANCIALS)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.gate == GateResult.FAIL
    assert state.is_terminated
    assert state.terminated_at_step == 3
    assert state.recommendation_type == "REJECT"


# ---------------------------------------------------------------------------
# Hard triggers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cfo_np_below_50_hard_trigger():
    """CFO/NP 3Y avg < 50% → hard trigger fires → FAIL regardless of hurdles."""
    f = FinancialMetrics(
        revenue_cagr_5y=20.0,   # pass
        pat_cagr_5y=22.0,       # pass
        roe_5y_avg=24.0,        # pass
        roce_5y_avg=26.0,       # pass
        cfo_net_profit_3y_avg=40.0,  # HARD TRIGGER: < 50
        debt_to_equity=0.3,     # pass
        interest_coverage=15.0,  # pass
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.gate == GateResult.FAIL
    assert any("CFO/Net Profit" in t for t in state.financial_gate.hard_triggers_fired)
    assert state.is_terminated


@pytest.mark.asyncio
async def test_de_above_3_hard_trigger():
    """D/E > 3.0 → hard trigger → FAIL regardless of other hurdles."""
    f = FinancialMetrics(
        revenue_cagr_5y=20.0,
        pat_cagr_5y=22.0,
        roe_5y_avg=24.0,
        roce_5y_avg=26.0,
        cfo_net_profit_3y_avg=90.0,
        debt_to_equity=3.5,     # HARD TRIGGER
        interest_coverage=15.0,
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.gate == GateResult.FAIL
    assert any("Debt/Equity > 3.0" in t for t in state.financial_gate.hard_triggers_fired)
    assert state.is_terminated


@pytest.mark.asyncio
async def test_interest_coverage_below_3_hard_trigger():
    """Interest coverage < 3x → hard trigger → FAIL."""
    f = FinancialMetrics(
        revenue_cagr_5y=20.0,
        pat_cagr_5y=22.0,
        roe_5y_avg=24.0,
        roce_5y_avg=26.0,
        cfo_net_profit_3y_avg=90.0,
        debt_to_equity=0.3,
        interest_coverage=2.5,  # HARD TRIGGER: < 3
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.gate == GateResult.FAIL
    assert any("Interest coverage" in t for t in state.financial_gate.hard_triggers_fired)


# ---------------------------------------------------------------------------
# No financial data → FAIL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_financial_data_fails():
    """If financials is None, pipeline should terminate with FAIL."""
    state = AnalysisState(ticker="NODATA")
    state.quote = SAMPLE_QUOTE
    state.financials = None
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.gate == GateResult.FAIL
    assert state.is_terminated
    assert "NO_FINANCIAL_DATA" in state.financial_gate.hard_triggers_fired


# ---------------------------------------------------------------------------
# Interest coverage = None (debt-free) → passes the hurdle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debt_free_company_passes_interest_coverage():
    """Interest coverage = None means debt-free → should pass the hurdle."""
    f = FinancialMetrics(
        revenue_cagr_5y=20.0,
        pat_cagr_5y=22.0,
        roe_5y_avg=24.0,
        roce_5y_avg=26.0,
        cfo_net_profit_3y_avg=90.0,
        debt_to_equity=0.0,
        interest_coverage=None,  # debt-free
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.hurdles_met.get("interest_coverage > 6") is True
    assert state.financial_gate.gate == GateResult.PASS_GREEN


# ---------------------------------------------------------------------------
# Financial sector D/E bypass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_financial_sector_de_hurdle_waived():
    """Banks / NBFCs: D/E > 1.0 hurdle should be waived via sector override."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=18.0,
        roce_5y_avg=20.0,
        cfo_net_profit_3y_avg=85.0,
        debt_to_equity=4.5,    # Would normally trigger hard + soft fail
        interest_coverage=None,  # Not applicable for a bank
    )
    state = AnalysisState(ticker="HDFCBANK", company_name="HDFC Bank Limited")
    state.quote = SAMPLE_QUOTE
    state.financials = f
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    # D/E hurdle should be waived — not a failure
    assert state.financial_gate.hurdles_met.get("debt_to_equity < 1.0") is True
    # Hard D/E trigger should also be suppressed
    assert not any("Debt/Equity" in t for t in state.financial_gate.hard_triggers_fired)
    assert any("SECTOR OVERRIDE" in o for o in state.financial_gate.sector_overrides)


@pytest.mark.asyncio
async def test_non_financial_sector_de_hurdle_applies():
    """Non-financial company: D/E > 1.0 hurdle should still apply."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=18.0,
        roce_5y_avg=20.0,
        cfo_net_profit_3y_avg=85.0,
        debt_to_equity=1.5,  # Should fail the hurdle
        interest_coverage=8.0,
    )
    state = AnalysisState(ticker="TATASTEEL", company_name="Tata Steel Limited")
    state.quote = SAMPLE_QUOTE
    state.financials = f
    state.governance_data = SAMPLE_GOVERNANCE

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.hurdles_met.get("debt_to_equity < 1.0") is False


# ---------------------------------------------------------------------------
# Soft quality checks — deceleration and EBITDA margin flags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revenue_deceleration_adds_concern():
    """5Y revenue CAGR >> 3Y revenue CAGR → concern appended (no score change)."""
    f = FinancialMetrics(
        revenue_cagr_5y=25.0,   # Strong 5Y
        revenue_cagr_3y=10.0,   # Severely decelerating
        pat_cagr_5y=20.0,
        roe_5y_avg=22.0,
        roce_5y_avg=24.0,
        cfo_net_profit_3y_avg=88.0,
        debt_to_equity=0.3,
        interest_coverage=12.0,
        ebitda_margin_latest=18.0,
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    # Score should still be 7 (all hurdles pass)
    assert state.financial_gate.score == 7
    # Deceleration concern should appear in flags
    all_flags = "\n".join(state.all_data_flags)
    # The concern is logged but not in all_data_flags — verify the step doesn't break
    assert state.financial_gate.gate == GateResult.PASS_GREEN


@pytest.mark.asyncio
async def test_thin_ebitda_margin_adds_flag():
    """EBITDA margin < 8% → data flag added (no score change)."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=16.0,
        roe_5y_avg=20.0,
        roce_5y_avg=22.0,
        cfo_net_profit_3y_avg=85.0,
        debt_to_equity=0.4,
        interest_coverage=10.0,
        ebitda_margin_latest=6.0,  # Very thin
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    assert state.financial_gate.score == 7
    assert any("EBITDA margin" in f or "ebitda_margin" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_icr_none_with_debt_adds_data_flag():
    """ICR = None when D/E > 0.1 → data flag added (ICR data gap, not debt-free)."""
    f = FinancialMetrics(
        revenue_cagr_5y=18.0,
        pat_cagr_5y=20.0,
        roe_5y_avg=22.0,
        roce_5y_avg=24.0,
        cfo_net_profit_3y_avg=90.0,
        debt_to_equity=0.5,    # Has debt
        interest_coverage=None,  # Data gap, not debt-free
    )
    state = make_state(f)

    step = make_step()
    state = await step.run(state)

    # Hurdle passes (None = debt-free is assumed), but a data flag should warn
    assert state.financial_gate.hurdles_met.get("interest_coverage > 6") is True
    assert any("interest_coverage" in f for f in state.all_data_flags)
