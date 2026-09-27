"""Tests for NSEClient — NextApi quote, index constituents, SHP shareholding, session handling."""
from __future__ import annotations

import httpx
import pytest
import respx

from src.api.nse import NSEClient, parse_shp_pledge
from src.api.yfinance_client import YFinanceClient
from src.models import StockQuote

NSE = "https://www.nseindia.com"
ARCHIVE = "https://nsearchives.nseindia.com/corporate/xbrl"
QUOTE_PATH = "/api/NextApi/apiClient/GetQuoteApi"
INDICES_PATH = "/api/NextApi/apiClient/marketWatchApi"
SHP_MASTER_PATH = "/api/corporate-share-holdings-master"

# --- Sample NSE API responses (shapes captured Sep 2026) ---

NSE_QUOTE_RESPONSE = {
    "equityResponse": [
        {
            "metaData": {"symbol": "RELIANCE", "companyName": "Reliance Industries Limited",
                         "closePrice": 2849.0},
            "tradeInfo": {"lastPrice": 2850.50, "totalMarketCap": 19_310_000_000_000},
            "priceInfo": {"yearHigh": 3217.90, "yearLow": 2220.75},
        }
    ]
}

NSE_INDICES_RESPONSE = {
    "data": [
        {"index": "NIFTY 50", "last": 22_500.00, "yearHigh": 24_857.00},
        {"index": "NIFTY BANK", "last": 47_500.00, "yearHigh": 52_000.00},
    ]
}

NSE_CONSTITUENTS_RESPONSE = {
    "data": {
        "data": [
            {"priority": 1, "symbol": "NIFTY 50", "series": None},
            {"priority": 0, "symbol": "RELIANCE", "series": "EQ"},
            {"priority": 0, "symbol": "hdfcbank", "series": "EQ"},
        ],
        "marketStatus": {},
    }
}


def _shp_xml(pledged_fraction: float | None, declared: bool) -> str:
    facts = (
        '<in-bse-shp:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup '
        f'contextRef="MainI">{str(declared).lower()}'
        "</in-bse-shp:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup>"
    )
    if pledged_fraction is not None:
        facts += (
            '<in-bse-shp:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares '
            f'contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI" decimals="INF" unit="pure">'
            f"{pledged_fraction}</in-bse-shp:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares>"
            # Same concept on the whole-company context — must be ignored (wrong basis)
            '<in-bse-shp:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares '
            'contextRef="ShareholdingPattern_ContextI">0.0001'
            "</in-bse-shp:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares>"
        )
    return (
        '<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-bse-shp="http://www.bseindia.com/xbrl/shp">' + facts + "</xbrli:xbrl>"
    )


def _shp_row(date: str, holding: str, public: str, name: str, broadcast: str) -> dict:
    return {
        "date": date,
        "pr_and_prgrp": holding,
        "public_val": public,
        "broadcastDate": broadcast,
        "xbrl": f"{ARCHIVE}/{name}.xml",
    }


SHP_MASTER = [
    _shp_row("30-JUN-2026", "44.29", "55.52", "q4", "18-JUL-2026 11:21:01"),
    _shp_row("31-MAR-2026", "45.32", "54.50", "q3", "20-APR-2026 17:19:06"),
    _shp_row("31-DEC-2025", "45.32", "54.50", "q2", "20-JAN-2026 18:32:06"),
    _shp_row("30-SEP-2025", "45.31", "54.51", "q1", "17-OCT-2025 14:48:36"),
    _shp_row("30-JUN-2025", "44.84", "55.00", "q0", "21-JUL-2025 20:53:35"),
]


def _mock_session(mock) -> None:
    # Homepage commonly 403s for non-browser clients; must not abort the call
    mock.get(f"{NSE}/").mock(return_value=httpx.Response(403, text="Access Denied"))


# --- Session / transport ---


def test_does_not_advertise_brotli():
    """httpx can't decode NSE's brotli bodies without the optional package."""
    assert "br" not in NSEClient()._default_headers()["Accept-Encoding"]


# --- Quote ---


