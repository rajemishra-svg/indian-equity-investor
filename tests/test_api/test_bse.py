"""Tests for BSEClient — scripcode lookup, shareholding parse, circuit breaker."""
from __future__ import annotations

import httpx
import pytest
import respx

from src.api import bse as bse_module
from src.api.bse import (
    CIRCUIT_BREAKER_THRESHOLD,
    BSEClient,
    is_circuit_open,
    reset_circuit_breaker,
)
from src.models import GovernanceData

BASE = "https://api.bseindia.com"
SEARCH_PATH = "/BseIndiaAPI/api/PeerSmartSearch/w"
QUARTERS_PATH = "/BseIndiaAPI/api/SHPQNewFormat/w"
SUMMARY_PATH = "/BseIndiaAPI/api/Corp_shpSec_SHPSUMMARY_ng/w"

BOT_BLOCK_HTML = "<!DOCTYPE html><html><body>Access Denied</body></html>"


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Circuit breaker state is module-global — isolate every test."""
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


def _li(scripcode: str, symbol: str, name: str) -> str:
    """One PeerSmartSearch result item, mimicking BSE's real markup."""
    return (
        f"<li class='quotemenu' onclick=\"liclick('{scripcode}','{name}')\">"
        f"<a><strong>{symbol}</strong> {name.upper()}<br />"
        f"<span><strong>{symbol}</strong>&nbsp;&nbsp;&nbsp;INE000A01001"
        f"&nbsp;&nbsp;&nbsp;{scripcode}</span></a></li>"
    )


def _quarters(*qtrids: float) -> dict:
    return {"Table": [{"qtrid": q, "qtr": f"Q{q}"} for q in qtrids]}


def _summary(holding: float | None, pledge: float | None) -> dict:
    """SHPSUMMARY response. The non-promoter row comes first deliberately —
    a naive 'promoter in category' match would pick it up."""
    promoter_row = {"Fld_ShortCatg": "Promoter and Promoter Group"}
    if holding is not None:
        promoter_row["Fld_TotalPercentageOf_A_B_C2"] = holding
    if pledge is not None:
        promoter_row["Fld_PledgeEncumberedPercentage"] = pledge
    return {
        "Table1": [
            {
                "Fld_ShortCatg": "Non Promoter- Non Public shareholder",
                "Fld_TotalPercentageOf_A_B_C2": 1.78,
                "Fld_PledgeEncumberedPercentage": 99.0,
            },
            promoter_row,
            {
                "Fld_ShortCatg": "Public shareholder",
                "Fld_TotalPercentageOf_A_B_C2": 48.22,
                "Fld_PledgeEncumberedPercentage": 0.0,
            },
        ]
    }


def _mock_happy_path(mock: respx.MockRouter, pledges: list[float]) -> None:
    """Mock the full chain: search → quarter list → one summary per quarter.

    ``pledges`` is newest-first, one entry per mocked quarter.
    """
    mock.get(SEARCH_PATH, params={"Type": "EQ", "text": "RELIANCE"}).mock(
        return_value=httpx.Response(
            200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
        )
    )
    qtrids = [129 - i for i in range(len(pledges))]
    mock.get(QUARTERS_PATH, params={"scripcode": "500325"}).mock(
        return_value=httpx.Response(200, json=_quarters(*[float(q) for q in qtrids]))
    )
    for qtrid, pledge in zip(qtrids, pledges, strict=True):
        mock.get(
            SUMMARY_PATH, params={"scripcode": "500325", "qtrcode": str(qtrid)}
        ).mock(return_value=httpx.Response(200, json=_summary(50.0, pledge)))


# ── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_shareholding_parses_promoter_row_and_trend():
    """Latest-quarter numbers come from the promoter row; trend is chronological."""
    with respx.mock(base_url=BASE) as mock:
        _mock_happy_path(mock, pledges=[5.0, 4.0, 3.0, 2.0])  # newest first

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert isinstance(gov, GovernanceData)
    assert gov.promoter_holding_pct == pytest.approx(50.0)
    assert gov.promoter_pledging_pct == pytest.approx(5.0)  # latest quarter
    assert gov.promoter_pledging_trend == [2.0, 3.0, 4.0, 5.0]  # oldest → newest
    assert gov.pledging_trend_direction == "increasing"
    assert not is_circuit_open()


