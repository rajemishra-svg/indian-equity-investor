"""Tests for holdings exit monitoring (src/monitor/holdings.py)."""
from __future__ import annotations

from datetime import date

import pytest

from src.monitor.holdings import (
    HoldingPosition,
    evaluate_holding,
    exit_levels,
    rollup_lots,
    sort_alerts,
)

TODAY = date(2026, 9, 25)


def _pos(avg_cost: float = 1000.0, first_buy: str | None = "2025-01-10") -> HoldingPosition:
    return HoldingPosition(ticker="TESTCO", quantity=10, avg_cost=avg_cost, first_buy=first_buy)


def _analysis(**overrides) -> dict:
    row = {
        "ticker": "TESTCO",
        "analysis_date": "2026-09-20",
        "recommendation": "BUY",
        "cap_size": "large_cap",
        "sector_name": None,
        "dcf_intrinsic_weighted": 1200.0,
        "termination_reason": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# exit_levels
# ---------------------------------------------------------------------------


def test_review_level_anchored_to_avg_cost_by_cap_size():
    assert exit_levels(_pos(), _analysis()).review_price == 820.0            # large 18%
    assert exit_levels(_pos(), _analysis(cap_size="small_cap")).review_price == 700.0
    assert exit_levels(_pos(), None).review_price == 750.0                    # mid default


def test_stored_multiplier_wins_over_cap_size():
    lv = exit_levels(_pos(), _analysis(stop_loss_multiplier=0.9))
    assert lv.review_price == 900.0


def test_exit_ladder_derived_from_dcf_for_legacy_rows():
    lv = exit_levels(_pos(), _analysis())
    assert (lv.trim, lv.reduce, lv.full) == (1380.0, 1800.0, 2400.0)   # 1.15/1.5/2.0


def test_exit_ladder_uses_cyclical_multipliers():
    lv = exit_levels(_pos(), _analysis(sector_name="commodities_cyclical"))
    assert (lv.trim, lv.reduce, lv.full) == (1320.0, 1560.0, 2040.0)   # 1.10/1.3/1.7


def test_stored_exit_ladder_preferred():
    lv = exit_levels(
        _pos(),
        _analysis(exit_trim_price=1.0, exit_reduce_price=2.0, exit_full_price=3.0),
    )
    assert (lv.trim, lv.reduce, lv.full) == (1.0, 2.0, 3.0)


def test_no_dcf_means_no_ladder():
    lv = exit_levels(_pos(), _analysis(dcf_intrinsic_weighted=None))
    assert lv.trim is lv.reduce is lv.full is None


# ---------------------------------------------------------------------------
# evaluate_holding
# ---------------------------------------------------------------------------


def test_healthy_position_is_ok():
    a = evaluate_holding(_pos(), _analysis(), 1100.0, TODAY)
    assert a.severity == "OK"
    assert a.reasons == []
    assert a.pnl_pct == 10.0


def test_sharp_fall_asks_for_reanalysis_not_exit():
    a = evaluate_holding(_pos(), _analysis(cmp=1100.0), 800.0, TODAY)
    assert (a.severity, a.action) == ("MEDIUM", "SHARP FALL")
    assert "re-analyse the thesis" in a.reasons[0]
    assert "20% below avg cost" in a.reasons[0]


def test_fall_already_reanalysed_with_thesis_intact_is_low():
    a = evaluate_holding(_pos(), _analysis(cmp=790.0), 800.0, TODAY)
    assert (a.severity, a.action) == ("LOW", "Fall re-checked")
    assert "→ BUY" in a.reasons[0]


def test_fall_reanalysed_but_thesis_broken_stays_high():
    a = evaluate_holding(
        _pos(), _analysis(cmp=790.0, recommendation="REJECT"), 800.0, TODAY
    )
    assert (a.severity, a.action) == ("HIGH", "THESIS BROKEN")
    assert not any("re-analyse" in r for r in a.reasons)
    assert any("below avg cost" in r for r in a.reasons)


def test_small_dip_above_review_level_is_ok():
    a = evaluate_holding(_pos(), _analysis(), 850.0, TODAY)
    assert a.severity == "OK"


@pytest.mark.parametrize(
    ("cmp", "severity", "action"),
    [
        (1400.0, "MEDIUM", "TRIM"),
        (1850.0, "MEDIUM", "REDUCE"),
        (2500.0, "HIGH", "FULL EXIT"),
    ],
)
def test_exit_ladder_levels(cmp, severity, action):
    a = evaluate_holding(_pos(), _analysis(), cmp, TODAY)
    assert (a.severity, a.action) == (severity, action)


@pytest.mark.parametrize("rec", ["REJECT", "PEER_SWITCH", "GROWTH_REJECT"])
def test_broken_thesis_is_high_even_when_price_fine(rec):
    a = evaluate_holding(
        _pos(), _analysis(recommendation=rec, termination_reason="Pledging > 10%"), 1100.0, TODAY
    )
    assert (a.severity, a.action) == ("HIGH", "THESIS BROKEN")
    assert "Pledging > 10%" in a.reasons[0]


def test_stale_analysis_is_low():
    a = evaluate_holding(_pos(), _analysis(analysis_date="2026-06-01"), 1100.0, TODAY)
    assert (a.severity, a.action) == ("LOW", "Stale analysis")


def test_no_analysis_still_checks_stop():
    a = evaluate_holding(_pos(), None, 700.0, TODAY)
    assert a.action == "SHARP FALL"
    assert any("Never analysed" in r for r in a.reasons)


def test_missing_price_is_medium():
    a = evaluate_holding(_pos(), _analysis(), None, TODAY)
    assert (a.severity, a.action) == ("MEDIUM", "No live price")


def test_multiple_rules_keep_worst_first_and_all_reasons():
    a = evaluate_holding(
        _pos(), _analysis(recommendation="REJECT", analysis_date="2026-01-01"), 1400.0, TODAY
    )
    assert a.severity == "HIGH"
    assert len(a.reasons) == 3   # thesis broken + trim + stale


def test_ltcg_note_when_exit_fires_close_to_eligibility():
    a = evaluate_holding(_pos(first_buy="2025-10-20"), _analysis(), 1400.0, TODAY)
    assert any("turns LTCG in 25d" in r for r in a.reasons)


def test_no_ltcg_note_on_sharp_fall():
    a = evaluate_holding(_pos(first_buy="2025-10-20"), _analysis(), 700.0, TODAY)
    assert not any("LTCG" in r for r in a.reasons)


# ---------------------------------------------------------------------------
# rollup / sort
# ---------------------------------------------------------------------------


def test_rollup_weights_cost_and_keeps_oldest_buy():
    lots = [
        {"ticker": "B", "quantity": 10, "avg_cost": 100.0, "purchase_date": "2026-03-01"},
        {"ticker": "B", "quantity": 30, "avg_cost": 200.0, "purchase_date": "2025-11-01"},
        {"ticker": "A", "quantity": 5, "avg_cost": 50.0, "purchase_date": "2026-01-01"},
    ]
    a, b = rollup_lots(lots)
    assert (a.ticker, a.quantity, a.avg_cost) == ("A", 5, 50.0)
    assert (b.quantity, b.avg_cost, b.first_buy) == (40, 175.0, "2025-11-01")


def test_sort_alerts_most_urgent_first():
    ok = evaluate_holding(_pos(), _analysis(), 1100.0, TODAY)
    broken = evaluate_holding(_pos(), _analysis(recommendation="REJECT"), 1100.0, TODAY)
    fall = evaluate_holding(_pos(), _analysis(), 700.0, TODAY)
    assert [x.action for x in sort_alerts([ok, fall, broken])] == [
        "THESIS BROKEN", "SHARP FALL", "Hold"
    ]


def test_growth_mode_analysis_skips_exit_ladder():
    a = evaluate_holding(_pos(), _analysis(analysis_mode="growth", dcf_intrinsic_weighted=50_000.0),
                         1100.0, TODAY)
    assert a.levels.trim is a.levels.full is None
    assert a.levels.review_price == 820.0
    assert a.severity == "OK"
    assert any("Growth-mode DCF" in r for r in a.reasons)


def test_exit_reason_shows_dcf_multiple():
    a = evaluate_holding(_pos(), _analysis(), 2500.0, TODAY)
    assert "2.1× DCF ₹1,200" in a.reasons[0]
