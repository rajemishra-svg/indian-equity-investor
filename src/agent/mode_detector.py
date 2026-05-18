"""Market mode detection based on Nifty 50 vs 52-week high."""
from __future__ import annotations

from src.api.nse import NSEClient
from src.api.yfinance_client import YFinanceClient
from src.logging_config import get_logger
from src.models import AnalysisState, MarketMode

log = get_logger("mode_detector")


def _classify_mode(current: float, peak: float) -> tuple[MarketMode, float]:
    """Compute decline % and return the corresponding MarketMode."""
    decline_pct = (peak - current) / peak * 100
    if decline_pct >= 15.0:
        mode = MarketMode.MAXIMUM_OPPORTUNITY
    elif decline_pct >= 8.0:
        mode = MarketMode.CORRECTION
    else:
        mode = MarketMode.NORMAL
    return mode, round(decline_pct, 2)


async def detect_mode(nse_client: NSEClient, state: AnalysisState) -> MarketMode:
    """Detect market mode based on Nifty 50 vs its 52-week high.

    Mode thresholds (per SKILL.md Section 2):
        decline% <  8%  → NORMAL
        8% ≤ decline% < 15% → CORRECTION
        decline% ≥ 15% → MAXIMUM_OPPORTUNITY

    Tries NSE India first; falls back to Yahoo Finance (^NSEI) if NSE returns
    403 or any other error. Only marks MODE UNCONFIRMED if both sources fail.

    Args:
        nse_client: Initialised NSEClient (session already active via context manager).
        state: Mutable AnalysisState; nifty fields populated in-place.

    Returns:
        Detected MarketMode.
    """
    # ── 1. Try NSE ──────────────────────────────────────────────────────────
    try:
        current, peak = await nse_client.get_nifty50()
        if peak == 0:
            raise ValueError("Peak Nifty value is zero")

        mode, decline_pct = _classify_mode(current, peak)
        state.nifty_level = current
        state.nifty_52w_high = peak
        state.nifty_decline_pct = decline_pct
        log.info(
            "nifty_mode_computed",
            source="nse",
            current=current,
            peak=peak,
            decline_pct=decline_pct,
        )
        log.info("market_mode_detected", mode=mode.value, decline_pct=decline_pct)
        return mode

    except Exception as nse_exc:
        log.warning(
            "nse_nifty50_failed_trying_yfinance",
            error=str(nse_exc),
            error_tag="ER-08",
        )

    # ── 2. Fallback: Yahoo Finance (^NSEI) ───────────────────────────────────
    try:
        yf_client = YFinanceClient()
        current, peak = await yf_client.get_nifty50()
        if peak == 0:
            raise ValueError("Peak Nifty value is zero from Yahoo Finance")

        mode, decline_pct = _classify_mode(current, peak)
        state.nifty_level = current
        state.nifty_52w_high = peak
        state.nifty_decline_pct = decline_pct
        state.add_flag("[NIFTY DATA SOURCE: Yahoo Finance (NSE blocked) — 15-20 min delay]")
        log.info(
            "nifty_mode_computed",
            source="yfinance",
            current=current,
            peak=peak,
            decline_pct=decline_pct,
        )
        log.info("market_mode_detected", mode=mode.value, decline_pct=decline_pct)
        return mode

    except Exception as yf_exc:
        log.warning(
            "mode_detection_failed",
            nse_error="see above",
            yfinance_error=str(yf_exc),
            error_tag="ER-08",
            fallback_mode=MarketMode.NORMAL.value,
        )
        state.add_error("ER-08")
        state.add_flag("[MODE UNCONFIRMED — NIFTY DATA UNAVAILABLE]")
        return MarketMode.NORMAL
