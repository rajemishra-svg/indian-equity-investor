"""NSE India API client.

Endpoint notes (verified Sep 2026):

* ``/api/quote-equity`` is now behind Akamai bot protection (403 without a
  browser-issued sensor cookie). The quote page itself uses the NextApi proxy
  ``/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData`` which
  answers plain HTTP clients.
* ``/api/equity-stockIndices`` and ``/api/corporate-shareholding-pattern`` were
  retired (404). Replacements: NextApi ``marketWatchApi?functionName=getIndicesData``
  and ``/api/corporate-share-holdings-master`` (one row per quarterly SHP filing,
  each linking its XBRL, which carries the promoter pledge figures).
* The homepage often returns 403 to non-browser clients; the APIs above don't
  need its cookies, so the session visit is best-effort.
* NSE serves brotli when offered; httpx can't decode it without the optional
  ``brotli`` package, so only gzip/deflate are advertised.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from lxml import etree

from src.api.base import BaseHTTPClient
from src.models import GovernanceData, StockQuote

_ARCHIVE_PREFIX = "https://nsearchives.nseindia.com/"
_TREND_QUARTERS = 4  # quarters of SHP XBRL fetched for the pledging trend (matches BSE)
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _to_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_shp_pledge(xml: bytes) -> float | None:
    """Promoter-group shares pledged, as % of the promoter group's own holding.

    Same basis as BSE's ``Fld_PledgeEncumberedPercentage`` (SHP Table I column
    "as a % of total shares held"). Returns 0.0 when the filing declares no
    promoter pledge, None when the filing can't answer either way.
    """
    root = etree.fromstring(xml, parser=_XML_PARSER)
    declared: bool | None = None
    pledged_pct: float | None = None
    for el in root:
        if not isinstance(el.tag, str):
            continue
        name = el.tag.rsplit("}", 1)[-1]
        text = (el.text or "").strip()
        if name == "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup":
            declared = text.lower() == "true"
        elif (
            name == "EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares"
            and el.get("contextRef") == "ShareholdingOfPromoterAndPromoterGroup_ContextI"
        ):
            val = _to_float(text)
            if val is not None:
                pledged_pct = round(val * 100, 2)  # XBRL stores fractions (0.116 = 11.6%)
    if pledged_pct is not None:
        return pledged_pct
    if declared is False:
        return 0.0
    return None


class NSEClient(BaseHTTPClient):
    """Client for NSE India public JSON APIs."""

    def __init__(self) -> None:
        super().__init__(base_url="https://www.nseindia.com")
        self._session_established = False

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers.update(
            {
                "Referer": "https://www.nseindia.com",
                "X-Requested-With": "XMLHttpRequest",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        return headers

    async def _establish_session(self) -> None:
        """Best-effort cookie visit to the homepage.

        NSE frequently 403s the homepage for non-browser clients while its data
        APIs still answer, so a failure here is logged and ignored rather than
        aborting the real request.
        """
        self._session_established = True
        try:
            await self.get("/")
            self.log.info("nse_session_established")
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            self.log.debug("nse_session_warmup_failed", error=str(exc))

    async def get_stock_quote(self, symbol: str) -> StockQuote | None:
        """Fetch equity quote for a symbol from NSE.

        Only live-price fields are populated; history-derived fields (200-DMA,
        average traded value, volume trend) are left None for the caller to
        backfill from a history source.

        Args:
            symbol: NSE ticker symbol, e.g. "RELIANCE".

        Returns:
            StockQuote or None on failure.
        """
        if not self._session_established:
            await self._establish_session()

        symbol = symbol.upper().strip()
        try:
            resp = await self.get(
                "/api/NextApi/apiClient/GetQuoteApi",
                params={
                    "functionName": "getSymbolData",
                    "marketType": "N",
                    "series": "EQ",
                    "symbol": symbol,
                },
            )
            data = resp.json()
            row = (data.get("equityResponse") or [None])[0]
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError, AttributeError) as exc:
            self.log.warning("nse_quote_failed", symbol=symbol, error=str(exc), error_tag="ER-01")
            return None

        if not isinstance(row, dict):
            self.log.warning("nse_quote_empty", symbol=symbol, error_tag="ER-01")
            return None

        meta = row.get("metaData") or {}
        trade = row.get("tradeInfo") or {}
        price = row.get("priceInfo") or {}
        cmp = _to_float(trade.get("lastPrice")) or _to_float(meta.get("closePrice"))
        if not cmp:
            self.log.warning("nse_quote_no_price", symbol=symbol, error_tag="ER-01")
            return None

        raw_mc = _to_float(trade.get("totalMarketCap")) or 0.0
        return StockQuote(
            ticker=symbol,
            company_name=meta.get("companyName") or symbol,
            cmp=cmp,
            w52_high=_to_float(price.get("yearHigh")) or 0.0,
            w52_low=_to_float(price.get("yearLow")) or 0.0,
            dma_200=None,
            market_cap_cr=raw_mc / 1e7,  # rupees → crores
            exchange="NSE",
            data_timestamp=datetime.now(UTC),
            is_stale=False,
        )

    async def get_nifty50(self) -> tuple[float, float]:
        """Fetch Nifty 50 current level and 52-week high.

        Returns:
            Tuple of (current_level, 52w_high).

        Raises:
            ValueError: If Nifty 50 index not found.
            httpx.RequestError: On network failure (let caller handle).
        """
        if not self._session_established:
            await self._establish_session()

        resp = await self.get("/api/allIndices")
        data = resp.json()
        for index in data.get("data", []):
            if index.get("index") == "NIFTY 50":
                current = float(index.get("last", 0))
                year_high = float(index.get("yearHigh", 0))
                self.log.debug("nifty50_fetched", current=current, year_high=year_high)
                return current, year_high
        raise ValueError("Nifty 50 not found in NSE index data")

    async def get_index_constituents(self, index: str = "NIFTY 500") -> list[str]:
        """Fetch all constituent ticker symbols for an NSE index.

        Args:
            index: Index name, e.g. "NIFTY 500", "NIFTY 100", "NIFTY 50".

        Returns:
            List of NSE ticker symbols (uppercase, no spaces).

        Raises:
            httpx.RequestError / ValueError on network or parse failure.
        """
        if not self._session_established:
            await self._establish_session()

        resp = await self.get(
            "/api/NextApi/apiClient/marketWatchApi",
            params={"functionName": "getIndicesData", "symbol": index},
        )
        payload = resp.json().get("data") or {}
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        tickers = []
        for item in rows:
            symbol = str(item.get("symbol", "")).strip().upper()
            # The first row is the index itself (no series); skip it and blanks
            if symbol and item.get("series") and " " not in symbol:
                tickers.append(symbol)
        if not tickers:
            raise ValueError(f"No constituents returned for index {index!r}")
        self.log.info("index_constituents_fetched", index=index, count=len(tickers))
        return tickers

    async def get_shareholding(self, symbol: str) -> GovernanceData | None:
        """Fetch promoter holding and pledging from NSE shareholding-pattern filings.

        Holding and public % come from the SHP master list (8 quarters); the
        pledge % comes from each quarter's SHP XBRL (latest ``_TREND_QUARTERS``).

        Args:
            symbol: NSE ticker symbol.

        Returns:
            GovernanceData with holding/pledging fields populated, or None on failure.
        """
        if not self._session_established:
            await self._establish_session()

        symbol = symbol.upper().strip()
        try:
            resp = await self.get(
                "/api/corporate-share-holdings-master",
                params={"index": "equities", "symbol": symbol},
            )
            rows = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.warning(
                # No ER-04 here — that tag means ALL shareholding sources failed;
                # the pipeline adds it after the BSE and Screener fallbacks also fail.
                "nse_shareholding_failed", symbol=symbol, error=str(exc)
            )
            return None

        quarters = self._latest_filing_per_quarter(rows)
        if not quarters:
            self.log.warning("nse_shareholding_empty_response", symbol=symbol)
            return None

        pledges = await asyncio.gather(
            *(self._fetch_pledge(q.get("xbrl")) for q in quarters[:_TREND_QUARTERS]),
            return_exceptions=True,
        )
        return self._build_governance(symbol, quarters, pledges)

    @staticmethod
    def _latest_filing_per_quarter(rows: object) -> list[dict]:
        """Newest-first, one filing per quarter (a revision supersedes the original)."""
        if not isinstance(rows, list):
            return []
        by_quarter: dict[datetime, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                qdate = datetime.strptime(str(row.get("date", "")).title(), "%d-%b-%Y")
            except ValueError:
                continue
            if _to_float(row.get("pr_and_prgrp")) is None:
                continue
            prev = by_quarter.get(qdate)
            if prev is None or str(row.get("broadcastDate", "")) > str(prev.get("broadcastDate", "")):
                by_quarter[qdate] = row
        return [by_quarter[d] for d in sorted(by_quarter, reverse=True)]

    async def _fetch_pledge(self, url: object) -> float | None:
        if not isinstance(url, str) or not url.startswith(_ARCHIVE_PREFIX):
            return None
        try:
            resp = await self.get(url)
            return parse_shp_pledge(resp.content)
        except (httpx.HTTPStatusError, httpx.RequestError, etree.XMLSyntaxError) as exc:
            self.log.debug("nse_shp_xbrl_failed", url=url, error=str(exc))
            return None

    def _build_governance(
        self, symbol: str, quarters: list[dict], pledges: list[float | None | BaseException]
    ) -> GovernanceData:
        flags: list[str] = []
        promoter_holding = _to_float(quarters[0].get("pr_and_prgrp"))
        public_holding = _to_float(quarters[0].get("public_val"))

        pledge_values = [None if isinstance(p, BaseException) else p for p in pledges]
        promoter_pledging = pledge_values[0] if pledge_values else None

        # Trend lists are chronological: oldest first, latest last.
        pledging_trend = [p for p in reversed(pledge_values) if p is not None]
        trend_direction: str | None = None
        if len(pledging_trend) >= 2:
            if pledging_trend[-1] > pledging_trend[0]:
                trend_direction = "increasing"
            elif pledging_trend[-1] < pledging_trend[0]:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"

        recent = quarters[:8]
        holding_trend = [
            h for h in (_to_float(q.get("pr_and_prgrp")) for q in reversed(recent)) if h is not None
        ]
        public_trend = [
            p for p in (_to_float(q.get("public_val")) for q in reversed(recent)) if p is not None
        ]

        if promoter_pledging is None:
            # Unknown ≠ zero: leave None so Step 1 flags it instead of scoring a clean 0%.
            flags.append("[PLEDGING UNKNOWN — NSE SHP XBRL]")

        self.log.info(
            "nse_shareholding_parsed",
            symbol=symbol,
            promoter_holding=promoter_holding,
            promoter_pledging=promoter_pledging,
            trend_quarters=len(pledging_trend),
        )
        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging,
            promoter_pledging_trend=pledging_trend,
            pledging_trend_direction=trend_direction,
            promoter_holding_trend=holding_trend,
            public_holding_pct=public_holding,
            public_holding_trend=public_trend,
            data_flags=flags,
        )
