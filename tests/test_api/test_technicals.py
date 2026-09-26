"""Tests for shared price-history indicators and the sources that use them."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.api.breeze_client import _parse_history
from src.api.technicals import compute_52w_range, compute_dma, compute_rsi
from src.api.yfinance_client import YFinanceClient, _history_technicals

# ---------------------------------------------------------------------------
# compute_rsi
# ---------------------------------------------------------------------------


def test_rsi_needs_period_plus_one_closes():
    assert compute_rsi([100.0] * 14) is None
    assert compute_rsi([100.0] * 15) is not None


def test_rsi_only_gains_is_100():
    assert compute_rsi([float(i) for i in range(1, 31)]) == 100.0


def test_rsi_only_losses_is_0():
    assert compute_rsi([float(i) for i in range(30, 0, -1)]) == 0.0


def test_rsi_flat_series_is_neutral():
    assert compute_rsi([100.0] * 20) == 50.0


def test_rsi_alternating_equal_moves_is_near_50():
    closes = [100.0 + (i % 2) for i in range(40)]
    assert compute_rsi(closes) == pytest.approx(50.0, abs=5.0)


def test_rsi_steady_decline_reads_oversold():
    # Mostly down days with small bounces → RSI well below 40
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.005 if i % 4 == 0 else 0.99))
    assert compute_rsi(closes) < 40.0


# ---------------------------------------------------------------------------
# compute_dma / compute_52w_range
# ---------------------------------------------------------------------------


def test_dma_requires_full_window():
    assert compute_dma([1.0] * 199) is None
    assert compute_dma([1.0] * 100 + [3.0] * 200) == 3.0


def test_52w_range_uses_last_252_bars_only():
    highs = [999.0] * 50 + [110.0] * 252   # a spike 14 months ago must not count
    lows = [1.0] * 50 + [90.0] * 252
    assert compute_52w_range(highs, lows) == (110.0, 90.0)


def test_52w_range_empty_is_none():
    assert compute_52w_range([], []) == (None, None)


# ---------------------------------------------------------------------------
# yfinance history → technicals
# ---------------------------------------------------------------------------


def _daily_hist(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000.0] * len(closes),
        },
        index=pd.date_range("2025-01-01", periods=len(closes), freq="B"),
    )


def test_history_technicals_computes_all_fields():
    closes = [100.0 + i * 0.1 for i in range(252)]
    tech = _history_technicals(_daily_hist(closes))
    assert tech["rsi_14"] == 100.0
    assert tech["dma_200"] == pytest.approx(sum(closes[-200:]) / 200, abs=0.01)
    assert tech["w52_high"] == pytest.approx(closes[-1] * 1.01)
    assert tech["w52_low"] == pytest.approx(closes[0] * 0.99)


def test_history_technicals_non_dataframe_is_empty():
    assert _history_technicals(None) == {}
    assert _history_technicals(MagicMock()) == {}


@pytest.mark.asyncio
async def test_quote_fills_rsi_dma_and_range_from_history():
    """fast_info with no range/DMA → values come from the 1Y history."""
    closes = [200.0 - i * 0.2 for i in range(252)]
    fi = MagicMock()
    fi.get = lambda key, default=None: {"lastPrice": closes[-1]}.get(key, default)
    ticker = MagicMock()
    ticker.fast_info = fi
    ticker.history = MagicMock(return_value=_daily_hist(closes))

    with patch("src.api.yfinance_client.yf.Ticker", return_value=ticker):
        quote = await YFinanceClient().get_stock_quote("TCS")

    ticker.history.assert_called_once_with(period="1y", interval="1d")
    assert quote is not None
    assert quote.rsi_14 == 0.0
    assert quote.dma_200 is not None
    assert quote.w52_high == pytest.approx(200.0 * 1.01)
    assert quote.w52_low == pytest.approx(closes[-1] * 0.99)


@pytest.mark.asyncio
async def test_get_price_technicals_returns_empty_on_failure():
    with patch("src.api.yfinance_client.yf.Ticker", side_effect=RuntimeError("boom")):
        assert await YFinanceClient().get_price_technicals("TCS") == {}


# ---------------------------------------------------------------------------
# Breeze history parsing
# ---------------------------------------------------------------------------


def _breeze_rows(closes: list[float]) -> list[dict]:
    return [
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
        for c in closes
    ]


def test_breeze_history_has_rsi_and_true_52w_window():
    closes = [500.0] * 30 + [100.0 + i for i in range(252)]
    m = _parse_history(_breeze_rows(closes))
    assert m["rsi_14"] == 100.0
    assert m["w52_high"] == pytest.approx(351.0 * 1.01)   # old 500 spike excluded
    assert m["w52_low"] == pytest.approx(100.0 * 0.99)


def test_breeze_dma_none_with_short_history():
    assert _parse_history(_breeze_rows([100.0] * 120))["dma_200"] is None
