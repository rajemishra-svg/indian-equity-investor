"""Async SQLite repository for analysis results and raw data snapshots.

Uses ``aiosqlite`` for non-blocking I/O so it can be awaited inside the
asyncio pipeline without blocking the event loop.

All public coroutines are module-level functions (not class methods) so they
can be imported and called directly by the pipeline::

    from src.db.repository import save_analysis, save_snapshot, init_db
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Optional

import aiosqlite

from src.db.schema import CREATE_TABLES_SQL
from src.models import AnalysisState


# ---------------------------------------------------------------------------
# Public class (thin wrapper for backwards-compat; pipeline uses free functions)
# ---------------------------------------------------------------------------


class AnalysisRepository:
    """Thin OO wrapper — primarily for use in CLI commands."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        await init_db(self.db_path)

    async def save(self, state: AnalysisState) -> None:
        await save_analysis(self.db_path, state)

    async def get_latest(self, ticker: str) -> Optional[dict]:
        return await get_latest_analysis(self.db_path, ticker)

    async def history(self, ticker: str, limit: int = 10) -> list[dict]:
        return await get_analysis_history(self.db_path, ticker, limit)

    async def by_recommendation(self, recommendation: str = "BUY") -> list[dict]:
        return await list_recommendations(self.db_path, recommendation)

    async def summary(self) -> list[dict]:
        return await get_summary(self.db_path)


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------


async def init_db(db_path: str) -> None:
    """Create tables and indexes if they do not yet exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def save_analysis(db_path: str, state: AnalysisState) -> None:
    """Upsert an analysis result row.

    Uses ``INSERT OR REPLACE`` to honour the ``UNIQUE(ticker, analysis_date)``
    constraint — running the same analysis twice in a day overwrites the
    previous result.
    """
    await init_db(db_path)

    today = date.today().isoformat()

    cmp = state.quote.cmp if state.quote else None
    market_cap_cr = state.quote.market_cap_cr if state.quote else None

    pre_screen_score = state.pre_screen.score if state.pre_screen else None
    pre_screen_gate = state.pre_screen.gate.value if state.pre_screen else None

    gov_score = state.governance.score if state.governance else None
    gov_gate = state.governance.gate.value if state.governance else None
    gov_sub_scores = (
        json.dumps(state.governance.sub_scores) if state.governance else None
    )
    gov_triggers = (
        json.dumps(state.governance.immediate_triggers) if state.governance else None
    )

    fin_score = state.financial_gate.score if state.financial_gate else None
    fin_gate = state.financial_gate.gate.value if state.financial_gate else None
    fin_triggers = (
        json.dumps(state.financial_gate.hard_triggers_fired) if state.financial_gate else None
    )

    val_gate = state.valuation.gate.value if state.valuation else None
    val_methods = state.valuation.methods_in_buy_zone if state.valuation else None
    mos_pct = state.valuation.margin_of_safety_pct if state.valuation else None
    required_mos_pct = state.valuation.required_mos_pct if state.valuation else None
    dcf_intrinsic_weighted = (
        state.valuation.dcf_intrinsic_weighted if state.valuation else None
    )

    watchlist_tier = int(state.watchlist_tier) if state.watchlist_tier else None
    conviction = state.conviction.value if state.conviction else None

    # P2-2: Compute DCF-derived target buy price for WATCHLIST entries.
    # target = DCF intrinsic × (1 - required MoS %) — the price at which the
    # required safety margin is just met.  Stored so watchlist-alerts can compare
    # live CMP without re-running the full pipeline.
    target_buy_price: Optional[float] = None
    if (
        state.recommendation_type == "WATCHLIST"
        and state.valuation
        and state.valuation.dcf_intrinsic_weighted
        and state.valuation.required_mos_pct
    ):
        target_buy_price = round(
            state.valuation.dcf_intrinsic_weighted
            * (1 - state.valuation.required_mos_pct / 100),
            2,
        )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO analyses (
                ticker, company_name, analysis_date, market_mode, sector_name, cap_size,
                cmp, market_cap_cr,
                pre_screen_score, pre_screen_gate,
                governance_score, governance_gate, governance_sub_scores, governance_triggers,
                financial_score, financial_gate, financial_triggers,
                valuation_gate, valuation_methods_in_buy_zone, mos_pct, required_mos_pct,
                dcf_intrinsic_weighted,
                terminated_at_step, termination_reason, recommendation, conviction,
                watchlist_tier, target_buy_price, investment_thesis,
                all_data_flags, error_tags
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
            """,
            (
                state.ticker,
                state.company_name or None,
                today,
                state.mode.value,
                state.sector_name,
                state.cap_size,
                cmp,
                market_cap_cr,
                pre_screen_score,
                pre_screen_gate,
                gov_score,
                gov_gate,
                gov_sub_scores,
                gov_triggers,
                fin_score,
                fin_gate,
                fin_triggers,
                val_gate,
                val_methods,
                mos_pct,
                required_mos_pct,
                dcf_intrinsic_weighted,
                state.terminated_at_step,
                state.termination_reason,
                state.recommendation_type,
                conviction,
                watchlist_tier,
                target_buy_price,
                state.investment_thesis,
                json.dumps(state.all_data_flags),
                json.dumps(state.error_tags),
            ),
        )
        await db.commit()


