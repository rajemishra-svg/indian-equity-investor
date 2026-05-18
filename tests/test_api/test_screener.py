"""Tests for ScreenerClient."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from src.api.screener import ScreenerClient
from src.models import FinancialMetrics


# ---------------------------------------------------------------------------
# Minimal realistic Screener HTML (simplified)
# ---------------------------------------------------------------------------

SCREENER_HTML = """
<html>
<body>
<ul id="top-ratios">
  <li><span>Market Cap</span> <span>₹ 15,00,000 Cr.</span></li>
</ul>
<section id="profit-loss">
  <table class="data-table responsive-text-nowrap">
    <thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td>Sales+</td><td>200000</td><td>250000</td><td>300000</td></tr>
      <tr><td>Net Profit+</td><td>42000</td><td>47000</td><td>52000</td></tr>
    </tbody>
  </table>
  <table class="ranges-table">
    <tr><th>Compounded Sales Growth</th></tr>
    <tr><td>5 Years:</td><td>18%</td></tr>
    <tr><td>3 Years:</td><td>22%</td></tr>
  </table>
  <table class="ranges-table">
    <tr><th>Compounded Profit Growth</th></tr>
    <tr><td>5 Years:</td><td>21%</td></tr>
    <tr><td>3 Years:</td><td>19%</td></tr>
  </table>
  <table class="ranges-table">
    <tr><th>Return on Equity</th></tr>
    <tr><td>5 Years:</td><td>22%</td></tr>
    <tr><td>Last Year:</td><td>26%</td></tr>
  </table>
</section>
<section id="ratios">
  <table class="data-table">
    <thead><tr><th></th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th></tr></thead>
    <tbody>
      <tr><td>ROCE %</td><td>20</td><td>22</td><td>24</td><td>26</td><td>28</td></tr>
      <tr><td>Interest Coverage Ratio</td><td>10</td><td>11</td><td>12</td><td>13</td><td>12.5</td></tr>
      <tr><td>OPM %</td><td>15</td><td>16</td><td>17</td><td>18</td><td>18.2</td></tr>
    </tbody>
  </table>
</section>
<section id="balance-sheet">
  <table class="data-table">
    <thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td>Equity Capital</td><td>100</td><td>100</td><td>100</td></tr>
      <tr><td>Reserves</td><td>800</td><td>850</td><td>900</td></tr>
      <tr><td>Borrowings+</td><td>350</td><td>360</td><td>380</td></tr>
    </tbody>
  </table>
</section>
<section id="cash-flow">
  <table>
    <tbody>
      <tr><td>Cash from Operating Activity+</td><td>40000</td><td>45000</td><td>50000</td></tr>
      <tr><td>Cash from Investing Activity+</td><td>-5000</td><td>-6000</td><td>-7000</td></tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_get_financials_parses_data_correctly():
    """ScreenerClient should parse revenue/profit CAGRs and ratios from HTML."""
    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/RELIANCE/consolidated/").mock(
            return_value=httpx.Response(200, text=SCREENER_HTML)
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("RELIANCE")

    assert metrics is not None
    assert isinstance(metrics, FinancialMetrics)
    assert metrics.revenue_cagr_5y == pytest.approx(18.0)
    assert metrics.revenue_cagr_3y == pytest.approx(22.0)
    assert metrics.pat_cagr_5y == pytest.approx(21.0)
    assert metrics.pat_cagr_3y == pytest.approx(19.0)
    assert metrics.roe_5y_avg == pytest.approx(22.0)
    assert metrics.roce_5y_avg == pytest.approx(24.0)  # avg of 5 values: (20+22+24+26+28)/5
    # D/E computed from balance sheet: 380 / (100 + 900) = 0.38
    assert metrics.debt_to_equity == pytest.approx(0.38)
    assert metrics.interest_coverage == pytest.approx(12.5)
    assert metrics.ebitda_margin_latest == pytest.approx(18.2)
    assert metrics.market_cap_cr == pytest.approx(1_500_000.0)
    # CFO/NP from last 3 years: 50000/52000, 45000/47000, 40000/42000
    assert metrics.cfo_net_profit_3y_avg == pytest.approx(96.0, abs=2.0)


@pytest.mark.asyncio
async def test_get_financials_returns_none_on_network_error():
    """get_financials should return None on request error."""
    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/BADTICKER/consolidated/").mock(
            side_effect=httpx.ConnectError("refused")
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("BADTICKER")

    assert metrics is None


@pytest.mark.asyncio
async def test_get_financials_rate_limit_triggers_wait(monkeypatch):
    """429 response should trigger an exponential back-off wait before retry."""
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    # Suppress random jitter so assertions are deterministic
    monkeypatch.setattr("src.api.screener.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr("src.api.screener.asyncio.sleep", fake_sleep)

    with respx.mock(base_url="https://www.screener.in") as mock:
        # First call returns 429, second returns HTML
        mock.get("/company/RELIANCE/consolidated/").mock(
            side_effect=[
                httpx.Response(429, text="Too Many Requests"),
                httpx.Response(200, text=SCREENER_HTML),
            ]
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("RELIANCE")

    # First back-off step is 10 s (jitter zeroed → exactly 10.0)
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 10.0
    assert metrics is not None


@pytest.mark.asyncio
async def test_get_financials_rate_limit_multiple_retries(monkeypatch):
    """Three consecutive 429s should walk through the full backoff schedule."""
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("src.api.screener.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr("src.api.screener.asyncio.sleep", fake_sleep)

    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/RELIANCE/consolidated/").mock(
            side_effect=[
                httpx.Response(429, text="Too Many Requests"),
                httpx.Response(429, text="Too Many Requests"),
                httpx.Response(429, text="Too Many Requests"),
                httpx.Response(200, text=SCREENER_HTML),
            ]
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("RELIANCE")

    # Backoff schedule: 10 s, 30 s, 90 s (jitter zeroed)
    assert sleep_calls == [10.0, 30.0, 90.0]
    assert metrics is not None


@pytest.mark.asyncio
async def test_missing_data_adds_flags():
    """When financial data is missing from HTML, data flags should be added."""
    empty_html = "<html><body><section id='profit-loss'></section></body></html>"

    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/EMPTY/consolidated/").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("EMPTY")

    assert metrics is not None
    assert metrics.revenue_cagr_5y is None
    assert len(metrics.data_flags) > 0
    assert any("DATA UNVERIFIED" in f for f in metrics.data_flags)