@pytest.mark.asyncio
async def test_get_shareholding_only_fetches_trend_quarters():
    """A long filing archive must not trigger one summary call per quarter."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
            )
        )
        mock.get(QUARTERS_PATH).mock(
            return_value=httpx.Response(
                200, json=_quarters(*[float(129 - i) for i in range(20)])
            )
        )
        summary_route = mock.get(SUMMARY_PATH).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 0.0))
        )

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is not None
    assert summary_route.call_count == bse_module._TREND_QUARTERS


@pytest.mark.asyncio
async def test_scripcode_lookup_picks_exact_symbol_match():
    """Search returns several companies; only the exact symbol match counts."""
    li_html = (
        _li("532939", "RPOWER", "Reliance Power Ltd")
        + _li("500325", "RELIANCE", "Reliance Industries Ltd")
    )
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(return_value=httpx.Response(200, json=li_html))
        mock.get(QUARTERS_PATH, params={"scripcode": "500325"}).mock(
            return_value=httpx.Response(200, json=_quarters(129.0))
        )
        mock.get(SUMMARY_PATH).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 0.0))
        )

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is not None
    assert gov.promoter_holding_pct == pytest.approx(50.0)


def test_parse_scripcode_search_handles_split_highlight():
    """The <strong> highlight may wrap only part of the symbol."""
    li_html = (
        "<li onclick=\"liclick('500325','Reliance Industries Ltd')\">"
        "<a><span><strong>RELI</strong>ANCE&nbsp;&nbsp;INE002A01018&nbsp;&nbsp;500325</span></a></li>"
    )
    assert BSEClient._parse_scripcode_search(li_html, "RELIANCE") == "500325"


# ── Not-listed vs infrastructure failure ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_not_on_bse_returns_none_without_counting_failure():
    """A clean 'no match' is not an infrastructure failure — breaker stays shut."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(return_value=httpx.Response(200, json=""))

        async with BSEClient() as client:
            for _ in range(CIRCUIT_BREAKER_THRESHOLD + 2):
                assert await client.get_shareholding("NSEONLY") is None

    assert not is_circuit_open()


@pytest.mark.asyncio
async def test_html_bot_block_returns_none():
    """Non-JSON response (the original bug) fails over cleanly to None."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, text=BOT_BLOCK_HTML, headers={"content-type": "text/html"}
            )
        )

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is None
    assert bse_module._consecutive_failures == 1


@pytest.mark.asyncio
async def test_latest_quarter_summary_failure_returns_none():
    """If the newest quarter's summary fails, the whole result is unusable."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
            )
        )
        mock.get(QUARTERS_PATH).mock(
            return_value=httpx.Response(200, json=_quarters(129.0, 128.0))
        )
        mock.get(SUMMARY_PATH, params={"qtrcode": "129"}).mock(
            return_value=httpx.Response(500)
        )
        mock.get(SUMMARY_PATH, params={"qtrcode": "128"}).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 0.0))
        )

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is None
    assert bse_module._consecutive_failures == 1


