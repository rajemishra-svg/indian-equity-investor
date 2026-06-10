"""Tests for the SQLite persistence layer (src/db/repository.py)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.db.repository import (
    get_analysis_history,
    get_latest_analysis,
    get_summary,
    init_db,
    list_recommendations,
    save_analysis,
    save_snapshot,
)
from src.models import (
    AnalysisState,
    ConvictionLevel,
    FinancialGateResult,
    FinancialMetrics,
    GateResult,
    GovernanceData,
    GovernanceScore,
    MarketMode,
    PreScreenResult,
    StockQuote,
    ValuationResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Return a temporary SQLite DB path that is cleaned up after the test."""
    return str(tmp_path / "test_investor.db")


def _make_minimal_state(ticker: str = "RELIANCE") -> AnalysisState:
    """Build a minimal AnalysisState sufficient for save_analysis."""
    state = AnalysisState(ticker=ticker)
    state.company_name = f"{ticker} Industries"
    state.mode = MarketMode.NORMAL
    state.recommendation_type = "BUY"
    state.conviction = ConvictionLevel.HIGH
    state.investment_thesis = "Strong moat, reasonable valuation."
    state.quote = StockQuote(
        ticker=ticker,
        company_name=state.company_name,
        cmp=2850.0,
        w52_high=3200.0,
        w52_low=2200.0,
        market_cap_cr=19_000.0,
    )
    return state


