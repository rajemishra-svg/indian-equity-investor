"""Watchlist entry evaluation — live CMP against a market-mode-adjusted buy target.

A watchlist entry's stored ``target_buy_price`` bakes in the margin of safety
required *on the analysis date*: a name analysed in a NORMAL market needs a
5–10pp deeper discount than the same name analysed during a correction
(``AnalysisState.required_mos_pct``).  When the market has since corrected,
the stored target is too strict and entries are flagged late — exactly when
good companies get cheap.  ``mode_adjusted_target`` re-prices the target for
the *current* market mode from the stored DCF value.

Growth-mode rows carry no target: their forward-revenue DCF is not trusted
for entry prices (see ``src/monitor/holdings.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import MarketMode

WATCHLIST_RECS = ("WATCHLIST", "GROWTH_WATCHLIST")
APPROACHING_PCT = 10.0  # within this % above target → "approaching"

# MoS concession (pp) granted per market mode — mirrors AnalysisState.required_mos_pct
_MODE_MOS_CONCESSION = {
    MarketMode.NORMAL.value: 0.0,
    MarketMode.CORRECTION.value: 5.0,
    MarketMode.MAXIMUM_OPPORTUNITY.value: 10.0,
}


@dataclass
class WatchlistStatus:
    ticker: str
    status: str                  # ENTER ZONE | APPROACHING | MONITORING | NO TARGET | NO PRICE
    target: float | None = None
    gap_pct: float | None = None  # (live − target) / target; ≤ 0 means in zone
    required_mos_pct: float | None = None
    note: str | None = None


def _concession(mode: str | None) -> float:
    return _MODE_MOS_CONCESSION.get(mode or MarketMode.NORMAL.value, 0.0)


def mode_adjusted_target(
    row: dict, current_mode: MarketMode | None
) -> tuple[float | None, float | None]:
    """(target buy price, required MoS %) re-priced for ``current_mode``.

    Undoes the concession of the analysis-date mode and applies the current
    one.  Falls back to the stored target when the DCF or MoS is missing, or
    when the current mode is unknown.
    """
    dcf = row.get("dcf_intrinsic_weighted")
    stored_mos = row.get("required_mos_pct")
    if not dcf or dcf <= 0 or stored_mos is None or current_mode is None:
        return row.get("target_buy_price"), stored_mos
    required = stored_mos + _concession(row.get("market_mode")) - _concession(current_mode.value)
    required = max(required, 0.0)
    return round(dcf * (1 - required / 100), 2), required


def evaluate_watchlist_row(
    row: dict, live_cmp: float | None, current_mode: MarketMode | None
) -> WatchlistStatus:
    ticker = row.get("ticker", "")
    if row.get("analysis_mode") == "growth" or row.get("recommendation") == "GROWTH_WATCHLIST":
        return WatchlistStatus(
            ticker, "NO TARGET", note="Growth-mode DCF not used for entry targets"
        )
    target, required = mode_adjusted_target(row, current_mode)
    if not target:
        return WatchlistStatus(ticker, "NO TARGET", note="No DCF value in the latest analysis")
    if live_cmp is None:
        return WatchlistStatus(ticker, "NO PRICE", target=target, required_mos_pct=required)

    gap = round((live_cmp - target) / target * 100, 2)
    if gap <= 0:
        status = "ENTER ZONE"
    elif gap <= APPROACHING_PCT:
        status = "APPROACHING"
    else:
        status = "MONITORING"

    note = None
    stored = row.get("target_buy_price")
    if stored and abs(stored - target) > 0.005:
        note = (
            f"Target re-priced for {current_mode.value} market "
            f"(MoS {required:.0f}%; stored ₹{stored:,.2f})"
        )
    return WatchlistStatus(ticker, status, target, gap, required, note)


_STATUS_ORDER = {"ENTER ZONE": 0, "APPROACHING": 1, "MONITORING": 2, "NO PRICE": 3, "NO TARGET": 4}


def sort_statuses(statuses: list[WatchlistStatus]) -> list[WatchlistStatus]:
    """In-zone first, then closest to target, then untargeted."""
    return sorted(
        statuses,
        key=lambda s: (
            _STATUS_ORDER[s.status],
            s.gap_pct if s.gap_pct is not None else float("inf"),
            s.ticker,
        ),
    )
