"""Portfolio state management — async DB-backed per-user operations.

Each user's holdings, transactions, and tax records are stored in the shared
SQLite database (investor.db) under their own user_id.  The default user_id
comes from settings.investor_user (env var INVESTOR_USER, default 'default').

Watchlist and rejection data are stored separately in the analyses table —
use src.db.repository for those queries.
"""
from __future__ import annotations

from datetime import date

from src.config import settings
from src.db.repository import (
    add_holding as _db_add_holding,
)
from src.db.repository import (
    add_tax_entry as _db_add_tax_entry,
)
from src.db.repository import (
    add_transaction as _db_add_transaction,
)
from src.db.repository import (
    get_holdings as _db_get_holdings,
)
from src.db.repository import (
    get_transactions as _db_get_transactions,
)
from src.logging_config import get_logger


class PortfolioTracker:
    """Async DB-backed portfolio tracker.  All methods are coroutines."""

    def __init__(
        self,
        db_path: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.db_path = db_path or settings.db_path
        self.user_id = user_id or settings.investor_user
        self.log = get_logger("portfolio")

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------

    async def add_holding(
        self,
        ticker: str,
        avg_cost: float,
        quantity: int,
        purchase_date: date,
        allocation_pct: float = 0.0,
        company_name: str = "",
    ) -> None:
        """Insert a new holding row for this user."""
        await _db_add_holding(
            db_path=self.db_path,
            user_id=self.user_id,
            ticker=ticker,
            company_name=company_name or ticker,
            avg_cost=avg_cost,
            quantity=quantity,
            purchase_date=purchase_date.isoformat(),
            allocation_pct=allocation_pct,
        )
        self.log.info(
            "holding_added",
            user=self.user_id,
            ticker=ticker,
            avg_cost=avg_cost,
            quantity=quantity,
        )

    async def get_holdings(self) -> list[dict]:
        """Return all holdings for this user."""
        return await _db_get_holdings(self.db_path, self.user_id)

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def add_transaction(
        self,
        ticker: str,
        action: str,
        price: float,
        quantity: int,
        txn_date: date,
        notes: str = "",
    ) -> None:
        """Insert a transaction row for this user."""
        await _db_add_transaction(
            db_path=self.db_path,
            user_id=self.user_id,
            ticker=ticker,
            action=action.upper(),
            price=price,
            quantity=quantity,
            txn_date=txn_date.isoformat(),
            notes=notes,
        )
        self.log.info(
            "transaction_added",
            user=self.user_id,
            ticker=ticker,
            action=action.upper(),
            price=price,
            quantity=quantity,
        )

    async def get_transactions(self, ticker: str | None = None) -> list[dict]:
        """Return transactions for this user, optionally filtered by ticker."""
        return await _db_get_transactions(self.db_path, self.user_id, ticker)

    # ------------------------------------------------------------------
    # Tax tracker
    # ------------------------------------------------------------------

    async def add_tax_entry(
        self,
        ticker: str,
        purchase_date: date,
        ltcg_date: date,
        avg_cost: float = 0.0,
    ) -> None:
        """Insert an LTCG eligibility row for this user."""
        await _db_add_tax_entry(
            db_path=self.db_path,
            user_id=self.user_id,
            ticker=ticker,
            purchase_date=purchase_date.isoformat(),
            ltcg_date=ltcg_date.isoformat(),
            avg_cost=avg_cost,
        )
        self.log.info(
            "tax_entry_added",
            user=self.user_id,
            ticker=ticker,
            purchase_date=purchase_date.isoformat(),
            ltcg_date=ltcg_date.isoformat(),
        )
