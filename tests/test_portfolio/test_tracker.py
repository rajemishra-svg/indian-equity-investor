"""Tests for PortfolioTracker — async DB-backed interface."""
from __future__ import annotations

from datetime import date

import pytest

from src.portfolio.tracker import PortfolioTracker


@pytest.fixture
def tracker(tmp_path) -> PortfolioTracker:
    """PortfolioTracker backed by a temp SQLite DB with user 'testuser'."""
    return PortfolioTracker(db_path=str(tmp_path / "test.db"), user_id="testuser")


@pytest.fixture
def other_tracker(tmp_path) -> PortfolioTracker:
    """Second user on the same DB — used for isolation tests."""
    return PortfolioTracker(db_path=str(tmp_path / "test.db"), user_id="otheruser")


# ---------------------------------------------------------------------------
# add_holding / get_holdings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_holding_and_retrieve(tracker):
    await tracker.add_holding(
        ticker="RELIANCE",
        avg_cost=2800.0,
        quantity=100,
        purchase_date=date(2026, 5, 15),
        allocation_pct=5.0,
        company_name="Reliance Industries",
    )
    holdings = await tracker.get_holdings()
    assert len(holdings) == 1
    h = holdings[0]
    assert h["ticker"] == "RELIANCE"
    assert h["avg_cost"] == pytest.approx(2800.0)
    assert h["quantity"] == 100
    assert h["purchase_date"] == "2026-05-15"
    assert h["allocation_pct"] == pytest.approx(5.0)
    assert h["company_name"] == "Reliance Industries"


@pytest.mark.asyncio
async def test_add_multiple_holdings(tracker):
    await tracker.add_holding("RELIANCE", 2800.0, 100, date(2026, 1, 1), 5.0)
    await tracker.add_holding("HDFCBANK", 1700.0, 50, date(2026, 2, 1), 3.0)
    holdings = await tracker.get_holdings()
    assert len(holdings) == 2
    tickers = [h["ticker"] for h in holdings]
    assert "RELIANCE" in tickers
    assert "HDFCBANK" in tickers


@pytest.mark.asyncio
async def test_get_holdings_empty(tracker):
    assert await tracker.get_holdings() == []


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holdings_isolated_by_user(tracker, other_tracker):
    await tracker.add_holding("RELIANCE", 2800.0, 10, date(2026, 5, 1), 5.0)
    await other_tracker.add_holding("INFY", 1900.0, 20, date(2026, 5, 1), 4.0)

    my_holdings = await tracker.get_holdings()
    other_holdings = await other_tracker.get_holdings()

    assert len(my_holdings) == 1
    assert my_holdings[0]["ticker"] == "RELIANCE"
    assert len(other_holdings) == 1
    assert other_holdings[0]["ticker"] == "INFY"


# ---------------------------------------------------------------------------
# add_transaction / get_transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_transaction_and_retrieve(tracker):
    await tracker.add_transaction(
        ticker="RELIANCE",
        action="BUY",
        price=2850.0,
        quantity=50,
        txn_date=date(2026, 5, 15),
        notes="Tranche 1 entry",
    )
    txns = await tracker.get_transactions()
    assert len(txns) == 1
    t = txns[0]
    assert t["ticker"] == "RELIANCE"
    assert t["action"] == "BUY"
    assert t["price"] == pytest.approx(2850.0)
    assert t["quantity"] == 50
    assert t["notes"] == "Tranche 1 entry"


@pytest.mark.asyncio
async def test_action_uppercased(tracker):
    await tracker.add_transaction("HDFC", "buy", 1700.0, 30, date(2026, 5, 1))
    txns = await tracker.get_transactions()
    assert txns[0]["action"] == "BUY"


@pytest.mark.asyncio
async def test_get_transactions_filter_by_ticker(tracker):
    await tracker.add_transaction("RELIANCE", "BUY", 2800.0, 10, date(2026, 1, 1))
    await tracker.add_transaction("INFY", "BUY", 1900.0, 5, date(2026, 2, 1))
    rel_txns = await tracker.get_transactions(ticker="RELIANCE")
    assert len(rel_txns) == 1
    assert rel_txns[0]["ticker"] == "RELIANCE"


@pytest.mark.asyncio
async def test_transactions_isolated_by_user(tracker, other_tracker):
    await tracker.add_transaction("RELIANCE", "BUY", 2800.0, 10, date(2026, 5, 1))
    assert await other_tracker.get_transactions() == []