async def save_snapshot(
    db_path: str,
    ticker: str,
    snapshot_date: str,
    data_type: str,
    data: Any,
    source: str,
) -> None:
    """Upsert a raw data snapshot row.

    Args:
        db_path: Path to the SQLite database file.
        ticker: NSE ticker symbol.
        snapshot_date: ISO date string (YYYY-MM-DD).
        data_type: One of ``'quote'``, ``'financials'``, ``'governance'``, ``'valuation'``.
        data: Any JSON-serialisable object (dict, list, etc.).
        source: Data source identifier (e.g. ``'nse'``, ``'screener'``).
    """
    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO data_snapshots
                (ticker, snapshot_date, data_type, source, data_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticker, snapshot_date, data_type, source, json.dumps(data)),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def get_fresh_snapshot(
    db_path: str,
    ticker: str,
    data_type: str,
    max_age_hours: int = 24,
) -> Optional[dict]:
    """Return the most recent snapshot for ``ticker`` / ``data_type`` if it is
    fresher than ``max_age_hours``, otherwise ``None``.

    Used by the batch scanner warm-cache layer to avoid redundant Screener
    requests across scan runs.

    Args:
        db_path: Path to the SQLite database file.
        ticker: NSE ticker symbol (case-insensitive).
        data_type: ``'financials'``, ``'governance'``, ``'quote'``, or ``'valuation'``.
        max_age_hours: Maximum acceptable age in hours. Default 24 h (matches
            the in-memory ``cache_ttl_financials``). Use a larger value (e.g.
            168 = 7 days) for slow-changing quarterly data.

    Returns:
        Parsed dict from the stored JSON, or ``None`` if no fresh row found.
    """
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT data_json, created_at
            FROM data_snapshots
            WHERE ticker = ?
              AND data_type = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ticker.upper(), data_type),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return None

    # Parse the stored ISO timestamp and compare age
    raw_ts: str = row["created_at"]  # e.g. "2026-05-18 09:30:00"
    try:
        stored_at = datetime.fromisoformat(raw_ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return None  # unparseable timestamp — treat as stale

    age_hours = (datetime.now(timezone.utc) - stored_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        return None  # too old

    try:
        return json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError):
        return None


async def get_latest_analysis(db_path: str, ticker: str) -> Optional[dict]:
    """Return the most recent analysis row for ``ticker``, or ``None``."""
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM analyses
            WHERE ticker = ?
            ORDER BY analysis_date DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_dict(row)


async def get_analysis_history(
    db_path: str, ticker: str, limit: int = 10
) -> list[dict]:
    """Return the most recent ``limit`` analysis rows for ``ticker``."""
    await init_db(db_path)
    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM analyses
            WHERE ticker = ?
            ORDER BY analysis_date DESC
            LIMIT ?
            """,
            (ticker.upper(), limit),
        ) as cursor:
            async for row in cursor:
                rows.append(_row_to_dict(row))
    return rows


async def list_recommendations(
    db_path: str, recommendation: str = "BUY"
) -> list[dict]:
    """Return all analyses with the given recommendation, newest first."""
    await init_db(db_path)
    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM analyses
            WHERE recommendation = ?
            ORDER BY analysis_date DESC, mos_pct DESC
            """,
            (recommendation.upper(),),
        ) as cursor:
            async for row in cursor:
                rows.append(_row_to_dict(row))
    return rows


