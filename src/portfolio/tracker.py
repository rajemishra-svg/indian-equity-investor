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
    consume_holdings_fifo as _db_consume_holdings_fifo,
)
from src.db.repository import (
    get_holdings as _db_get_holdings,
)
from src.db.repository import (
    get_transactions as _db_get_transactions,
)
from src.logging_config import get_logger

# Indian LTCG rule: equity gains qualify as long-term after 12 months.
_LTCG_HOLDING_DAYS = 365


def add_one_year(d: date) -> date:
    """Return the date one calendar year later, leap-safe (Feb 29 → Feb 28)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # Feb 29 in a leap year
        return d.replace(year=d.year + 1, day=28)


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
    # Sells — FIFO lot consumption with realized P&L
    # ------------------------------------------------------------------

    async def record_sell(
        self,
        ticker: str,
        price: float,
        quantity: int,
        txn_date: date,
        notes: str = "",
    ) -> dict:
        """Sell ``quantity`` shares FIFO: consume lots, log the transaction,
        and return realized P&L split into LTCG and STCG.

        Lots are consumed oldest-first; exhausted lots disappear from holdings
        (and their tax-tracker rows are removed), a partially sold lot keeps
        its remaining quantity.  The SELL transaction is logged only after the
        lots are successfully consumed.

        Returns::

            {
              "lots": [{"purchase_date", "avg_cost", "quantity_consumed",
                        "lot_exhausted", "gain", "is_ltcg"}, ...],
              "realized_gain": float,   # total, ₹
              "ltcg_gain": float,       # held ≥ 1 year
              "stcg_gain": float,       # held < 1 year
              "remaining_qty": int,     # still held after the sale
            }

        Raises:
            ValueError: when fewer than ``quantity`` shares are held —
                nothing is recorded in that case.
        """
        ticker = ticker.upper()
        consumed = await _db_consume_holdings_fifo(
            self.db_path, self.user_id, ticker, quantity
        )
        await self.add_transaction(
            ticker=ticker,
            action="SELL",
            price=price,
            quantity=quantity,
            txn_date=txn_date,
            notes=notes,
        )

        lots: list[dict] = []
        ltcg_gain = 0.0
        stcg_gain = 0.0
        for slice_ in consumed:
            purchase = date.fromisoformat(slice_["purchase_date"])
            is_ltcg = (txn_date - purchase).days >= _LTCG_HOLDING_DAYS
            gain = round((price - slice_["avg_cost"]) * slice_["quantity_consumed"], 2)
            if is_ltcg:
                ltcg_gain += gain
            else:
                stcg_gain += gain
            lots.append({**slice_, "gain": gain, "is_ltcg": is_ltcg})

        remaining_qty = sum(
            h["quantity"] for h in await self.get_holdings() if h["ticker"] == ticker
        )

        result = {
            "lots": lots,
            "realized_gain": round(ltcg_gain + stcg_gain, 2),
            "ltcg_gain": round(ltcg_gain, 2),
            "stcg_gain": round(stcg_gain, 2),
            "remaining_qty": remaining_qty,
        }
        self.log.info(
            "sell_recorded",
            user=self.user_id,
            ticker=ticker,
            price=price,
            quantity=quantity,
            realized_gain=result["realized_gain"],
            ltcg_gain=result["ltcg_gain"],
            stcg_gain=result["stcg_gain"],
            remaining_qty=remaining_qty,
        )
        return result

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