# ---------------------------------------------------------------------------
# add_tax_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_tax_entry(tracker):
    import aiosqlite

    await tracker.add_tax_entry(
        ticker="RELIANCE",
        purchase_date=date(2026, 5, 15),
        ltcg_date=date(2027, 5, 15),
        avg_cost=2850.0,
    )
    # Verify directly in DB
    async with aiosqlite.connect(tracker.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM portfolio_tax WHERE user_id=? AND ticker=?",
            (tracker.user_id, "RELIANCE"),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["purchase_date"] == "2026-05-15"
    assert row["ltcg_date"] == "2027-05-15"
    assert row["avg_cost"] == pytest.approx(2850.0)


# ---------------------------------------------------------------------------
# record_sell — FIFO lot consumption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_consumes_oldest_lot_first(tracker):
    await tracker.add_holding("RELIANCE", 2000.0, 10, date(2024, 1, 10))
    await tracker.add_holding("RELIANCE", 2500.0, 10, date(2025, 6, 10))

    result = await tracker.record_sell(
        "RELIANCE", price=3000.0, quantity=10, txn_date=date(2026, 6, 1)
    )

    assert len(result["lots"]) == 1
    lot = result["lots"][0]
    assert lot["purchase_date"] == "2024-01-10"  # oldest lot goes first
    assert lot["lot_exhausted"] is True
    assert result["remaining_qty"] == 10

    holdings = await tracker.get_holdings()
    assert len(holdings) == 1
    assert holdings[0]["avg_cost"] == pytest.approx(2500.0)


@pytest.mark.asyncio
async def test_sell_spans_lots_and_splits_ltcg_stcg(tracker):
    await tracker.add_holding("INFY", 1500.0, 10, date(2024, 1, 1))   # > 1y → LTCG
    await tracker.add_holding("INFY", 1800.0, 10, date(2026, 3, 1))   # ~3m → STCG

    result = await tracker.record_sell(
        "INFY", price=2000.0, quantity=15, txn_date=date(2026, 6, 1)
    )

    first, second = result["lots"]
    assert first["quantity_consumed"] == 10 and first["lot_exhausted"]
    assert second["quantity_consumed"] == 5 and not second["lot_exhausted"]
    assert first["is_ltcg"] is True
    assert second["is_ltcg"] is False
    assert result["ltcg_gain"] == pytest.approx((2000 - 1500) * 10)
    assert result["stcg_gain"] == pytest.approx((2000 - 1800) * 5)
    assert result["realized_gain"] == pytest.approx(6000.0)
    assert result["remaining_qty"] == 5

    holdings = await tracker.get_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 5


@pytest.mark.asyncio
async def test_oversell_raises_and_records_nothing(tracker):
    await tracker.add_holding("TCS", 3500.0, 5, date(2026, 1, 1))

    with pytest.raises(ValueError, match="only 5 held"):
        await tracker.record_sell("TCS", price=4000.0, quantity=6, txn_date=date(2026, 6, 1))

    holdings = await tracker.get_holdings()
    assert holdings[0]["quantity"] == 5  # untouched
    assert await tracker.get_transactions() == []  # no SELL logged


@pytest.mark.asyncio
async def test_sell_logs_transaction_after_consuming_lots(tracker):
    await tracker.add_holding("HDFCBANK", 1600.0, 10, date(2026, 1, 1))

    await tracker.record_sell("HDFCBANK", price=1700.0, quantity=4, txn_date=date(2026, 6, 1))

    txns = await tracker.get_transactions()
    assert len(txns) == 1
    assert txns[0]["action"] == "SELL"
    assert txns[0]["quantity"] == 4


@pytest.mark.asyncio
async def test_exhausted_lot_drops_its_tax_entry(tracker):
    import aiosqlite

    await tracker.add_holding("WIPRO", 400.0, 10, date(2025, 1, 1))
    await tracker.add_tax_entry("WIPRO", date(2025, 1, 1), date(2026, 1, 1), 400.0)
    await tracker.add_holding("WIPRO", 450.0, 10, date(2025, 7, 1))
    await tracker.add_tax_entry("WIPRO", date(2025, 7, 1), date(2026, 7, 1), 450.0)

    await tracker.record_sell("WIPRO", price=500.0, quantity=10, txn_date=date(2026, 6, 1))

    async with aiosqlite.connect(tracker.db_path) as db:
        async with db.execute(
            "SELECT purchase_date FROM portfolio_tax WHERE user_id=? AND ticker=?",
            (tracker.user_id, "WIPRO"),
        ) as cur:
            rows = [r[0] for r in await cur.fetchall()]
    assert rows == ["2025-07-01"]  # exhausted 2025-01-01 lot's entry removed


@pytest.mark.asyncio
async def test_sell_isolated_by_user(tracker, other_tracker):
    await tracker.add_holding("RELIANCE", 2800.0, 10, date(2026, 1, 1))

    with pytest.raises(ValueError):
        await other_tracker.record_sell(
            "RELIANCE", price=2900.0, quantity=5, txn_date=date(2026, 6, 1)
        )


# ---------------------------------------------------------------------------
# add_one_year — leap-safe LTCG date arithmetic
# ---------------------------------------------------------------------------


def test_add_one_year_regular_date():
    from src.portfolio.tracker import add_one_year

    assert add_one_year(date(2026, 5, 15)) == date(2027, 5, 15)


def test_add_one_year_leap_day():
    from src.portfolio.tracker import add_one_year

    assert add_one_year(date(2024, 2, 29)) == date(2025, 2, 28)
