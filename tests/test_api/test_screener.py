"""Tests for ScreenerClient."""
from __future__ import annotations

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
async def test_get_financials_fetches_gross_block_schedule_when_company_id_present():
    """Gross Block / Accumulated Depreciation come from a separate schedule
    endpoint keyed by Screener's internal company id — only reachable once
    that id is found on the page (the '#company-info' div)."""
    html_with_company_id = SCREENER_HTML + '<div id="company-info" data-company-id="57"></div>'
    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/RELIANCE/consolidated/").mock(
            return_value=httpx.Response(200, text=html_with_company_id)
        )
        mock.get(
            "/api/company/57/schedules/",
            params={"parent": "Fixed Assets", "section": "balance-sheet", "consolidated": "true"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "Gross Block": {"Mar 2024": "100", "Mar 2023": "80", "Mar 2025": "120"},
                    "Accumulated Depreciation": {"Mar 2023": "10", "Mar 2024": "15", "Mar 2025": "22"},
                },
            )
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("RELIANCE")

    assert metrics is not None
    # Chronological oldest→newest, regardless of the dict's key order.
    assert metrics.gross_block_cr_series == [80.0, 100.0, 120.0]
    assert metrics.accumulated_depreciation_cr_series == [10.0, 15.0, 22.0]


@pytest.mark.asyncio
async def test_get_financials_survives_gross_block_schedule_failure():
    """A failed/missing schedule call must not break the main financials fetch —
    it's enrichment, not core data."""
    html_with_company_id = SCREENER_HTML + '<div id="company-info" data-company-id="57"></div>'
    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/RELIANCE/consolidated/").mock(
            return_value=httpx.Response(200, text=html_with_company_id)
        )
        mock.get("/api/company/57/schedules/").mock(
            return_value=httpx.Response(404)
        )

        async with ScreenerClient() as client:
            metrics = await client.get_financials("RELIANCE")

    assert metrics is not None
    assert metrics.revenue_cagr_5y == pytest.approx(18.0)  # main parse unaffected
    assert metrics.gross_block_cr_series == []
    assert metrics.accumulated_depreciation_cr_series == []


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


# ---------------------------------------------------------------------------
# TTM column — Screener appends it to the P&L table once a quarter past FY-end
# is reported.  It must not be read as a fiscal year.
# ---------------------------------------------------------------------------

SCREENER_TTM_HTML = """
<html>
<body>
<section id="profit-loss">
  <table class="data-table responsive-text-nowrap">
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th><th>Mar 2026</th><th>TTM</th></tr></thead>
    <tbody>
      <tr><td>Sales+</td><td>100</td><td>125</td><td>150</td><td>160</td></tr>
      <tr><td>Raw Materials</td><td>60</td><td>75</td><td>90</td><td>80</td></tr>
      <tr><td>OPM %</td><td>10%</td><td>12%</td><td>14%</td><td>20%</td></tr>
      <tr><td>Other Income+</td><td>2</td><td>3</td><td>6</td><td>40</td></tr>
      <tr><td>Net Profit+</td><td>10</td><td>12</td><td>15</td><td>16</td></tr>
    </tbody>
  </table>
</section>
<section id="cash-flow">
  <table>
    <tbody>
      <tr><td>Cash from Operating Activity+</td><td>9</td><td>11</td><td>14</td></tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""


async def _financials_from(html: str) -> FinancialMetrics:
    with respx.mock(base_url="https://www.screener.in") as mock:
        mock.get("/company/TTMCO/consolidated/").mock(
            return_value=httpx.Response(200, text=html)
        )
        async with ScreenerClient() as client:
            metrics = await client.get_financials("TTMCO")
    assert metrics is not None
    return metrics


@pytest.mark.asyncio
async def test_ttm_column_is_trailing_revenue_not_a_fiscal_year():
    """TTM feeds trailing_revenue_cr; the two FY fields are the last two fiscal years."""
    metrics = await _financials_from(SCREENER_TTM_HTML)

    assert metrics.trailing_revenue_cr == pytest.approx(160.0)
    assert metrics.revenue_latest_fy_cr == pytest.approx(150.0)
    # Regression: previously 150 (the FY *before* TTM), making YoY = TTM / FY26
    assert metrics.revenue_1y_ago_cr == pytest.approx(125.0)


@pytest.mark.asyncio
async def test_ttm_column_does_not_collapse_growth_yoy():
    """YoY must be FY26 / FY25 = +20%, not TTM / FY26 = +6.7% — the latter
    falsely tripped HT-G1 revenue deceleration for every growth ticker."""
    from src.agent.growth_pipeline import compute_growth_metrics
    from src.models import AnalysisState

    state = AnalysisState(ticker="TTMCO")
    state.financials = await _financials_from(SCREENER_TTM_HTML)
    compute_growth_metrics(state)

    assert state.growth_metrics.revenue_cagr_1y == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_ttm_column_keeps_net_profit_aligned_with_cfo_years():
    """Cash flow has no TTM column, so NP must drop TTM to stay year-aligned.

    Aligned: 9/10, 11/12, 14/15 → 91.7%.  Shifted by TTM it was 11/15, 14/16 … → ~78%.
    """
    metrics = await _financials_from(SCREENER_TTM_HTML)

    assert metrics.cfo_net_profit_3y_avg == pytest.approx(91.7, abs=0.1)


@pytest.mark.asyncio
async def test_ttm_column_excluded_from_gross_margin_and_other_income():
    metrics = await _financials_from(SCREENER_TTM_HTML)

    # Three fiscal years at 40% margin; the TTM point (50%) is not part of the series
    assert metrics.gross_profit_margin_series == [40.0, 40.0, 40.0]
    # Latest FY: 6 / 150 = 4% (TTM would give 40 / 160 = 25%)
    assert metrics.other_income_pct_revenue == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_no_ttm_column_latest_fy_equals_trailing():
    metrics = await _financials_from(SCREENER_HTML)

    assert metrics.trailing_revenue_cr == pytest.approx(300000.0)
    assert metrics.revenue_latest_fy_cr == pytest.approx(300000.0)
    assert metrics.revenue_1y_ago_cr == pytest.approx(250000.0)


@pytest.mark.asyncio
async def test_ebitda_margin_parsed_from_pl_opm_row():
    """Live Screener pages carry "OPM %" in the P&L table, not in Ratios — so
    ebitda_margin_* was never populated.  Fiscal years only; TTM (20%) ignored."""
    metrics = await _financials_from(SCREENER_TTM_HTML)

    assert metrics.ebitda_margin_latest == pytest.approx(14.0)
    assert metrics.ebitda_margin_5y_avg == pytest.approx(12.0)  # < 5 years → mean of 3
    assert metrics.ebitda_margin_trend is None  # needs ≥ 5 years
