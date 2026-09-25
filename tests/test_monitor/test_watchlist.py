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
