"""Tests for Step 5 — deterministic DCF, EC-02 normalization, gate behavior."""
from __future__ import annotations

import pytest

from src.agent.steps.step5_valuation import (
    _EBITDA_TO_FCF_CONVERSION,
    Step5Valuation,
    _normalized_base_fcf,
    _run_deterministic_dcf,
)
from src.models import (
    AnalysisState,
    FinancialMetrics,
    GateResult,
    StockQuote,
    ValuationData,
)


def _make_state(sector: str = "default", cmp: float = 100.0, **fin_overrides) -> AnalysisState:
    """State with healthy financials; peak-cycle margins (latest 30% vs 5Y avg 18%)."""
    state = AnalysisState(ticker="TESTCO")
    state.sector_name = sector
    state.quote = StockQuote(
        ticker="TESTCO",
        company_name="Test Co",
        cmp=cmp,
        w52_high=cmp * 1.5,
        w52_low=cmp * 0.8,
        market_cap_cr=50_000.0,  # large cap
    )
    fin = dict(
        revenue_cagr_5y=12.0,
        revenue_cagr_3y=10.0,
        pat_cagr_5y=11.0,
        pat_cagr_3y=9.0,
        roe_5y_avg=15.0,
        roce_5y_avg=16.0,
        cfo_net_profit_3y_avg=80.0,
        debt_to_equity=0.5,
        interest_coverage=8.0,
        ebitda_margin_latest=30.0,   # cycle peak
        ebitda_margin_5y_avg=18.0,   # mid-cycle
        trailing_revenue_cr=10_000.0,
    )
    fin.update(fin_overrides)
    state.financials = FinancialMetrics(**fin)
    state.valuation_data = ValuationData(
        pe_current=20.0,
        ev_ebitda_current=10.0,
        pbv_current=3.0,
        peg_ratio=1.1,
        fcf_yield_pct=4.0,
        pe_10y_percentile=40.0,
        fcf_latest_cr=3_000.0,  # peak-cycle FCF
        net_debt_cr=0.0,
        shares_outstanding_cr=500.0,
    )
    return state


# ---------------------------------------------------------------------------
# EC-02 normalized base FCF
# ---------------------------------------------------------------------------


def test_normalized_base_fcf_computation():
    state = _make_state()
    # 10,000 Cr revenue × 18% 5Y avg OPM × 0.55 conversion = 990 Cr
    expected = round(10_000.0 * 0.18 * _EBITDA_TO_FCF_CONVERSION, 2)
    assert _normalized_base_fcf(state) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"trailing_revenue_cr": None},
        {"ebitda_margin_5y_avg": None},
        {"trailing_revenue_cr": 0.0},
        {"ebitda_margin_5y_avg": -5.0},
    ],
)
def test_normalized_base_fcf_missing_or_degenerate_inputs(overrides):
    state = _make_state(**overrides)
    assert _normalized_base_fcf(state) is None


def test_normalized_fcf_actually_drives_dcf():
    """The normalized FCF must change the intrinsic value, not just the flag text."""
    state = _make_state()
    std = _run_deterministic_dcf(state, 13.0, 6.0)
    norm = _run_deterministic_dcf(
        state, 13.0, 6.0, normalized_fcf_cr=_normalized_base_fcf(state)
    )
    # Peak-cycle latest FCF (3,000 Cr) vs normalized (990 Cr): same WACC/growth,
    # so intrinsic must scale down proportionally.
    assert std[3] is not None and norm[3] is not None
    assert norm[3] < std[3]
    assert "Normalized base FCF (EC-02)" in norm[4]
    assert "Normalized" not in std[4]


@pytest.mark.asyncio
async def test_ec02_normalization_applied_for_cyclical_sector():
    state = _make_state(sector="commodities_cyclical")
    step = Step5Valuation(None, {})
    state = await step.run(state)

    assert state.valuation is not None
    applied = [f for f in state.all_data_flags if "EC-02 CYCLICAL: DCF base FCF normalized" in f]
    assert applied, f"EC-02 applied flag missing; flags: {state.all_data_flags}"
    assert not any("normalization skipped" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_ec02_skip_flag_when_normalization_inputs_missing():
    state = _make_state(sector="commodities_cyclical", trailing_revenue_cr=None)
    step = Step5Valuation(None, {})
    state = await step.run(state)

    assert any("EC-02 CYCLICAL: margin normalization skipped" in f for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_default_sector_has_no_ec02_flag():
    state = _make_state(sector="default")
    step = Step5Valuation(None, {})
    state = await step.run(state)

    assert not any("EC-02 CYCLICAL" in f for f in state.all_data_flags)


# ---------------------------------------------------------------------------
# Valuation FAIL → WATCHLIST without terminating the pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valuation_fail_sets_watchlist_but_does_not_terminate():
    state = _make_state(cmp=2_000.0)  # far above any plausible intrinsic value
    state.valuation_data = ValuationData(
        pe_current=80.0,
        ev_ebitda_current=40.0,   # AVOID
        pbv_current=15.0,
        peg_ratio=3.0,            # AVOID
        fcf_yield_pct=1.0,        # EXPENSIVE
        pe_10y_percentile=95.0,   # AVOID
        fcf_latest_cr=3_000.0,
        net_debt_cr=0.0,
        shares_outstanding_cr=500.0,
    )
    step = Step5Valuation(None, {})
    state = await step.run(state)

    assert state.valuation is not None
    assert state.valuation.gate == GateResult.FAIL
    assert state.recommendation_type == "WATCHLIST"
    assert state.watchlist_tier is not None
    assert state.termination_reason is not None
    # The whole point: Steps 6–8 must still run for WATCHLIST candidates.
    assert state.terminated_at_step is None
    assert not state.is_terminated
