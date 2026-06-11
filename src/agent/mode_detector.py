"""Market mode detection based on Nifty 50 vs 52-week high."""
from __future__ import annotations

import time

from src.api.cache import data_cache
from src.api.nse import NSEClient
from src.api.yfinance_client import YFinanceClient
from src.logging_config import get_logger
from src.models import AnalysisState, MarketMode

log = get_logger("mode_detector")

# Module-level cache for Nifty mode — valid for 15 minutes.
# Avoids a fresh HTTP call on every analysis during batch scans.
_MODE_CACHE_TTL = 900  # 15 minutes
_cached_mode: MarketMode | None = None
_cached_nifty_level: float | None = None
_cached_nifty_peak: float | None = None
_cached_decline_pct: float | None = None
_cache_expires_at: float = 0.0


def reset_mode_cache() -> None:
    """Reset both the module-level and DataCache Nifty entries. Intended for use in tests."""
    global _cached_mode, _cached_nifty_level, _cached_nifty_peak
    global _cached_decline_pct, _cache_expires_at
    _cached_mode = None
    _cached_nifty_level = None
    _cached_nifty_peak = None
    _cached_decline_pct = None
    _cache_expires_at = 0.0
    data_cache.invalidate(data_cache.nifty_key())


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

    Results are cached for 15 minutes to avoid redundant HTTP calls during
    batch scans where hundreds of tickers are analysed in sequence.

    Tries NSE India first; falls back to Yahoo Finance (^NSEI) if NSE returns
    403 or any other error. Only marks MODE UNCONFIRMED if both sources fail.

    Args:
        nse_client: Initialised NSEClient (session already active via context manager).
        state: Mutable AnalysisState; nifty fields populated in-place.

    Returns:
        Detected MarketMode.
    """
    global _cached_mode, _cached_nifty_level, _cached_nifty_peak
    global _cached_decline_pct, _cache_expires_at

    # ── 0. Return from cache if still fresh ────────────────────────────────
    now = time.monotonic()
    if _cached_mode is not None and now < _cache_expires_at:
        state.nifty_level = _cached_nifty_level
        state.nifty_52w_high = _cached_nifty_peak
        state.nifty_decline_pct = _cached_decline_pct
        log.debug(
            "nifty_mode_cache_hit",
            mode=_cached_mode.value,
            expires_in_s=round(_cache_expires_at - now),
        )
        return _cached_mode

    # ── 0b. Secondary check: DataCache (survives across pipeline instances) ─
    _cached_nifty = data_cache.get(data_cache.nifty_key())
    if _cached_nifty is not None:
        mode = MarketMode(_cached_nifty["mode"])
        state.nifty_level = _cached_nifty["current"]
        state.nifty_52w_high = _cached_nifty["peak"]
        state.nifty_decline_pct = _cached_nifty["decline_pct"]
        _cached_mode = mode
        _cached_nifty_level = _cached_nifty["current"]
        _cached_nifty_peak = _cached_nifty["peak"]
        _cached_decline_pct = _cached_nifty["decline_pct"]
        _cache_expires_at = now + _MODE_CACHE_TTL
        log.debug("nifty_mode_data_cache_hit", mode=mode.value)
        return mode

    # ── 1. Try NSE ──────────────────────────────────────────────────────────
    try:
        current, peak = await nse_client.get_nifty50()
        if peak == 0:
            raise ValueError("Peak Nifty value is zero")

        mode, decline_pct = _classify_mode(current, peak)
        state.nifty_level = current
        state.nifty_52w_high = peak
        state.nifty_decline_pct = decline_pct
        _cached_mode = mode
        _cached_nifty_level = current
        _cached_nifty_peak = peak
        _cached_decline_pct = decline_pct
        _cache_expires_at = time.monotonic() + _MODE_CACHE_TTL
        data_cache.set(
            data_cache.nifty_key(),
            {"mode": mode.value, "current": current, "peak": peak, "decline_pct": decline_pct},
            _MODE_CACHE_TTL,
        )
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
        _cached_mode = mode
        _cached_nifty_level = current
        _cached_nifty_peak = peak
        _cached_decline_pct = decline_pct
        _cache_expires_at = time.monotonic() + _MODE_CACHE_TTL
        data_cache.set(
            data_cache.nifty_key(),
            {"mode": mode.value, "current": current, "peak": peak, "decline_pct": decline_pct},
            _MODE_CACHE_TTL,
        )
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
