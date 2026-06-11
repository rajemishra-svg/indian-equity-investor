"""Tests for NSEClient."""
from __future__ import annotations

import httpx
import pytest
import respx

from src.api.nse import NSEClient
from src.models import StockQuote

# --- Sample NSE API responses ---

NSE_QUOTE_RESPONSE = {
    "info": {"companyName": "Reliance Industries Limited"},
    "priceInfo": {
        "lastPrice": 2850.50,
        "weekHighLow": {"max": 3217.90, "min": 2220.75},
    },
    "marketDeptOrderBook": {
        "tradeInfo": {"totalMarketCap": 19_310_000_000_000}  # ~₹19.31 lakh crore
    },
}

NSE_INDICES_RESPONSE = {
    "data": [
        {"index": "NIFTY 50", "last": 22_500.00, "yearHigh": 24_857.00},
        {"index": "NIFTY BANK", "last": 47_500.00, "yearHigh": 52_000.00},
    ]
}


@pytest.mark.asyncio
async def test_get_stock_quote_returns_correct_quote():
    """get_stock_quote should parse NSE JSON and return StockQuote."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        # Homepage for session establishment
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        # Quote endpoint
        mock.get("/api/quote-equity", params={"symbol": "RELIANCE"}).mock(
            return_value=httpx.Response(200, json=NSE_QUOTE_RESPONSE)
        )

        async with NSEClient() as client:
            quote = await client.get_stock_quote("RELIANCE")

    assert quote is not None
    assert isinstance(quote, StockQuote)
    assert quote.ticker == "RELIANCE"
    assert quote.company_name == "Reliance Industries Limited"
    assert quote.cmp == pytest.approx(2850.50)
    assert quote.w52_high == pytest.approx(3217.90)
    assert quote.w52_low == pytest.approx(2220.75)
    assert quote.market_cap_cr == pytest.approx(1_931_000.0, rel=1e-2)
    assert quote.exchange == "NSE"


@pytest.mark.asyncio
async def test_get_nifty50_returns_correct_tuple():
    """get_nifty50 should return (current_level, year_high) for Nifty 50."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        mock.get("/api/allIndices").mock(
            return_value=httpx.Response(200, json=NSE_INDICES_RESPONSE)
        )

        async with NSEClient() as client:
            current, peak = await client.get_nifty50()

    assert current == pytest.approx(22_500.00)
    assert peak == pytest.approx(24_857.00)


@pytest.mark.asyncio
async def test_get_nifty50_raises_when_not_found():
    """get_nifty50 should raise ValueError if Nifty 50 not in response."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        mock.get("/api/allIndices").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        async with NSEClient() as client:
            with pytest.raises(ValueError, match="Nifty 50 not found"):
                await client.get_nifty50()


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_on_http_error():
    """get_stock_quote should return None on HTTP error (not raise)."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        mock.get("/api/quote-equity", params={"symbol": "INVALID"}).mock(
            return_value=httpx.Response(404, json={"message": "Not found"})
        )

        async with NSEClient() as client:
            quote = await client.get_stock_quote("INVALID")

    assert quote is None


@pytest.mark.asyncio
async def test_get_stock_quote_symbol_uppercased():
    """Ticker is uppercased before calling NSE API."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        # Should be called with RELIANCE (uppercase)
        mock.get("/api/quote-equity", params={"symbol": "RELIANCE"}).mock(
            return_value=httpx.Response(200, json=NSE_QUOTE_RESPONSE)
        )

        async with NSEClient() as client:
            quote = await client.get_stock_quote("reliance")  # lowercase input

    assert quote is not None
    assert quote.ticker == "RELIANCE"


@pytest.mark.asyncio
async def test_connection_error_returns_none():
    """get_stock_quote should return None when connection fails after retries."""
    with respx.mock(base_url="https://www.nseindia.com") as mock:
        mock.get("/").mock(return_value=httpx.Response(200, text="<html></html>"))
        mock.get("/api/quote-equity").mock(side_effect=httpx.ConnectError("connection refused"))

        async with NSEClient() as client:
            # ConnectError triggers tenacity retry; after exhausting, NSEClient catches it
            quote = await client.get_stock_quote("RELIANCE")

    assert quote is None
