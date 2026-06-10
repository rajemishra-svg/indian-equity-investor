"""Claude tool definitions and execution dispatcher."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re as _re
import socket
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.logging_config import get_logger

# Maximum redirect hops _web_fetch follows manually — each hop is re-validated
# so a public page cannot redirect the agent onto an internal endpoint.
_MAX_REDIRECTS = 5

log = get_logger("tools")


# ---------------------------------------------------------------------------
# SSRF guard — DNS-resolution based, applied to every fetch and redirect hop
# ---------------------------------------------------------------------------


def _resolves_to_non_global(host: str) -> bool:
    """True when ``host`` is, or resolves to, any non-global IP address.

    Catches what a string blocklist cannot: hostnames that DNS-resolve to
    internal IPs, decimal/octal IP encodings (``http://2130706433/``), IPv6
    loopback (``[::1]``), ``0.0.0.0``, link-local metadata endpoints, CGN
    ranges, and unresolvable hosts (blocked conservatively).

    Known limitation: resolve-then-fetch is not atomic, so a DNS-rebinding
    attacker controlling a domain's TTL could still race the check.  Full
    protection would require pinning the resolved IP into the transport.
    """
    bare = host.strip("[]")
    try:
        return not ipaddress.ip_address(bare).is_global
    except ValueError:
        pass  # not an IP literal — resolve it
    try:
        infos = socket.getaddrinfo(bare, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True  # unresolvable — treat as blocked
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if not ip.is_global:
            return True
    return False


async def _validate_fetch_url(url: str) -> str | None:
    """Return a [BLOCKED: ...] message when ``url`` must not be fetched, else None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"[BLOCKED: scheme '{parsed.scheme or 'none'}' not permitted — only http/https]"
    host = parsed.hostname
    if not host:
        return f"[BLOCKED: '{url}' has no resolvable host]"
    # getaddrinfo is blocking — keep the event loop free during DNS lookups.
    is_blocked = await asyncio.get_event_loop().run_in_executor(
        None, _resolves_to_non_global, host
    )
    if is_blocked:
        return (
            f"[BLOCKED: '{host}' is or resolves to a private/reserved address — "
            "not permitted]"
        )
    return None

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


def _sanitize_web_text(text: str, max_chars: int = 3500) -> str:
    """Strip prompt-injection patterns from external web content before sending to Claude.

    External pages can embed adversarial instructions like "Ignore previous instructions…".
    This function strips the most dangerous patterns and caps length to reduce token cost.
    """
    # Strip common prompt-injection openers (case-insensitive).
    # "act as " is intentionally narrowed to AI/chatbot variants to avoid
    # redacting legitimate financial phrases like "act as a catalyst".
    _INJECTION_PATTERNS = [
        r"ignore (all |your )?(previous|prior|above) instructions?",
        r"disregard (all |your )?(previous|prior|above) instructions?",
        r"you are now ",
        r"act as (?:an? )?(?:AI|assistant|language model|chatbot|different|new|another)",
        r"new instructions?:",
        r"system prompt:",
        r"<\|im_start\|>",
        r"<\|im_end\|>",
    ]
    cleaned = text
    for pat in _INJECTION_PATTERNS:
        cleaned = _re.sub(pat, "[REDACTED]", cleaned, flags=_re.IGNORECASE)
    return cleaned[:max_chars]


async def _web_search(query: str) -> str:
    """Perform a web search via DuckDuckGo lite."""
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
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
                entry = f"TITLE: {title}\nSNIPPET: {snippet}"
                results.append(_sanitize_web_text(entry, max_chars=500))
        if results:
            return "\n\n".join(results)
        return "[NO RESULTS FOUND]"
    except Exception as exc:
        return f"[SEARCH ERROR: {exc}]"


async def _web_fetch(url: str, extract_text: bool = True) -> str:
    """Fetch and extract clean text from a URL.

    Raw HTML is never sent to Claude — it always passes through BeautifulSoup
    text extraction to prevent prompt injection via adversarial page content.

    SSRF guard: the host is DNS-resolved and checked against non-global IP
    ranges before every request, and redirects are followed manually so each
    hop is re-validated — a public page cannot bounce the agent onto an
    internal endpoint.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=False,  # followed manually below, re-validating each hop
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                )
            },
        ) as client:
            resp = None
            for _ in range(_MAX_REDIRECTS + 1):
                blocked = await _validate_fetch_url(url)
                if blocked:
                    return blocked
                resp = await client.get(url)
                if resp.has_redirect_location:
                    url = urljoin(url, resp.headers["location"])
                    continue
                break
            else:
                return f"[FETCH ERROR: more than {_MAX_REDIRECTS} redirects]"
        # Always extract text — never send raw HTML to Claude regardless of extract_text flag.
        # Raw HTML can contain hidden adversarial instructions in script tags or comments.
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return _sanitize_web_text(text, max_chars=3500)
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
