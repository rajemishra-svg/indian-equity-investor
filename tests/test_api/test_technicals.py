"""Tests for shared price-history indicators and the sources that use them."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.api.breeze_client import _parse_history
from src.api.technicals import compute_52w_range, compute_dma, compute_rsi
from src.api.yfinance_client import YFinanceClient, _history_technicals
from src.models import StockQuote

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


def _live_quote(**overrides) -> StockQuote:
    base = dict(ticker="X", company_name="X", cmp=100.0, w52_high=120.0, w52_low=80.0,
                market_cap_cr=1000.0, dma_200=90.0, rsi_14=45.0,
                avg_daily_value_cr=12.5, volume_trend_down_days="stable")
    base.update(overrides)
    return StockQuote(**base)


@pytest.mark.asyncio
async def test_backfill_fills_rsi_and_zero_52w_range(monkeypatch):
    """Price-only sources (NSE/Breeze gaps) get RSI and a real 52W range."""
    live = _live_quote(rsi_14=None, w52_high=0.0, w52_low=0.0)
    hist = _live_quote(cmp=95.0, rsi_14=38.0, w52_high=130.0, w52_low=70.0)
    yf_client = YFinanceClient()

    async def fake_quote(_ticker):
        return hist

    monkeypatch.setattr(yf_client, "get_stock_quote", fake_quote)
    out = await yf_client.backfill_quote_history(live)
    assert out.cmp == 100.0                       # live price kept
    assert out.rsi_14 == 38.0
    assert (out.w52_high, out.w52_low) == (130.0, 70.0)


@pytest.mark.asyncio
async def test_backfill_keeps_source_52w_range(monkeypatch):
    live = _live_quote(rsi_14=None)
    hist = _live_quote(rsi_14=38.0, w52_high=999.0, w52_low=1.0)
    yf_client = YFinanceClient()

    async def fake_quote(_ticker):
        return hist

    monkeypatch.setattr(yf_client, "get_stock_quote", fake_quote)
    out = await yf_client.backfill_quote_history(live)
    assert (out.w52_high, out.w52_low) == (120.0, 80.0)


@pytest.mark.asyncio
async def test_backfill_skips_fetch_when_complete(monkeypatch):
    live = _live_quote()
    yf_client = YFinanceClient()

    async def fail(_ticker):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(yf_client, "get_stock_quote", fail)
    assert await yf_client.backfill_quote_history(live) is live


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
