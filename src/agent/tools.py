"""Claude tool definitions and execution dispatcher."""
from __future__ import annotations

import json
from typing import Any

import httpx
from bs4 import BeautifulSoup

from src.logging_config import get_logger

log = get_logger("tools")

# ---------------------------------------------------------------------------
# Tool schemas for Claude API
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "web_search",
        "description": (
            "Search the web for financial data, news, or information about an Indian stock. "
            "Use for management interviews, concall summaries, SEBI orders, sector news."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch content from a URL. Use for NSE/BSE filings, Screener.in, Trendlyne, "
            "annual reports, and any financial webpage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to fetch",
                },
                "extract_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, extract clean text; otherwise return raw HTML.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_stock_quote",
        "description": (
            "Get real-time stock quote from NSE India. "
            "Returns CMP, 52W high/low, market cap, and exchange."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "NSE ticker symbol, e.g. RELIANCE",
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_financial_data",
        "description": (
            "Get financial ratios and historical data from Screener.in "
            "(consolidated financials — revenue CAGR, ROE, ROCE, D/E, CFO/NP)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "NSE/BSE ticker symbol",
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_shareholding_data",
        "description": (
            "Get promoter holding and pledging data from BSE shareholding patterns "
            "(8 quarters of history for trend analysis)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "NSE ticker symbol",
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_valuation_data",
        "description": (
            "Get current and historical valuation multiples "
            "(P/E 10Y range, EV/EBITDA, P/B) from Trendlyne."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "NSE ticker symbol",
                }
            },
            "required": ["ticker"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


async def execute_tool(tool_name: str, tool_input: dict[str, Any], clients: dict) -> str:
    """Dispatch a tool call to the appropriate handler.

    Args:
        tool_name: Name of the tool to execute.
        tool_input: Parameters dict.
        clients: Dict of initialised API clients (nse, screener, bse, trendlyne).

    Returns:
        String result to pass back to Claude.
    """
    log.info("tool_execute", tool=tool_name, input=tool_input)
    try:
        if tool_name == "web_search":
            return await _web_search(tool_input["query"])
        elif tool_name == "web_fetch":
            return await _web_fetch(
                tool_input["url"],
                tool_input.get("extract_text", True),
            )
        elif tool_name == "get_stock_quote":
            return await _get_stock_quote(tool_input["ticker"], clients)
        elif tool_name == "get_financial_data":
            return await _get_financial_data(tool_input["ticker"], clients)
        elif tool_name == "get_shareholding_data":
            return await _get_shareholding_data(tool_input["ticker"], clients)
        elif tool_name == "get_valuation_data":
            return await _get_valuation_data(tool_input["ticker"], clients)
        else:
            return f"[ERROR: Unknown tool '{tool_name}']"
    except Exception as exc:
        log.warning("tool_error", tool=tool_name, error=str(exc))
        return f"[ERROR executing {tool_name}: {exc}]"


async def _web_search(query: str) -> str:
    """Perform a web search via DuckDuckGo lite."""
    encoded = query.replace(" ", "+")
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36"
                    )
                },
            )
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for result in soup.find_all("div", class_="result")[:5]:
            title_tag = result.find("a", class_="result__a")
            snippet_tag = result.find("a", class_="result__snippet")
            if title_tag:
                title = title_tag.get_text(strip=True)
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                results.append(f"TITLE: {title}\nSNIPPET: {snippet}")
        if results:
            return "\n\n".join(results)
        return "[NO RESULTS FOUND]"
    except Exception as exc:
        return f"[SEARCH ERROR: {exc}]"


async def _web_fetch(url: str, extract_text: bool = True) -> str:
    """Fetch and optionally extract clean text from a URL."""
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                )
            },
        ) as client:
            resp = await client.get(url)
        if extract_text:
            soup = BeautifulSoup(resp.text, "lxml")
            # Remove scripts and styles
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # Truncate to 4000 chars to reduce context tokens sent to Claude
            return text[:4000]
        return resp.text[:4000]
    except Exception as exc:
        return f"[FETCH ERROR: {exc}]"


async def _get_stock_quote(ticker: str, clients: dict) -> str:
    """Get stock quote via NSE client."""
    nse = clients.get("nse")
    if nse is None:
        return "[ERROR: NSE client not available]"
    quote = await nse.get_stock_quote(ticker)
    if quote is None:
        return f"[NOT AVAILABLE: Could not fetch quote for {ticker}]"
    return json.dumps(
        {
            "ticker": quote.ticker,
            "company_name": quote.company_name,
            "cmp": quote.cmp,
            "w52_high": quote.w52_high,
            "w52_low": quote.w52_low,
            "market_cap_cr": quote.market_cap_cr,
            "exchange": quote.exchange,
        }
    )


async def _get_financial_data(ticker: str, clients: dict) -> str:
    """Get financial metrics via Screener client."""
    screener = clients.get("screener")
    if screener is None:
        return "[ERROR: Screener client not available]"
    metrics = await screener.get_financials(ticker)
    if metrics is None:
        return f"[NOT AVAILABLE: Could not fetch financials for {ticker}]"
    return json.dumps(
        {
            "revenue_cagr_5y": metrics.revenue_cagr_5y,
            "revenue_cagr_3y": metrics.revenue_cagr_3y,
            "pat_cagr_5y": metrics.pat_cagr_5y,
            "pat_cagr_3y": metrics.pat_cagr_3y,
            "roe_5y_avg": metrics.roe_5y_avg,
            "roce_5y_avg": metrics.roce_5y_avg,
            "cfo_net_profit_3y_avg": metrics.cfo_net_profit_3y_avg,
            "debt_to_equity": metrics.debt_to_equity,
            "interest_coverage": metrics.interest_coverage,
            "ebitda_margin_latest": metrics.ebitda_margin_latest,
            "data_flags": metrics.data_flags,
        }
    )


async def _get_shareholding_data(ticker: str, clients: dict) -> str:
    """Get shareholding pattern via BSE client."""
    bse = clients.get("bse")
    if bse is None:
        return "[ERROR: BSE client not available]"
    governance = await bse.get_shareholding(ticker)
    if governance is None:
        return f"[NOT AVAILABLE: Could not fetch shareholding for {ticker}]"
    return json.dumps(
        {
            "promoter_holding_pct": governance.promoter_holding_pct,
            "promoter_pledging_pct": governance.promoter_pledging_pct,
            "pledging_trend": governance.promoter_pledging_trend,
            "pledging_trend_direction": governance.pledging_trend_direction,
            "data_flags": governance.data_flags,
        }
    )


async def _get_valuation_data(ticker: str, clients: dict) -> str:
    """Get valuation multiples via Trendlyne client."""
    trendlyne = clients.get("trendlyne")
    if trendlyne is None:
        return "[ERROR: Trendlyne client not available]"
    valuation = await trendlyne.get_valuation_data(ticker)
    if valuation is None:
        return f"[NOT AVAILABLE: Could not fetch valuation data for {ticker}]"
    return json.dumps(
        {
            "pe_current": valuation.pe_current,
            "ev_ebitda_current": valuation.ev_ebitda_current,
            "pbv_current": valuation.pbv_current,
            "pe_10y_low": valuation.pe_10y_low,
            "pe_10y_high": valuation.pe_10y_high,
            "pe_10y_percentile": valuation.pe_10y_percentile,
            "data_flags": valuation.data_flags,
        }
    )
