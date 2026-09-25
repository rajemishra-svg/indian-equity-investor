"""Price-history indicators shared by every quote source (Step 6 inputs).

Pure functions over oldest-first lists so yfinance (pandas) and Breeze (JSON
rows) compute RSI / 200-DMA / 52W range identically.  Every function returns
None when history is too short rather than a misleading partial value.
"""
from __future__ import annotations

TRADING_DAYS_1Y = 252


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over ``period`` bars. Needs at least ``period + 1`` closes."""
    if len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_dma(closes: list[float], window: int = 200) -> float | None:
    """Simple moving average of the last ``window`` closes; None if history is shorter."""
    if len(closes) < window:
        return None
    return round(sum(closes[-window:]) / window, 2)


def compute_52w_range(
    highs: list[float], lows: list[float]
) -> tuple[float | None, float | None]:
    """(52W high, 52W low) over the last 252 trading days of highs/lows."""
    recent_highs = highs[-TRADING_DAYS_1Y:]
    recent_lows = lows[-TRADING_DAYS_1Y:]
    return (
        max(recent_highs) if recent_highs else None,
        min(recent_lows) if recent_lows else None,
    )