@pytest.mark.asyncio
async def test_older_quarter_failure_is_tolerated():
    """An older quarter erroring only shortens the trend; latest data survives."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
            )
        )
        mock.get(QUARTERS_PATH).mock(
            return_value=httpx.Response(200, json=_quarters(129.0, 128.0, 127.0))
        )
        mock.get(SUMMARY_PATH, params={"qtrcode": "129"}).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 5.0))
        )
        mock.get(SUMMARY_PATH, params={"qtrcode": "128"}).mock(
            return_value=httpx.Response(500)
        )
        mock.get(SUMMARY_PATH, params={"qtrcode": "127"}).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 3.0))
        )

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is not None
    assert gov.promoter_pledging_pct == pytest.approx(5.0)
    assert gov.promoter_pledging_trend == [3.0, 5.0]
    assert gov.pledging_trend_direction == "increasing"
    assert not is_circuit_open()


@pytest.mark.asyncio
async def test_missing_promoter_row_returns_none_for_screener_fallback():
    """A filing with no parseable promoter row yields None, not a zero-stub."""
    empty = {"Table1": [{"Fld_ShortCatg": "Public shareholder"}]}
    with respx.mock(base_url=BASE) as mock:
        mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
            )
        )
        mock.get(QUARTERS_PATH).mock(
            return_value=httpx.Response(200, json=_quarters(129.0))
        )
        mock.get(SUMMARY_PATH).mock(return_value=httpx.Response(200, json=empty))

        async with BSEClient() as client:
            gov = await client.get_shareholding("RELIANCE")

    assert gov is None
    assert not is_circuit_open()


# ── Circuit breaker ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold_and_skips_http():
    """After N consecutive failures BSE is skipped — no further HTTP calls."""
    with respx.mock(base_url=BASE) as mock:
        search_route = mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, text=BOT_BLOCK_HTML, headers={"content-type": "text/html"}
            )
        )

        async with BSEClient() as client:
            for _ in range(CIRCUIT_BREAKER_THRESHOLD):
                assert await client.get_shareholding("RELIANCE") is None
            assert is_circuit_open()
            assert search_route.call_count == CIRCUIT_BREAKER_THRESHOLD

            # Subsequent calls return None without touching the network
            assert await client.get_shareholding("TCS") is None
            assert search_route.call_count == CIRCUIT_BREAKER_THRESHOLD


@pytest.mark.asyncio
async def test_breaker_spans_client_instances():
    """The breaker is module-level: a fresh BSEClient instance still skips."""
    with respx.mock(base_url=BASE) as mock:
        route = mock.get(SEARCH_PATH).mock(
            return_value=httpx.Response(
                200, text=BOT_BLOCK_HTML, headers={"content-type": "text/html"}
            )
        )

        async with BSEClient() as client:
            for _ in range(CIRCUIT_BREAKER_THRESHOLD):
                await client.get_shareholding("RELIANCE")
        assert is_circuit_open()

        async with BSEClient() as fresh_client:
            assert await fresh_client.get_shareholding("INFY") is None
        assert route.call_count == CIRCUIT_BREAKER_THRESHOLD


@pytest.mark.asyncio
async def test_success_resets_consecutive_failure_count():
    """Failures interleaved with successes never trip the breaker."""
    with respx.mock(base_url=BASE) as mock:
        bad = httpx.Response(
            200, text=BOT_BLOCK_HTML, headers={"content-type": "text/html"}
        )
        search_route = mock.get(SEARCH_PATH)
        mock.get(QUARTERS_PATH).mock(
            return_value=httpx.Response(200, json=_quarters(129.0))
        )
        mock.get(SUMMARY_PATH).mock(
            return_value=httpx.Response(200, json=_summary(50.0, 0.0))
        )

        async with BSEClient() as client:
            # threshold - 1 failures...
            search_route.mock(return_value=bad)
            for _ in range(CIRCUIT_BREAKER_THRESHOLD - 1):
                await client.get_shareholding("RELIANCE")
            assert bse_module._consecutive_failures == CIRCUIT_BREAKER_THRESHOLD - 1

            # ...one success resets the counter...
            search_route.mock(
                return_value=httpx.Response(
                    200, json=_li("500325", "RELIANCE", "Reliance Industries Ltd")
                )
            )
            gov = await client.get_shareholding("RELIANCE")
            assert gov is not None
            assert bse_module._consecutive_failures == 0

            # ...so more failures below the threshold still don't trip it
            search_route.mock(return_value=bad)
            for _ in range(CIRCUIT_BREAKER_THRESHOLD - 1):
                await client.get_shareholding("RELIANCE")
            assert not is_circuit_open()
