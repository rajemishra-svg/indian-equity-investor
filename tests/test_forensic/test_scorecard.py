"""Tests for the forensic scorecard (src/forensic/scorecard.py)."""
from __future__ import annotations

from src.forensic.scorecard import build_scorecard
from src.models import FinancialMetrics, GovernanceData, ScorecardItem, ValuationData


def _financials(**overrides) -> FinancialMetrics:
    return FinancialMetrics(**overrides)


def _governance(**overrides) -> GovernanceData:
    return GovernanceData(**overrides)


def _valuation(**overrides) -> ValuationData:
    return ValuationData(**overrides)


def _status(items: list[ScorecardItem], name: str) -> str:
    for item in items:
        if item.name == name:
            return item.status
    raise AssertionError(f"No scorecard item named {name!r}")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_build_scorecard_returns_17_items_all_amber_on_empty_data():
    items = build_scorecard(_financials(), _governance(), None)
    assert len(items) == 17
    assert all(item.status == "amber" for item in items)
    assert {item.category for item in items} == {"Accounting & Shareholding", "Price & Performance"}


# ---------------------------------------------------------------------------
# Accounting & Shareholding
# ---------------------------------------------------------------------------


def test_contingent_liabilities_green_below_10pct():
    items = build_scorecard(_financials(), _governance(contingent_liabilities_pct_networth=5.0), None)
    assert _status(items, "Contingent Liabilities") == "green"


def test_contingent_liabilities_red_at_or_above_10pct():
    items = build_scorecard(_financials(), _governance(contingent_liabilities_pct_networth=15.0), None)
    assert _status(items, "Contingent Liabilities") == "red"


def test_other_income_red_at_15pct_threshold():
    items = build_scorecard(_financials(other_income_pct_revenue=15.0), _governance(), None)
    assert _status(items, "Other Income") == "red"


def test_other_income_green_below_threshold():
    items = build_scorecard(_financials(other_income_pct_revenue=5.0), _governance(), None)
    assert _status(items, "Other Income") == "green"


def test_pledge_green_at_zero():
    items = build_scorecard(_financials(), _governance(promoter_pledging_pct=0.0), None)
    assert _status(items, "Pledge") == "green"


def test_pledge_amber_below_hard_trigger():
    items = build_scorecard(_financials(), _governance(promoter_pledging_pct=5.0), None)
    assert _status(items, "Pledge") == "amber"


def test_pledge_red_at_10pct_hard_trigger():
    items = build_scorecard(_financials(), _governance(promoter_pledging_pct=10.0), None)
    assert _status(items, "Pledge") == "red"


def test_public_holding_green_when_declining():
    items = build_scorecard(_financials(), _governance(public_holding_trend=[40.0, 36.0]), None)
    assert _status(items, "Public Holding") == "green"


def test_public_holding_red_when_rising():
    items = build_scorecard(_financials(), _governance(public_holding_trend=[30.0, 34.0]), None)
    assert _status(items, "Public Holding") == "red"


def test_promoter_holding_green_when_stable_or_rising():
    items = build_scorecard(_financials(), _governance(promoter_holding_trend=[50.0, 52.0]), None)
    assert _status(items, "Promoter Holding") == "green"


def test_promoter_holding_red_when_declining():
    items = build_scorecard(_financials(), _governance(promoter_holding_trend=[55.0, 48.0]), None)
    assert _status(items, "Promoter Holding") == "red"


def test_depreciation_effect_red_when_rate_falls_sharply():
    # Accumulated depreciation deltas (annual charge): 10, 10, 10, 4 — rate collapses in the latest year.
    items = build_scorecard(
        _financials(
            gross_block_cr_series=[100, 110, 120, 130],
            accumulated_depreciation_cr_series=[10, 20, 30, 34],
        ),
        _governance(),
        None,
    )
    assert _status(items, "Depreciation Effect") == "red"


def test_depreciation_effect_green_when_rate_stable():
    items = build_scorecard(
        _financials(
            gross_block_cr_series=[100, 110, 120, 130],
            accumulated_depreciation_cr_series=[10, 21, 33, 46],
        ),
        _governance(),
        None,
    )
    assert _status(items, "Depreciation Effect") == "green"


def test_revenue_recognition_red_on_receivables_outrunning_sales_and_weak_cfo():
    items = build_scorecard(
        _financials(debtor_days_latest=80.0, debtor_days_3y_ago=50.0, cfo_net_profit_3y_avg=40.0),
        _governance(),
        None,
    )
    assert _status(items, "Revenue Recognition") == "red"


