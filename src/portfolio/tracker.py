"""Portfolio state management — reads and writes portfolio markdown files."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import List, Optional

from src.logging_config import get_logger
from src.models import AnalysisState


class PortfolioTracker:
    """Reads and writes the portfolio markdown files in portfolio/ directory."""

    HOLDINGS_FILE = "holdings.md"
    TRANSACTIONS_FILE = "transaction-log.md"
    EXIT_FILE = "exit-tracker.md"
    TAX_FILE = "tax-tracker.md"

    def __init__(self, portfolio_dir: Optional[Path] = None) -> None:
        self.portfolio_dir = portfolio_dir or Path("portfolio")
        self.log = get_logger("portfolio")

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------

    def add_holding(
        self,
        ticker: str,
        avg_cost: float,
        quantity: int,
        purchase_date: date,
        allocation_pct: float,
        company_name: str = "",
    ) -> None:
        """Append a new holding to holdings.md.

        Maintains the table format:
        | Ticker | Company | Avg Cost | Qty | Purchase Date | Allocation % |
        """
        path = self.portfolio_dir / self.HOLDINGS_FILE
        row = (
            f"| {ticker} | {company_name or ticker} | ₹{avg_cost:.2f} "
            f"| {quantity} | {purchase_date.isoformat()} | {allocation_pct:.1f}% |"
        )
        self._append_line(path, row)
        self.log.info(
            "holding_added",
            ticker=ticker,
            avg_cost=avg_cost,
            quantity=quantity,
        )

    def get_holdings(self) -> List[dict]:
        """Parse holdings.md and return list of holding dicts."""
        path = self.portfolio_dir / self.HOLDINGS_FILE
        if not path.exists():
            return []

        holdings = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line or "Ticker" in line:
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 6:
                try:
                    holdings.append(
                        {
                            "ticker": cells[0],
                            "company_name": cells[1],
                            "avg_cost": float(cells[2].replace("₹", "").replace(",", "")),
                            "quantity": int(cells[3]),
                            "purchase_date": cells[4],
                            "allocation_pct": float(cells[5].replace("%", "").strip()),
                        }
                    )
                except (ValueError, IndexError):
                    pass

        return holdings

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def add_transaction(
        self,
        ticker: str,
        action: str,
        price: float,
        quantity: int,
        txn_date: date,
        notes: str = "",
    ) -> None:
        """Append a transaction to transaction-log.md.

        Format: | Date | Ticker | Action | Price | Qty | Notes |
        """
        path = self.portfolio_dir / self.TRANSACTIONS_FILE
        row = (
            f"| {txn_date.isoformat()} | {ticker} | {action.upper()} "
            f"| ₹{price:.2f} | {quantity} | {notes} |"
        )
        self._append_line(path, row)
        self.log.info(
            "transaction_added",
            ticker=ticker,
            action=action,
            price=price,
            quantity=quantity,
        )

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def add_to_watchlist(
        self,
        ticker: str,
        tier: int,
        analysis_result: Optional[AnalysisState] = None,
        reason: str = "",
    ) -> None:
        """Append a ticker to the appropriate tier watchlist file.

        Format: | Ticker | Date Added | Reason | Pre-Screen Score | Notes |
        """
        tier_file = f"tier{tier}.md"
        path = Path("analysis") / "watchlist" / tier_file

        today = date.today().isoformat()
        pre_score = (
            f"{analysis_result.pre_screen.score}/9"
            if analysis_result and analysis_result.pre_screen
            else "N/A"
        )
        trigger = (
            analysis_result.termination_reason or reason
            if analysis_result
            else reason
        )
        row = f"| {ticker} | {today} | {trigger} | {pre_score} | - |"
        self._append_line(path, row)
        self.log.info(
            "watchlist_updated",
            ticker=ticker,
            tier=tier,
            reason=trigger,
        )

    # ------------------------------------------------------------------
    # Rejection tracker
    # ------------------------------------------------------------------

    def add_rejection(
        self,
        ticker: str,
        step: int,
        reasons: List[str],
        re_eval_condition: str,
    ) -> None:
        """Append a rejection to rejection-tracker.md.

        Format: | Date | Ticker | Step | Reasons | Re-Eval Condition |
        """
        path = Path("analysis") / "watchlist" / "rejection-tracker.md"
        today = date.today().isoformat()
        reasons_str = "; ".join(reasons)
        row = f"| {today} | {ticker} | Step {step} | {reasons_str} | {re_eval_condition} |"
        self._append_line(path, row)
        self.log.info(
            "rejection_logged",
            ticker=ticker,
            step=step,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # Tax tracker
    # ------------------------------------------------------------------

    def update_tax_tracker(
        self,
        ticker: str,
        purchase_date: date,
        ltcg_date: date,
        avg_cost: float = 0.0,
    ) -> None:
        """Append LTCG eligibility entry to tax-tracker.md.

        Format: | Ticker | Purchase Date | LTCG Eligible Date | Avg Cost |
        """
        path = self.portfolio_dir / self.TAX_FILE
        row = (
            f"| {ticker} | {purchase_date.isoformat()} "
            f"| {ltcg_date.isoformat()} | ₹{avg_cost:.2f} |"
        )
        self._append_line(path, row)
        self.log.info(
            "tax_tracker_updated",
            ticker=ticker,
            purchase_date=purchase_date.isoformat(),
            ltcg_date=ltcg_date.isoformat(),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_line(self, path: Path, line: str) -> None:
        """Append a line to a file, creating it if necessary."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Create with a minimal header for the table
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