def _make_full_state(ticker: str = "INFY") -> AnalysisState:
    """Build a state with all step results populated."""
    state = _make_minimal_state(ticker)
    state.sector_name = "default"
    # cap_size is a computed @property derived from market_cap_cr — no setter

    state.pre_screen = PreScreenResult(
        score=8,
        max_score=9,
        gate=GateResult.PASS_GREEN,
        metric_scores={"revenue_cagr_5y >= 12": True},
        failed_metrics=[],
    )

    state.governance = GovernanceScore(
        score=12,
        max_score=15,
        gate=GateResult.PASS_GREEN,
        sub_scores={"pledging": 3, "audit": 3, "rpt": 2, "capital_allocation": 2, "regulatory": 2},
        immediate_triggers=[],
        concerns=[],
    )

    state.financial_gate = FinancialGateResult(
        score=6,
        gate=GateResult.PASS_CONDITIONAL,
        hurdles_met={"revenue_cagr_5y >= 12": True, "cfo_net_profit_3y_avg >= 80": False},
        hard_triggers_fired=[],
    )

    state.valuation = ValuationResult(
        gate=GateResult.PASS_GREEN,
        methods_in_buy_zone=3,
        margin_of_safety_pct=28.5,
        required_mos_pct=25.0,
        dcf_intrinsic_weighted=3600.0,
    )

    return state


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_creates_tables(db_path):
    """init_db should create analyses and data_snapshots tables."""
    import aiosqlite

    await init_db(db_path)
    assert os.path.exists(db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            tables = {row[0] async for row in cursor}

    assert "analyses" in tables
    assert "data_snapshots" in tables


@pytest.mark.asyncio
async def test_init_db_idempotent(db_path):
    """Calling init_db twice should not raise."""
    await init_db(db_path)
    await init_db(db_path)  # second call — should be no-op


# ---------------------------------------------------------------------------
# save_analysis / get_latest_analysis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_retrieve_minimal_state(db_path):
    """Saving a minimal state and retrieving it should round-trip key fields."""
    state = _make_minimal_state("RELIANCE")
    await save_analysis(db_path, state)

    row = await get_latest_analysis(db_path, "RELIANCE")
    assert row is not None
    assert row["ticker"] == "RELIANCE"
    assert row["recommendation"] == "BUY"
    assert row["company_name"] == "RELIANCE Industries"


@pytest.mark.asyncio
async def test_save_and_retrieve_full_state(db_path):
    """Full state with all step results persisted correctly."""
    state = _make_full_state("INFY")
    await save_analysis(db_path, state)

    row = await get_latest_analysis(db_path, "INFY")
    assert row is not None
    assert row["pre_screen_score"] == 8
    assert row["governance_score"] == 12
    assert row["financial_score"] == 6
    assert row["financial_gate"] == "pass_conditional"
    assert row["valuation_gate"] == "pass_green"
    assert abs(row["mos_pct"] - 28.5) < 0.01
    assert row["sector_name"] == "default"
    assert row["cap_size"] == "mid_cap"  # derived from market_cap_cr=19_000 (5k–20k) → mid_cap


@pytest.mark.asyncio
async def test_save_upserts_on_same_day(db_path):
    """Saving the same ticker twice on the same day should update, not insert duplicate."""
    import aiosqlite

    state = _make_minimal_state("HDFC")
    state.recommendation_type = "WATCHLIST"
    await save_analysis(db_path, state)

    # Update recommendation and save again
    state.recommendation_type = "BUY"
    await save_analysis(db_path, state)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM analyses WHERE ticker = 'HDFC'") as cur:
            (count,) = await cur.fetchone()

    assert count == 1, "Should have exactly 1 row per ticker per day (upsert)"

    row = await get_latest_analysis(db_path, "HDFC")
    assert row["recommendation"] == "BUY"  # latest value persisted


@pytest.mark.asyncio
async def test_get_latest_returns_none_for_unknown_ticker(db_path):
    """get_latest_analysis should return None if ticker not in DB."""
    await init_db(db_path)
    result = await get_latest_analysis(db_path, "UNKNOWN")
    assert result is None


@pytest.mark.asyncio
async def test_ticker_uppercased(db_path):
    """Ticker lookup should be case-insensitive (auto-uppercased)."""
    state = _make_minimal_state("TCS")
    await save_analysis(db_path, state)

    row = await get_latest_analysis(db_path, "tcs")  # lowercase query
    assert row is not None
    assert row["ticker"] == "TCS"


# ---------------------------------------------------------------------------
# Terminated state persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_state_persisted(db_path):
    """A terminated / REJECT state should be saved with step number and reason."""
    state = _make_minimal_state("BADCO")
    state.recommendation_type = "REJECT"
    state.terminated_at_step = 1
    state.termination_reason = "Governance FAILED: pledging > 10%"
    state.conviction = None

    await save_analysis(db_path, state)

    row = await get_latest_analysis(db_path, "BADCO")
    assert row["recommendation"] == "REJECT"
    assert row["terminated_at_step"] == 1
    assert "pledging" in row["termination_reason"]


# ---------------------------------------------------------------------------
# JSON field deserialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_fields_deserialised(db_path):
    """governance_sub_scores and all_data_flags should be returned as Python objects."""
    state = _make_full_state("WIPRO")
    state.add_flag("[DATA UNVERIFIED: auditor_name]")
    await save_analysis(db_path, state)

    row = await get_latest_analysis(db_path, "WIPRO")

    assert isinstance(row["governance_sub_scores"], dict)
    assert "pledging" in row["governance_sub_scores"]
    assert isinstance(row["all_data_flags"], list)
    assert any("DATA UNVERIFIED" in f for f in row["all_data_flags"])


# ---------------------------------------------------------------------------
# get_analysis_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_returns_multiple_dates(db_path):
    """History query should return rows across different analysis dates."""
    import aiosqlite
    from datetime import date, timedelta

    state = _make_minimal_state("MARUTI")
    await save_analysis(db_path, state)

    # Manually insert a second row with a different date
    yesterday = (date.today().replace(day=date.today().day - 1)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO analyses (ticker, analysis_date, recommendation, market_mode) "
            "VALUES (?, ?, ?, ?)",
            ("MARUTI", yesterday, "WATCHLIST", "normal"),
        )
        await db.commit()

    rows = await get_analysis_history(db_path, "MARUTI", limit=10)
    assert len(rows) == 2
    # Most recent first
    assert rows[0]["analysis_date"] >= rows[1]["analysis_date"]


@pytest.mark.asyncio
async def test_history_limit_respected(db_path):
    """History should respect the limit parameter."""
    import aiosqlite
    from datetime import date

    await init_db(db_path)  # ensure tables exist before raw INSERT
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        for i in range(5):
            await db.execute(
                "INSERT INTO analyses (ticker, analysis_date, recommendation, market_mode) "
                "VALUES (?, ?, ?, ?)",
                ("TITAN", f"2026-0{i+1}-01", "BUY", "normal"),
            )
        await db.commit()

    rows = await get_analysis_history(db_path, "TITAN", limit=3)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# list_recommendations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recommendations_filters_by_type(db_path):
    """list_recommendations should only return rows matching the given recommendation."""
    for ticker, rec in [("STOCK1", "BUY"), ("STOCK2", "WATCHLIST"), ("STOCK3", "BUY"), ("STOCK4", "REJECT")]:
        state = _make_minimal_state(ticker)
        state.recommendation_type = rec
        await save_analysis(db_path, state)

    buys = await list_recommendations(db_path, "BUY")
    assert len(buys) == 2
    assert all(r["recommendation"] == "BUY" for r in buys)

    watchlist = await list_recommendations(db_path, "WATCHLIST")
    assert len(watchlist) == 1


# ---------------------------------------------------------------------------
# get_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_ordering(db_path):
    """get_summary should order: BUY → WATCHLIST → PEER_SWITCH → REJECT."""
    for ticker, rec in [
        ("R1", "REJECT"), ("B1", "BUY"), ("W1", "WATCHLIST"), ("B2", "BUY")
    ]:
        state = _make_minimal_state(ticker)
        state.recommendation_type = rec
        await save_analysis(db_path, state)

    rows = await get_summary(db_path)
    recs = [r["recommendation"] for r in rows]

    # All BUYs should come before WATCHLISTs and REJECTs
    buy_indices = [i for i, r in enumerate(recs) if r == "BUY"]
    watchlist_indices = [i for i, r in enumerate(recs) if r == "WATCHLIST"]
    reject_indices = [i for i, r in enumerate(recs) if r == "REJECT"]

    assert max(buy_indices) < min(watchlist_indices)
    assert max(watchlist_indices) < min(reject_indices)


# ---------------------------------------------------------------------------
# save_snapshot / data_snapshots table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_retrieve_snapshot(db_path):
    """save_snapshot should persist raw data as JSON."""
    import aiosqlite
    from datetime import date

    today = date.today().isoformat()
    payload = {"cmp": 2850.0, "market_cap_cr": 19000.0, "is_stale": False}

    await save_snapshot(db_path, "RELIANCE", today, "quote", payload, "nse")

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT data_json, source FROM data_snapshots WHERE ticker = 'RELIANCE'"
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    data = json.loads(row[0])
    assert data["cmp"] == 2850.0
    assert row[1] == "nse"


@pytest.mark.asyncio
async def test_snapshot_upsert_on_same_day(db_path):
    """Saving snapshot for same ticker/date/type twice should update, not duplicate."""
    import aiosqlite
    from datetime import date

    today = date.today().isoformat()
    await save_snapshot(db_path, "TCS", today, "financials", {"roe": 25.0}, "screener")
    await save_snapshot(db_path, "TCS", today, "financials", {"roe": 26.5}, "screener")  # update

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), data_json FROM data_snapshots "
            "WHERE ticker = 'TCS' AND data_type = 'financials'"
        ) as cur:
            count, data_json = await cur.fetchone()

    assert count == 1
    assert json.loads(data_json)["roe"] == 26.5


@pytest.mark.asyncio
async def test_snapshot_different_data_types_stored_separately(db_path):
    """Different data_type values for the same ticker should be separate rows."""
    import aiosqlite
    from datetime import date

    today = date.today().isoformat()
    await save_snapshot(db_path, "HDFC", today, "quote", {"cmp": 1700.0}, "nse")
    await save_snapshot(db_path, "HDFC", today, "financials", {"roe": 18.0}, "screener")
    await save_snapshot(db_path, "HDFC", today, "governance", {"pledging_pct": 0.0}, "bse")

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM data_snapshots WHERE ticker = 'HDFC'"
        ) as cur:
            (count,) = await cur.fetchone()

    assert count == 3


# ---------------------------------------------------------------------------
# WAL journal mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_enables_wal_journal_mode(db_path):
    """init_db must switch the database to WAL so concurrent scan workers
    don't hit 'database is locked' (which spuriously trips ER-07)."""
    import aiosqlite

    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA journal_mode") as cur:
            (mode,) = await cur.fetchone()
    assert mode.lower() == "wal"