def test_revenue_recognition_green_when_cash_conversion_healthy():
    items = build_scorecard(
        _financials(debtor_days_latest=80.0, debtor_days_3y_ago=50.0, cfo_net_profit_3y_avg=90.0),
        _governance(),
        None,
    )
    assert _status(items, "Revenue Recognition") == "green"


# ---------------------------------------------------------------------------
# Price & Performance
# ---------------------------------------------------------------------------


def test_capex_vs_roce_red_when_capex_up_and_roce_deteriorating():
    items = build_scorecard(
        _financials(capex_cr_3y=[100, 150, 200], roce_trend="deteriorating"), _governance(), None
    )
    assert _status(items, "Capex vs ROCE") == "red"


def test_capex_vs_roce_green_when_roce_improving():
    items = build_scorecard(
        _financials(capex_cr_3y=[100, 150, 200], roce_trend="improving"), _governance(), None
    )
    assert _status(items, "Capex vs ROCE") == "green"


def test_roe_green_above_threshold():
    items = build_scorecard(_financials(roe_5y_avg=20.0, roe_trend="stable"), _governance(), None)
    assert _status(items, "ROE") == "green"


def test_roe_red_below_threshold():
    items = build_scorecard(_financials(roe_5y_avg=10.0), _governance(), None)
    assert _status(items, "ROE") == "red"


def test_roe_red_above_threshold_but_deteriorating_trend_says_so():
    """A bank-style case: 5Y average clears the bar, but the trend is
    deteriorating — the explanation must name the trend, not claim the
    average itself is below threshold."""
    items = build_scorecard(_financials(roe_5y_avg=15.8, roe_trend="deteriorating"), _governance(), None)
    item = next(i for i in items if i.name == "ROE")
    assert item.status == "red"
    assert "clears" in item.explanation
    assert "deteriorating" in item.explanation


def test_working_capital_red_on_deterioration_above_30pct():
    items = build_scorecard(_financials(debtor_days_latest=70.0, debtor_days_3y_ago=50.0), _governance(), None)
    assert _status(items, "Working Capital") == "red"


def test_working_capital_green_when_stable():
    items = build_scorecard(_financials(debtor_days_latest=52.0, debtor_days_3y_ago=50.0), _governance(), None)
    assert _status(items, "Working Capital") == "green"


def test_valuation_vs_history_green_below_50th_percentile():
    items = build_scorecard(_financials(), _governance(), _valuation(pe_10y_percentile=30.0))
    assert _status(items, "Current vs Historic Valuation") == "green"


def test_valuation_vs_history_red_above_50th_percentile():
    items = build_scorecard(_financials(), _governance(), _valuation(pe_10y_percentile=80.0))
    assert _status(items, "Current vs Historic Valuation") == "red"


def test_sales_growth_green_above_12pct():
    items = build_scorecard(_financials(revenue_cagr_5y=15.0), _governance(), None)
    assert _status(items, "Sales Growth") == "green"


def test_sales_growth_red_below_12pct():
    items = build_scorecard(_financials(revenue_cagr_5y=5.0), _governance(), None)
    assert _status(items, "Sales Growth") == "red"


def test_roce_strength_green_above_18pct():
    items = build_scorecard(_financials(roce_5y_avg=25.0, roce_trend="stable"), _governance(), None)
    assert _status(items, "ROCE Strength") == "green"


def test_roce_strength_red_below_18pct():
    items = build_scorecard(_financials(roce_5y_avg=10.0), _governance(), None)
    assert _status(items, "ROCE Strength") == "red"


def test_balance_sheet_strength_green_low_leverage():
    items = build_scorecard(_financials(debt_to_equity=0.3, current_ratio=1.5), _governance(), None)
    assert _status(items, "Balance Sheet Strength") == "green"


def test_balance_sheet_strength_red_high_leverage():
    items = build_scorecard(_financials(debt_to_equity=2.0), _governance(), None)
    assert _status(items, "Balance Sheet Strength") == "red"


def test_debt_green_strong_coverage():
    items = build_scorecard(_financials(interest_coverage=10.0, net_debt_ebitda=1.0), _governance(), None)
    assert _status(items, "Debt") == "green"