@pytest.mark.asyncio
async def test_get_stock_quote_returns_correct_quote():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        route = mock.get(f"{NSE}{QUOTE_PATH}").mock(
            return_value=httpx.Response(200, json=NSE_QUOTE_RESPONSE)
        )
        async with NSEClient() as client:
            quote = await client.get_stock_quote("reliance")

    assert isinstance(quote, StockQuote)
    assert quote.ticker == "RELIANCE"
    assert quote.company_name == "Reliance Industries Limited"
    assert quote.cmp == 2850.50
    assert quote.w52_high == 3217.90
    assert quote.w52_low == 2220.75
    assert abs(quote.market_cap_cr - 1_931_000.0) < 1
    assert quote.dma_200 is None  # history fields are backfilled by the caller
    params = route.calls[0].request.url.params
    assert params["functionName"] == "getSymbolData"
    assert params["symbol"] == "RELIANCE"
    assert params["series"] == "EQ"


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_on_http_error():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{QUOTE_PATH}").mock(return_value=httpx.Response(403))
        async with NSEClient() as client:
            assert await client.get_stock_quote("INVALID") is None


@pytest.mark.asyncio
async def test_get_stock_quote_returns_none_on_empty_response():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{QUOTE_PATH}").mock(
            return_value=httpx.Response(200, json={"equityResponse": []})
        )
        async with NSEClient() as client:
            assert await client.get_stock_quote("NOSUCH") is None


@pytest.mark.asyncio
async def test_connection_error_returns_none():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{QUOTE_PATH}").mock(side_effect=httpx.ConnectError("connection refused"))
        async with NSEClient() as client:
            assert await client.get_stock_quote("RELIANCE") is None


# --- Nifty / constituents ---


@pytest.mark.asyncio
async def test_get_nifty50_returns_correct_tuple_despite_homepage_403():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}/api/allIndices").mock(
            return_value=httpx.Response(200, json=NSE_INDICES_RESPONSE)
        )
        async with NSEClient() as client:
            current, high = await client.get_nifty50()
    assert current == 22_500.00
    assert high == 24_857.00


@pytest.mark.asyncio
async def test_get_nifty50_raises_when_not_found():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}/api/allIndices").mock(
            return_value=httpx.Response(200, json={"data": [{"index": "NIFTY BANK"}]})
        )
        async with NSEClient() as client:
            with pytest.raises(ValueError, match="Nifty 50 not found"):
                await client.get_nifty50()


@pytest.mark.asyncio
async def test_get_index_constituents_skips_index_row():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        route = mock.get(f"{NSE}{INDICES_PATH}").mock(
            return_value=httpx.Response(200, json=NSE_CONSTITUENTS_RESPONSE)
        )
        async with NSEClient() as client:
            tickers = await client.get_index_constituents("NIFTY 50")
    assert tickers == ["RELIANCE", "HDFCBANK"]
    params = route.calls[0].request.url.params
    assert params["functionName"] == "getIndicesData"
    assert params["symbol"] == "NIFTY 50"


@pytest.mark.asyncio
async def test_get_index_constituents_raises_on_empty():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{INDICES_PATH}").mock(return_value=httpx.Response(200, json={"data": {}}))
        async with NSEClient() as client:
            with pytest.raises(ValueError):
                await client.get_index_constituents("NIFTY 50")


# --- Shareholding ---


def test_parse_shp_pledge_uses_promoter_basis():
    assert parse_shp_pledge(_shp_xml(0.116, declared=True).encode()) == 11.6


def test_parse_shp_pledge_declared_none_is_zero():
    assert parse_shp_pledge(_shp_xml(None, declared=False).encode()) == 0.0


def test_parse_shp_pledge_declared_but_missing_is_unknown():
    assert parse_shp_pledge(_shp_xml(None, declared=True).encode()) is None


