"""Tests for YFinanceClient — mocks yfinance.Ticker to avoid real network calls."""
from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from src.api.yfinance_client import YFinanceClient, _safe_float, _to_yf_symbol
from src.models import StockQuote, ValuationData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_fast_info(**kwargs) -> MagicMock:
    """Build a MagicMock that behaves like yf.Ticker.fast_info dict/object."""
    fi = MagicMock()
    fi.get = lambda key, default=None: kwargs.get(key, default)
    return fi


def _mock_ticker(fast_info: MagicMock | None = None, info: dict | None = None) -> MagicMock:
    ticker = MagicMock()
    ticker.fast_info = fast_info or _mock_fast_info()
    ticker.info = info or {}
    return ticker


# ---------------------------------------------------------------------------
# _to_yf_symbol
# ---------------------------------------------------------------------------


def test_to_yf_symbol_appends_ns():
    assert _to_yf_symbol("RELIANCE") == "RELIANCE.NS"
    assert _to_yf_symbol("tcs") == "TCS.NS"


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------


def test_safe_float_normal():
    assert _safe_float(3.14) == pytest.approx(3.14)


def test_safe_float_none():
    assert _safe_float(None) is None


def test_safe_float_nan():
    assert _safe_float(float("nan")) is None


def test_safe_float_inf():
    assert _safe_float(float("inf")) is None


def test_safe_float_string_number():
    assert _safe_float("42.5") == pytest.approx(42.5)


# ---------------------------------------------------------------------------
# YFinanceClient.get_stock_quote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stock_quote_returns_stock_quote():
    fast_info = _mock_fast_info(
        lastPrice=3500.0,
        fiftyTwoWeekHigh=3800.0,
        fiftyTwoWeekLow=2900.0,
        marketCap=9_60_00_00_00_000,  # ~96,000 Cr
        twoHundredDayAverage=3200.0,
    )
    mock_ticker = _mock_ticker(fast_info=fast_info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            quote = await client.get_stock_quote("TCS")

    assert isinstance(quote, StockQuote)
    assert quote.ticker == "TCS"
    assert quote.cmp == pytest.approx(3500.0)
    assert quote.w52_high == pytest.approx(3800.0)
    assert quote.w52_low == pytest.approx(2900.0)
    assert quote.market_cap_cr == pytest.approx(96000.0)
    assert quote.dma_200 == pytest.approx(3200.0)
    assert quote.exchange == "NSE"
    assert quote.is_stale is True  # Yahoo data is delayed
    assert quote.data_timestamp.tzinfo == UTC


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_when_price_is_zero():
    fast_info = _mock_fast_info(lastPrice=0.0)
    mock_ticker = _mock_ticker(fast_info=fast_info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            quote = await client.get_stock_quote("BADTICKER")

    assert quote is None


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_when_price_is_none():
    fast_info = _mock_fast_info()  # no lastPrice key
    mock_ticker = _mock_ticker(fast_info=fast_info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            quote = await client.get_stock_quote("NOTICKER")

    assert quote is None


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_on_exception():
    with patch("src.api.yfinance_client.yf.Ticker", side_effect=RuntimeError("network error")):
        async with YFinanceClient() as client:
            quote = await client.get_stock_quote("TCS")

    assert quote is None


@pytest.mark.asyncio
async def test_get_stock_quote_handles_missing_optional_fields():
    """w52_high, w52_low, dma_200 missing → defaults to 0.0 / None."""
    fast_info = _mock_fast_info(lastPrice=1500.0, marketCap=5_00_00_00_000)
    mock_ticker = _mock_ticker(fast_info=fast_info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            quote = await client.get_stock_quote("INFY")

    assert quote is not None
    assert quote.cmp == pytest.approx(1500.0)
    assert quote.w52_high == pytest.approx(0.0)
    assert quote.w52_low == pytest.approx(0.0)
    assert quote.dma_200 is None


# ---------------------------------------------------------------------------
# YFinanceClient.get_valuation_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_valuation_data_returns_valuation_data():
    info = {
        "trailingPE": 25.3,
        "priceToBook": 4.1,
        "pegRatio": 1.8,
        "enterpriseToEbitda": 18.5,
        "sharesOutstanding": 3_700_000,  # 0.37 Cr shares (3.7M)
        "quoteType": "EQUITY",
    }
    mock_ticker = _mock_ticker(info=info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            val = await client.get_valuation_data("TCS")

    assert isinstance(val, ValuationData)
    assert val.pe_current == pytest.approx(25.3)
    assert val.pbv_current == pytest.approx(4.1)
    assert val.peg_ratio == pytest.approx(1.8)
    assert val.ev_ebitda_current == pytest.approx(18.5)
    assert val.shares_outstanding_cr == pytest.approx(0.37)


@pytest.mark.asyncio
async def test_get_valuation_data_adds_flags_for_missing_pe_pb():
    info = {"quoteType": "EQUITY"}  # no PE or PB
    mock_ticker = _mock_ticker(info=info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            val = await client.get_valuation_data("NEWCO")

    assert val is not None
    assert val.pe_current is None
    assert val.pbv_current is None
    assert any("pe_current" in f for f in val.data_flags)
    assert any("pbv_current" in f for f in val.data_flags)


@pytest.mark.asyncio
async def test_get_valuation_data_returns_none_when_quote_type_none():
    info = {"quoteType": "NONE"}
    mock_ticker = _mock_ticker(info=info)

    with patch("src.api.yfinance_client.yf.Ticker", return_value=mock_ticker):
        async with YFinanceClient() as client:
            val = await client.get_valuation_data("BADTICKER")

    assert val is None


@pytest.mark.asyncio
async def test_get_valuation_data_returns_none_on_exception():
    with patch("src.api.yfinance_client.yf.Ticker", side_effect=RuntimeError("boom")):
        async with YFinanceClient() as client:
            val = await client.get_valuation_data("TCS")

    assert val is None


# ---------------------------------------------------------------------------
# Context manager (no-op)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_is_noop():
    async with YFinanceClient() as client:
        assert isinstance(client, YFinanceClient)