async def get_watchlist_with_targets(db_path: str) -> list[dict]:
    """Return the latest WATCHLIST analysis for every ticker that has one.

    Includes ``target_buy_price``, ``cmp`` (at time of analysis), ``dcf_intrinsic_weighted``,
    and ``required_mos_pct``.  Used by ``investor watchlist-alerts`` to compare live
    CMP against the stored DCF target without re-running the full pipeline.
    """
    await init_db(db_path)
    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                ticker, company_name, analysis_date, watchlist_tier,
                cmp AS cmp_at_analysis,
                target_buy_price,
                dcf_intrinsic_weighted,
                required_mos_pct,
                mos_pct,
                sector_name,
                termination_reason
            FROM analyses
            WHERE recommendation = 'WATCHLIST'
              AND id IN (
                  SELECT MAX(id) FROM analyses
                  WHERE recommendation = 'WATCHLIST'
                  GROUP BY ticker
              )
            ORDER BY watchlist_tier ASC, analysis_date DESC
            """
        ) as cursor:
            async for row in cursor:
                rows.append(_row_to_dict(row))
    return rows


async def get_all_tracked_tickers(db_path: str) -> list[dict]:
    """Return the latest BUY and WATCHLIST analyses for all tickers.

    Used by the surveillance command to check all positions in one sweep.
    """
    await init_db(db_path)
    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                ticker, company_name, analysis_date, recommendation,
                watchlist_tier, target_buy_price, cmp AS cmp_at_analysis,
                dcf_intrinsic_weighted, required_mos_pct, mos_pct,
                governance_score, financial_score, sector_name, conviction
            FROM analyses
            WHERE recommendation IN ('BUY', 'WATCHLIST')
              AND id IN (
                  SELECT MAX(id) FROM analyses
                  WHERE recommendation IN ('BUY', 'WATCHLIST')
                  GROUP BY ticker
              )
            ORDER BY recommendation, analysis_date DESC
            """
        ) as cursor:
            async for row in cursor:
                rows.append(_row_to_dict(row))
    return rows


async def get_summary(db_path: str) -> list[dict]:
    """Return a summary of all analyses grouped by recommendation."""
    await init_db(db_path)
    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                ticker,
                company_name,
                analysis_date,
                recommendation,
                conviction,
                sector_name,
                cap_size,
                mos_pct,
                terminated_at_step
            FROM analyses
            ORDER BY
                CASE recommendation
                    WHEN 'BUY' THEN 1
                    WHEN 'WATCHLIST' THEN 2
                    WHEN 'PEER_SWITCH' THEN 3
                    ELSE 4
                END,
                analysis_date DESC
            """
        ) as cursor:
            async for row in cursor:
                rows.append(_row_to_dict(row))
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert an aiosqlite Row to a plain dict, deserialising JSON fields."""
    d = dict(row)
    for json_field in (
        "governance_sub_scores",
        "governance_triggers",
        "financial_triggers",
        "all_data_flags",
        "error_tags",
    ):
        raw = d.get(json_field)
        if isinstance(raw, str):
            try:
                d[json_field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return d