@pytest.mark.asyncio
async def test_get_shareholding_builds_holding_and_pledge_trends():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{SHP_MASTER_PATH}").mock(return_value=httpx.Response(200, json=SHP_MASTER))
        for name, frac in (("q4", 0.116), ("q3", 0.1181), ("q2", 0.117), ("q1", 0.1247)):
            mock.get(f"{ARCHIVE}/{name}.xml").mock(
                return_value=httpx.Response(200, content=_shp_xml(frac, True).encode())
            )
        async with NSEClient() as client:
            g = await client.get_shareholding("jswsteel")

    assert g.promoter_holding_pct == 44.29
    assert g.public_holding_pct == 55.52
    assert g.promoter_pledging_pct == 11.6
    assert g.promoter_pledging_trend == [12.47, 11.7, 11.81, 11.6]  # oldest → latest
    assert g.pledging_trend_direction == "decreasing"
    assert g.promoter_holding_trend == [44.84, 45.31, 45.32, 45.32, 44.29]
    assert g.data_flags == []


@pytest.mark.asyncio
async def test_get_shareholding_unknown_pledge_stays_none():
    """Unparseable pledge must not be reported as a clean 0%."""
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{SHP_MASTER_PATH}").mock(return_value=httpx.Response(200, json=SHP_MASTER))
        mock.get(url__startswith=ARCHIVE).mock(return_value=httpx.Response(404))
        async with NSEClient() as client:
            g = await client.get_shareholding("JSWSTEEL")
    assert g.promoter_holding_pct == 44.29
    assert g.promoter_pledging_pct is None
    assert any("PLEDGING UNKNOWN" in f for f in g.data_flags)


@pytest.mark.asyncio
async def test_get_shareholding_revision_supersedes_original():
    master = SHP_MASTER + [
        _shp_row("30-JUN-2026", "44.10", "55.90", "q4rev", "25-JUL-2026 10:00:00"),
    ]
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{SHP_MASTER_PATH}").mock(return_value=httpx.Response(200, json=master))
        mock.get(url__startswith=ARCHIVE).mock(
            return_value=httpx.Response(200, content=_shp_xml(None, False).encode())
        )
        async with NSEClient() as client:
            g = await client.get_shareholding("JSWSTEEL")
    assert g.promoter_holding_pct == 44.10
    assert g.promoter_pledging_pct == 0.0


@pytest.mark.asyncio
async def test_get_shareholding_returns_none_on_failure():
    with respx.mock(assert_all_called=False) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{SHP_MASTER_PATH}").mock(return_value=httpx.Response(404))
        async with NSEClient() as client:
            assert await client.get_shareholding("JSWSTEEL") is None


@pytest.mark.asyncio
async def test_get_shareholding_ignores_non_archive_xbrl_urls():
    master = [{**SHP_MASTER[0], "xbrl": "http://169.254.169.254/latest"}]
    with respx.mock(assert_all_called=True) as mock:
        _mock_session(mock)
        mock.get(f"{NSE}{SHP_MASTER_PATH}").mock(return_value=httpx.Response(200, json=master))
        async with NSEClient() as client:
            g = await client.get_shareholding("JSWSTEEL")
    assert g.promoter_pledging_pct is None


# --- yfinance backfill of history-derived fields ---


@pytest.mark.asyncio
async def test_backfill_quote_history_keeps_live_price(monkeypatch):
    live = StockQuote(ticker="X", company_name="X", cmp=100.0, w52_high=120, w52_low=80,
                      market_cap_cr=1000)
    hist = live.model_copy(update={"cmp": 95.0, "dma_200": 90.0, "avg_daily_value_cr": 12.5,
                                   "volume_trend_down_days": "declining"})
    yf = YFinanceClient()

    async def fake_quote(_ticker):
        return hist

    monkeypatch.setattr(yf, "get_stock_quote", fake_quote)
    out = await yf.backfill_quote_history(live)
    assert out.cmp == 100.0
    assert (out.dma_200, out.avg_daily_value_cr, out.volume_trend_down_days) == (90.0, 12.5, "declining")


@pytest.mark.asyncio
async def test_backfill_quote_history_tolerates_yfinance_failure(monkeypatch):
    live = StockQuote(ticker="X", company_name="X", cmp=100.0, w52_high=120, w52_low=80,
                      market_cap_cr=1000)
    yf = YFinanceClient()

    async def fake_quote(_ticker):
        return None

    monkeypatch.setattr(yf, "get_stock_quote", fake_quote)
    assert await yf.backfill_quote_history(live) is live
