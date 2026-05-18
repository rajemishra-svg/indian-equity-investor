"""Tests for PortfolioTracker."""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from src.models import AnalysisState
from src.portfolio.tracker import PortfolioTracker
from tests.fixtures.sample_data import SAMPLE_FINANCIALS, SAMPLE_QUOTE


@pytest.fixture
def tmp_portfolio(tmp_path) -> PortfolioTracker:
    """Create a PortfolioTracker pointing at a temp directory."""
    portfolio_dir = tmp_path / "portfolio"
    portfolio_dir.mkdir()
    return PortfolioTracker(portfolio_dir=portfolio_dir)


@pytest.fixture
def tmp_tracker_with_watchlist(tmp_path) -> PortfolioTracker:
    """Tracker with analysis/ directory for watchlist tests."""
    portfolio_dir = tmp_path / "portfolio"
    portfolio_dir.mkdir()
    (tmp_path / "analysis" / "watchlist").mkdir(parents=True)
    tracker = PortfolioTracker(portfolio_dir=portfolio_dir)
    # Override watchlist path resolution by monkeypatching
    # The tracker uses relative paths from cwd for watchlists; use the helper method directly
    return tracker


# ---------------------------------------------------------------------------
# add_holding
# ---------------------------------------------------------------------------

def test_add_holding_writes_formatted_row(tmp_portfolio):
    """add_holding should write a properly formatted table row."""
    tracker = tmp_portfolio
    tracker.add_holding(
        ticker="RELIANCE",
        avg_cost=2800.0,
        quantity=100,
        purchase_date=date(2026, 5, 15),
        allocation_pct=5.0,
        company_name="Reliance Industries",
    )

    holdings_file = tracker.portfolio_dir / "holdings.md"
    assert holdings_file.exists()
    content = holdings_file.read_text()
    assert "RELIANCE" in content
    assert "2800.00" in content
    assert "100" in content
    assert "2026-05-15" in content
    assert "5.0%" in content


def test_add_holding_appends_multiple_rows(tmp_portfolio):
    """Multiple holdings should be appended, not overwritten."""
    tracker = tmp_portfolio
    tracker.add_holding("RELIANCE", 2800.0, 100, date(2026, 1, 1), 5.0)
    tracker.add_holding("HDFC", 1700.0, 50, date(2026, 2, 1), 3.0)

    content = (tracker.portfolio_dir / "holdings.md").read_text()
    assert "RELIANCE" in content
    assert "HDFC" in content


# ---------------------------------------------------------------------------
# get_holdings
# ---------------------------------------------------------------------------

def test_get_holdings_parses_correctly(tmp_portfolio):
    """get_holdings should parse the table and return a list of dicts."""
    tracker = tmp_portfolio
    holdings_file = tracker.portfolio_dir / "holdings.md"
    holdings_file.write_text(
        "| Ticker | Company | Avg Cost | Qty | Purchase Date | Allocation % |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| RELIANCE | Reliance Industries | ₹2800.00 | 100 | 2026-05-15 | 5.0% |\n"
        "| HDFC | HDFC Bank | ₹1700.00 | 50 | 2026-02-01 | 3.0% |\n",
        encoding="utf-8",
    )

    holdings = tracker.get_holdings()
    assert len(holdings) == 2
    assert holdings[0]["ticker"] == "RELIANCE"
    assert holdings[0]["avg_cost"] == pytest.approx(2800.0)
    assert holdings[0]["quantity"] == 100
    assert holdings[1]["ticker"] == "HDFC"
    assert holdings[1]["allocation_pct"] == pytest.approx(3.0)


def test_get_holdings_returns_empty_for_missing_file(tmp_portfolio):
    """get_holdings returns empty list when file doesn't exist."""
    holdings = tmp_portfolio.get_holdings()
    assert holdings == []


# ---------------------------------------------------------------------------
# add_transaction
# ---------------------------------------------------------------------------

def test_add_transaction_writes_row(tmp_portfolio):
    """add_transaction should append a formatted row to transaction-log.md."""
    tracker = tmp_portfolio
    tracker.add_transaction(
        ticker="RELIANCE",
        action="BUY",
        price=2850.0,
        quantity=50,
        txn_date=date(2026, 5, 15),
        notes="Tranche 1 entry",
    )

    txn_file = tracker.portfolio_dir / "transaction-log.md"
    assert txn_file.exists()
    content = txn_file.read_text()
    assert "RELIANCE" in content
    assert "BUY" in content
    assert "2850.00" in content
    assert "Tranche 1 entry" in content


def test_add_transaction_action_uppercased(tmp_portfolio):
    """Action should be uppercased in transaction log."""
    tracker = tmp_portfolio
    tracker.add_transaction("HDFC", "buy", 1700.0, 30, date(2026, 5, 1))
    content = (tracker.portfolio_dir / "transaction-log.md").read_text()
    assert "BUY" in content


# ---------------------------------------------------------------------------
# add_rejection
# ---------------------------------------------------------------------------

def test_add_rejection_writes_to_rejection_tracker(tmp_path):
    """add_rejection should append to analysis/watchlist/rejection-tracker.md."""
    (tmp_path / "analysis" / "watchlist").mkdir(parents=True)
    portfolio_dir = tmp_path / "portfolio"
    portfolio_dir.mkdir()

    original_cwd = Path.cwd()
    import os
    os.chdir(tmp_path)
    try:
        tracker = PortfolioTracker(portfolio_dir=portfolio_dir)
        tracker.add_rejection(
            ticker="BADCO",
            step=1,
            reasons=["promoter_pledging > 10%", "going_concern_qualification"],
            re_eval_condition="When pledging drops to 0%",
        )
    finally:
        os.chdir(original_cwd)

    rejection_file = tmp_path / "analysis" / "watchlist" / "rejection-tracker.md"
    assert rejection_file.exists()
    content = rejection_file.read_text()
    assert "BADCO" in content
    assert "Step 1" in content
    assert "promoter_pledging" in content


# ---------------------------------------------------------------------------
# add_to_watchlist
# ---------------------------------------------------------------------------

def test_add_to_watchlist_writes_to_correct_tier(tmp_path):
    """add_to_watchlist should write to the correct tier file."""
    (tmp_path / "analysis" / "watchlist").mkdir(parents=True)
    portfolio_dir = tmp_path / "portfolio"
    portfolio_dir.mkdir()

    import os
    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        tracker = PortfolioTracker(portfolio_dir=portfolio_dir)
        tracker.add_to_watchlist(
            ticker="WATCHME",
            tier=2,
            reason="Valuation not in buy zone",
        )
    finally:
        os.chdir(original_cwd)

    tier2_file = tmp_path / "analysis" / "watchlist" / "tier2.md"
    assert tier2_file.exists()
    content = tier2_file.read_text()
    assert "WATCHME" in content
    assert "Valuation" in content


# ---------------------------------------------------------------------------
# update_tax_tracker
# ---------------------------------------------------------------------------

def test_update_tax_tracker_writes_entry(tmp_portfolio):
    """update_tax_tracker should append LTCG eligibility entry."""
    tracker = tmp_portfolio
    tracker.update_tax_tracker(
        ticker="RELIANCE",
        purchase_date=date(2026, 5, 15),
        ltcg_date=date(2027, 5, 15),
        avg_cost=2850.0,
    )

    tax_file = tracker.portfolio_dir / "tax-tracker.md"
    assert tax_file.exists()
    content = tax_file.read_text()
    assert "RELIANCE" in content
    assert "2026-05-15" in content
    assert "2027-05-15" in content
    assert "2850.00" in content