def test_debt_red_weak_coverage():
    items = build_scorecard(_financials(interest_coverage=2.0), _governance(), None)
    assert _status(items, "Debt") == "red"


def test_share_price_red_when_rerated_ahead_of_earnings():
    items = build_scorecard(
        _financials(pat_cagr_5y=5.0), _governance(), _valuation(pe_10y_percentile=90.0)
    )
    assert _status(items, "Share Price") == "red"


def test_share_price_green_when_earnings_justify_valuation():
    items = build_scorecard(
        _financials(pat_cagr_5y=25.0), _governance(), _valuation(pe_10y_percentile=90.0)
    )
    assert _status(items, "Share Price") == "green"


def test_margin_stability_green_when_stable():
    items = build_scorecard(_financials(ebitda_margin_trend="stable"), _governance(), None)
    assert _status(items, "Margin Stability") == "green"


def test_margin_stability_red_when_deteriorating():
    items = build_scorecard(_financials(ebitda_margin_trend="deteriorating"), _governance(), None)
    assert _status(items, "Margin Stability") == "red"


# ---------------------------------------------------------------------------
# financial_services sector overrides — CAR / GNPA-NNPA / ROA / NIM
# ---------------------------------------------------------------------------


def test_other_income_waived_for_financial_services():
    """A bank's 40%+ other-income ratio must not trigger the generic red flag."""
    items = build_scorecard(
        _financials(other_income_pct_revenue=40.8), _governance(), None, sector_name="financial_services"
    )
    item = next(i for i in items if i.name == "Other Income")
    assert item.status == "amber"
    assert "Waived" in item.explanation


def test_roce_strength_uses_roa_for_financial_services():
    items_healthy = build_scorecard(
        _financials(roa_pct=1.5, roce_5y_avg=5.0), _governance(), None, sector_name="financial_services"
    )
    # roce_5y_avg=5.0 would be red under the generic path — confirms ROA, not ROCE, drove the verdict.
    assert _status(items_healthy, "ROCE Strength") == "green"

    items_weak = build_scorecard(
        _financials(roa_pct=0.5), _governance(), None, sector_name="financial_services"
    )
    assert _status(items_weak, "ROCE Strength") == "red"


def test_balance_sheet_strength_uses_car_for_financial_services():
    items_healthy = build_scorecard(
        _financials(car_pct=16.0, debt_to_equity=8.0), _governance(), None, sector_name="financial_services"
    )
    # debt_to_equity=8.0 would be deeply red under the generic path — confirms CAR drove the verdict.
    assert _status(items_healthy, "Balance Sheet Strength") == "green"

    items_thin = build_scorecard(
        _financials(car_pct=10.0), _governance(), None, sector_name="financial_services"
    )
    assert _status(items_thin, "Balance Sheet Strength") == "red"


def test_debt_uses_gnpa_nnpa_for_financial_services():
    items_healthy = build_scorecard(
        _financials(gnpa_pct=1.2, nnpa_pct=0.3, interest_coverage=1.0),
        _governance(), None, sector_name="financial_services",
    )
    # interest_coverage=1.0 would be red under the generic path — confirms GNPA/NNPA drove the verdict.
    assert _status(items_healthy, "Debt") == "green"

    items_weak = build_scorecard(
        _financials(gnpa_pct=5.0, nnpa_pct=2.0), _governance(), None, sector_name="financial_services"
    )
    assert _status(items_weak, "Debt") == "red"


def test_margin_stability_uses_nim_for_financial_services():
    items_healthy = build_scorecard(
        _financials(nim_pct=3.8, ebitda_margin_trend="deteriorating"),
        _governance(), None, sector_name="financial_services",
    )
    # ebitda_margin_trend="deteriorating" would be red under the generic path — confirms NIM drove the verdict.
    assert _status(items_healthy, "Margin Stability") == "green"

    items_weak = build_scorecard(
        _financials(nim_pct=2.0), _governance(), None, sector_name="financial_services"
    )
    assert _status(items_weak, "Margin Stability") == "red"


def test_non_financial_sector_unaffected_by_new_parameter():
    """Explicit sector_name='default' must behave identically to omitting it."""
    f = _financials(debt_to_equity=0.3, current_ratio=1.5, roce_5y_avg=25.0, roce_trend="stable")
    items_default = build_scorecard(f, _governance(), None)
    items_explicit = build_scorecard(f, _governance(), None, sector_name="default")
    assert items_default == items_explicit
