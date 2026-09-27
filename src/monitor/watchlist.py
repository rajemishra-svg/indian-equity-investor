"""Watchlist entry evaluation — live CMP against a market-mode-adjusted buy target.

A watchlist entry's stored ``target_buy_price`` bakes in the margin of safety
required *on the analysis date*: a name analysed in a NORMAL market needs a
5–10pp deeper discount than the same name analysed during a correction
(``AnalysisState.required_mos_pct``).  When the market has since corrected,
the stored target is too strict and entries are flagged late — exactly when
good companies get cheap.  ``mode_adjusted_target`` re-prices the target for
the *current* market mode from the stored DCF value.

Growth-mode rows are priced off the growth step's own forward-DCF MoS
(20%, or 30% when recently listed), which does not vary with market mode.
Growth rows saved before the growth-DCF unit fix carry no target
(``growth_dcf_trusted`` in ``src/monitor/holdings.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import MarketMode
from src.monitor.holdings import growth_dcf_trusted

WATCHLIST_RECS = ("WATCHLIST", "GROWTH_WATCHLIST")
APPROACHING_PCT = 10.0  # within this % above target → "approaching"

# Growth-mode forward-DCF MoS (Step 5G): mode-independent.
GROWTH_MOS_PCT = 20.0
GROWTH_MOS_RECENTLY_LISTED_PCT = 30.0
# Growth rows saved before Step 5G persisted its threshold carry this
# ValuationResult default instead of the MoS actually tested.
_LEGACY_DEFAULT_MOS_PCT = 35.0

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


def growth_target(row: dict) -> tuple[float | None, float | None]:
    """(target, MoS %) for a growth-mode row — no market-mode adjustment."""
    dcf = row.get("dcf_intrinsic_weighted")
    required = row.get("required_mos_pct")
    if required is None or required == _LEGACY_DEFAULT_MOS_PCT:
        required = (
            GROWTH_MOS_RECENTLY_LISTED_PCT
            if row.get("sector_name") == "recently_listed"
            else GROWTH_MOS_PCT
        )
    if not dcf or dcf <= 0:
        return None, required
    return round(dcf * (1 - required / 100), 2), required


def _is_growth(row: dict) -> bool:
    return row.get("analysis_mode") == "growth" or row.get("recommendation") == "GROWTH_WATCHLIST"


def evaluate_watchlist_row(
    row: dict, live_cmp: float | None, current_mode: MarketMode | None
) -> WatchlistStatus:
    ticker = row.get("ticker", "")
    growth = _is_growth(row)
    if growth:
        if not growth_dcf_trusted({**row, "analysis_mode": "growth"}):
            return WatchlistStatus(
                ticker, "NO TARGET", note="Growth DCF predates the valuation fix — re-analyse"
            )
        target, required = growth_target(row)
    else:
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
    if growth:
        note = f"Growth forward-DCF target (MoS {required:.0f}%, not mode-adjusted)"
    elif stored and abs(stored - target) > 0.005:
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
