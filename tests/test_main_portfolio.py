"""Tests for the `investor portfolio` CLI command's live allocation calculation.

Regression coverage for a bug where "Allocation %" always displayed 0.0% —
it was reading the `allocation_pct` DB column, which is only ever populated
when a caller explicitly passes `add-trade --allocation`, and nobody had.
The fix computes allocation live from quantity × current market price instead.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.config import settings
from src.models import StockQuote
from src.portfolio.tracker import PortfolioTracker


def _quote(ticker: str, cmp: float) -> StockQuote:
    return StockQuote(
        ticker=ticker,
        company_name=ticker,
        cmp=cmp,
        w52_high=cmp * 1.3,
        w52_low=cmp * 0.7,
        market_cap_cr=1000.0,
    )


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point settings.db_path at a throwaway SQLite file for the test's duration."""
    db_path = str(tmp_path / "portfolio_test.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    return db_path


def _seed_holding(db_path: str, ticker: str, avg_cost: float, quantity: int, company: str = "") -> None:
    import asyncio

    tracker = PortfolioTracker(db_path=db_path, user_id="testuser")
    asyncio.run(
        tracker.add_holding(
            ticker=ticker,
            avg_cost=avg_cost,
            quantity=quantity,
            purchase_date=date(2026, 1, 1),
            company_name=company or ticker,
        )
    )


def _mock_yfinance_client(prices: dict[str, float | None]):
    """Build a mock YFinanceClient whose get_stock_quote returns prices[ticker]."""
    client = AsyncMock()

    async def _get_quote(ticker: str):
        price = prices.get(ticker)
        return _quote(ticker, price) if price is not None else None

    client.get_stock_quote = AsyncMock(side_effect=_get_quote)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.usefixtures("isolated_db")
def test_allocation_computed_from_live_market_value(isolated_db):
    """Two holdings of equal live market value split 50/50, regardless of cost basis."""
    _seed_holding(isolated_db, "AAA", avg_cost=100.0, quantity=10)   # cost basis 1000
    _seed_holding(isolated_db, "BBB", avg_cost=500.0, quantity=1)    # cost basis 500

    # Live prices flip the relative size: AAA now worth 500, BBB now worth 500 → 50/50
    mock_client = _mock_yfinance_client({"AAA": 50.0, "BBB": 500.0})

    from src.main import cli

    with patch("src.api.yfinance_client.YFinanceClient", return_value=mock_client):
        result = CliRunner().invoke(cli, ["portfolio", "--user", "testuser"])

    assert result.exit_code == 0
    assert "50.0%" in result.output
    assert "Total Portfolio Value: ₹1,000.00" in result.output


@pytest.mark.usefixtures("isolated_db")
def test_stale_price_falls_back_to_cost_basis_and_is_flagged(isolated_db):
    """When a live quote is unavailable, the holding still counts (at cost basis)
    and the ticker is called out as an estimate rather than silently dropped."""
    _seed_holding(isolated_db, "GOODQUOTE", avg_cost=100.0, quantity=10)  # cost 1000
    _seed_holding(isolated_db, "NOQUOTE", avg_cost=200.0, quantity=5)     # cost 1000

    mock_client = _mock_yfinance_client({"GOODQUOTE": 100.0, "NOQUOTE": None})

    from src.main import cli

    with patch("src.api.yfinance_client.YFinanceClient", return_value=mock_client):
        result = CliRunner().invoke(cli, ["portfolio", "--user", "testuser"])

    assert result.exit_code == 0
    # Equal cost basis (1000 each) → still splits 50/50 even though NOQUOTE has no live price
    assert "50.0%" in result.output
    assert "NOQUOTE" in result.output
    assert "estimate" in result.output.lower()


@pytest.mark.usefixtures("isolated_db")
def test_lots_view_allocation_sums_to_total(isolated_db):
    """Two lots of the same ticker each get their own row; allocations sum correctly."""
    _seed_holding(isolated_db, "MULTI", avg_cost=100.0, quantity=10)
    _seed_holding(isolated_db, "MULTI", avg_cost=120.0, quantity=10)

    mock_client = _mock_yfinance_client({"MULTI": 110.0})

    from src.main import cli

    with patch("src.api.yfinance_client.YFinanceClient", return_value=mock_client):
        result = CliRunner().invoke(cli, ["portfolio", "--user", "testuser", "--lots"])

    assert result.exit_code == 0
    # Both lots have identical qty and the same live price → each is 50% of the (single-ticker) total
    assert result.output.count("50.0%") == 2


def test_empty_holdings_shows_message(isolated_db):
    from src.main import cli

    result = CliRunner().invoke(cli, ["portfolio", "--user", "nosuchuser"])

    assert result.exit_code == 0
    assert "No holdings found" in result.output
