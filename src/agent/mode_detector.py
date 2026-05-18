"""Market mode detection based on Nifty 50 vs 52-week high."""
from __future__ import annotations

from src.api.nse import NSEClient
from src.logging_config import get_logger
from src.models import AnalysisState, MarketMode

log = get_logger("mode_detector")


async def detect_mode(nse_client: NSEClient, state: AnalysisState) -> MarketMode:
    """Detect market mode based on Nifty 50 vs its 52-week high.

    Mode thresholds (per SKILL.md Section 2):
        decline% <  8%  → NORMAL
        8% ≤ decline% < 15% → CORRECTION
        decline% ≥ 15% → MAXIMUM_OPPORTUNITY

    Args:
        nse_client: Initialised NSEClient (session already active via context manager).
        state: Mutable AnalysisState; nifty fields populated in-place.

    Returns:
        Detected MarketMode.
    """
    try:
        current, peak = await nse_client.get_nifty50()
        if peak == 0:
            raise ValueError("Peak Nifty value is zero")

        decline_pct = (peak - current) / peak * 100

        state.nifty_level = current
        state.nifty_52w_high = peak
        state.nifty_decline_pct = round(decline_pct, 2)

        log.info(
            "nifty_mode_computed",
            current=current,
            peak=peak,
            decline_pct=round(decline_pct, 2),
        )

        if decline_pct >= 15.0:
            mode = MarketMode.MAXIMUM_OPPORTUNITY
        elif decline_pct >= 8.0:
            mode = MarketMode.CORRECTION
        else:
            mode = MarketMode.NORMAL

        log.info("market_mode_detected", mode=mode.value, decline_pct=round(decline_pct, 2))
        return mode

    except Exception as exc:
        log.warning(
            "mode_detection_failed",
            error=str(exc),
            error_tag="ER-08",
            fallback_mode=MarketMode.NORMAL.value,
        )
        state.add_error("ER-08")
        state.add_flag("[MODE UNCONFIRMED — NIFTY DATA UNAVAILABLE]")
        return MarketMode.NORMAL
