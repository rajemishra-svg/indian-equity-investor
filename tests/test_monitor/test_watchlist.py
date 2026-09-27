"""Tests for watchlist entry evaluation (src/monitor/watchlist.py)."""
from __future__ import annotations

import pytest

from src.models import MarketMode
from src.monitor.watchlist import (
    evaluate_watchlist_row,
    mode_adjusted_target,
    sort_statuses,
)


def _row(**overrides) -> dict:
    row = {
        "ticker": "GOODCO",
        "recommendation": "WATCHLIST",
        "analysis_mode": "value",
        "market_mode": "normal",
        "dcf_intrinsic_weighted": 1000.0,
        "required_mos_pct": 25.0,
        "target_buy_price": 750.0,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# mode_adjusted_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("analysed_in", "now", "expected_target", "expected_mos"),
    [
        ("normal", MarketMode.NORMAL, 750.0, 25.0),
        ("normal", MarketMode.CORRECTION, 800.0, 20.0),              # 5pp concession
        ("normal", MarketMode.MAXIMUM_OPPORTUNITY, 850.0, 15.0),     # 10pp
        ("correction", MarketMode.NORMAL, 700.0, 30.0),              # concession withdrawn
        ("correction", MarketMode.CORRECTION, 750.0, 25.0),
    ],
)
def test_target_repriced_for_current_mode(analysed_in, now, expected_target, expected_mos):
    target, mos = mode_adjusted_target(_row(market_mode=analysed_in), now)
    assert (target, mos) == (expected_target, expected_mos)


def test_unknown_mode_keeps_stored_target():
    assert mode_adjusted_target(_row(), None) == (750.0, 25.0)


def test_missing_dcf_falls_back_to_stored_target():
    assert mode_adjusted_target(_row(dcf_intrinsic_weighted=None), MarketMode.CORRECTION)[0] == 750.0


# ---------------------------------------------------------------------------
# evaluate_watchlist_row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmp", "status"),
    [(740.0, "ENTER ZONE"), (750.0, "ENTER ZONE"), (820.0, "APPROACHING"), (900.0, "MONITORING")],
)
def test_status_bands(cmp, status):
    assert evaluate_watchlist_row(_row(), cmp, MarketMode.NORMAL).status == status


def test_correction_brings_quality_name_into_zone():
    """₹790 misses the normal-market ₹750 target but meets the correction-mode ₹800."""
    st = evaluate_watchlist_row(_row(), 790.0, MarketMode.CORRECTION)
    assert st.status == "ENTER ZONE"
    assert st.target == 800.0
    assert "re-priced for correction" in st.note


def test_growth_rows_have_no_target():
    st = evaluate_watchlist_row(
        _row(recommendation="GROWTH_WATCHLIST", analysis_mode="growth", dcf_intrinsic_weighted=50_000.0),
        100.0,
        MarketMode.CORRECTION,
    )
    assert st.status == "NO TARGET"
    assert st.target is None


def test_missing_price():
    st = evaluate_watchlist_row(_row(), None, MarketMode.NORMAL)
    assert (st.status, st.target) == ("NO PRICE", 750.0)


def test_sort_in_zone_first_then_closest():
    rows = {
        "FAR": 1000.0, "IN": 700.0, "NEAR": 790.0,
    }
    statuses = [
        evaluate_watchlist_row(_row(ticker=t), cmp, MarketMode.NORMAL) for t, cmp in rows.items()
    ]
    statuses.append(evaluate_watchlist_row(_row(ticker="GROW", analysis_mode="growth"), 1.0, None))
    assert [s.ticker for s in sort_statuses(statuses)] == ["IN", "NEAR", "FAR", "GROW"]


# ---------------------------------------------------------------------------
# Growth-mode targets
# ---------------------------------------------------------------------------

_POST_FIX = "2026-09-26 01:44:20"


def _growth_row(**overrides) -> dict:
    fields = {
        "recommendation": "GROWTH_WATCHLIST", "analysis_mode": "growth",
        "created_at": _POST_FIX, "required_mos_pct": 20.0, "target_buy_price": None,
    }
    return _row(**{**fields, **overrides})


def test_trusted_growth_row_gets_fixed_mos_target_ignoring_mode():
    st = evaluate_watchlist_row(_growth_row(), 790.0, MarketMode.MAXIMUM_OPPORTUNITY)
    assert (st.status, st.target, st.required_mos_pct) == ("ENTER ZONE", 800.0, 20.0)
    assert "not mode-adjusted" in st.note


@pytest.mark.parametrize(
    ("sector", "expected_target"), [(None, 800.0), ("recently_listed", 700.0)]
)
def test_legacy_35pct_default_is_replaced_by_growth_threshold(sector, expected_target):
    st = evaluate_watchlist_row(
        _growth_row(required_mos_pct=35.0, sector_name=sector), 900.0, MarketMode.NORMAL
    )
    assert st.target == expected_target


def test_pre_fix_growth_row_has_no_target():
    st = evaluate_watchlist_row(
        _growth_row(created_at="2026-07-27 10:00:00"), 100.0, MarketMode.NORMAL
    )
    assert st.status == "NO TARGET"
    assert "predates the valuation fix" in st.note
