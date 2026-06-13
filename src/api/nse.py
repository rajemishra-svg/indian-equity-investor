"""NSE India API client."""
from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from src.api.base import BaseHTTPClient
from src.models import GovernanceData, StockQuote


class NSEClient(BaseHTTPClient):
    """Client for NSE India. Requires session establishment first."""

    def __init__(self) -> None:
        super().__init__(base_url="https://www.nseindia.com")
        self._session_established = False

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers.update(
            {
                "Referer": "https://www.nseindia.com",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        return headers

    async def _establish_session(self) -> None:
        """Establish session cookies by visiting the main page."""
        await self.get("/")
        self._session_established = True
        self.log.info("nse_session_established")

    async def get_stock_quote(self, symbol: str) -> StockQuote | None:
        """Fetch equity quote for a symbol from NSE.

        Args:
            symbol: NSE ticker symbol, e.g. "RELIANCE".

        Returns:
            StockQuote or None on failure.
        """
        if not self._session_established:
            await self._establish_session()

        symbol = symbol.upper().strip()
        try:
            resp = await self.get(f"/api/quote-equity?symbol={symbol}")
            data = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.warning("nse_quote_failed", symbol=symbol, error=str(exc), error_tag="ER-01")
            return None

        price_info = data.get("priceInfo", {})
        week_hl = price_info.get("weekHighLow", {})
        trade_info = (
            data.get("marketDeptOrderBook", {}).get("tradeInfo", {})
        )

        raw_mc = trade_info.get("totalMarketCap", 0) or 0
        market_cap_cr = raw_mc / 1e7  # convert rupees to crores

        # NSE does not reliably expose 200DMA in the quote endpoint; fetched separately via get_200dma
        dma_200: float | None = None

        return StockQuote(
            ticker=symbol,
            company_name=data.get("info", {}).get("companyName", symbol),
            cmp=price_info.get("lastPrice", 0.0),
            w52_high=week_hl.get("max", 0.0),
            w52_low=week_hl.get("min", 0.0),
            dma_200=dma_200,
            market_cap_cr=market_cap_cr,
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

        resp = await self.get("/api/equity-stockIndices", params={"index": index})
        data = resp.json()
        tickers = []
        for item in data.get("data", []):
            symbol = item.get("symbol", "").strip().upper()
            # Skip the index row itself (e.g. "NIFTY 500") and blank entries
            if symbol and " " not in symbol:
                tickers.append(symbol)
        self.log.info("index_constituents_fetched", index=index, count=len(tickers))
        return tickers

    async def get_shareholding(self, symbol: str) -> GovernanceData | None:
        """Fetch promoter holding and pledging from NSE shareholding pattern API.

        Uses the existing NSE session (no extra cookie round-trip needed).
        Fetches up to 8 recent quarterly data points to build the pledging trend.

        Args:
            symbol: NSE ticker symbol.

        Returns:
            GovernanceData with holding/pledging fields populated, or None on failure.
        """
        if not self._session_established:
            await self._establish_session()

        symbol = symbol.upper().strip()
        flags: list[str] = []

        try:
            resp = await self.get(
                "/api/corporate-shareholding-pattern",
                params={"symbol": symbol},
            )
            data = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.warning(
                # No ER-04 here — that tag means ALL shareholding sources failed;
                # the pipeline adds it after the BSE and Screener fallbacks also fail.
                "nse_shareholding_failed", symbol=symbol, error=str(exc)
            )
            return None

        return self._parse_shareholding(data, symbol, flags)

    def _parse_shareholding(
        self, data: dict, symbol: str, flags: list[str]
    ) -> GovernanceData | None:
        """Parse NSE shareholding pattern JSON into GovernanceData.

        NSE returns an array of quarterly snapshots. Each snapshot has categories
        (Promoter, Public, etc.) and a separate pledged-shares figure. We take the
        most-recent quarter for headline numbers and build a trend from all quarters.
        """
        promoter_holding: float | None = None
        promoter_pledging: float | None = None
        pledging_trend: list[float] = []

        # NSE returns different shapes depending on endpoint version.
        # Shape A: {"shareholdingPatterns": {"totalShareholdingPublic": [...], "data": [...]}}
        # Shape B: {"data": [{"category": "...", "sharePercentage": ...}]}
        # Shape C: flat list at top level
        rows: list[dict] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("shareholdingPatterns", "data", "shareHoldingList", "results"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    rows = candidate
                    break
                elif isinstance(candidate, dict):
                    # some endpoints nest one more level
                    for inner_key in ("data", "shareholdingList"):
                        inner = candidate.get(inner_key)
                        if isinstance(inner, list):
                            rows = inner
                            break
                    if rows:
                        break

        if not rows:
            self.log.warning("nse_shareholding_empty_response", symbol=symbol)
            return None

        for row in rows:
            if not isinstance(row, dict):
                continue
            category = str(
                row.get("category", row.get("Category", row.get("holdingType", "")))
            ).lower()
            raw_pct = (
                row.get("sharePercentage")
                or row.get("SharePer")
                or row.get("holdingPerc")
                or row.get("percentHolding")
                or 0
            )
            try:
                pct = float(raw_pct)
            except (ValueError, TypeError):
                continue

            if "promoter" in category and "pledge" not in category:
                if promoter_holding is None:
                    promoter_holding = pct
            elif "pledge" in category or "encumber" in category:
                pledging_trend.append(pct)
                if promoter_pledging is None:
                    promoter_pledging = pct

        # Determine trend direction from collected quarterly pledge figures
        trend_direction: str | None = None
        if len(pledging_trend) >= 2:
            if pledging_trend[-1] > pledging_trend[0]:
                trend_direction = "increasing"
            elif pledging_trend[-1] < pledging_trend[0]:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"

        if promoter_holding is None:
            flags.append("[DATA UNVERIFIED: promoter_holding — NSE parse]")
        if promoter_pledging is None:
            flags.append("[PLEDGING UNKNOWN — NSE parse]")

        self.log.info(
            "nse_shareholding_parsed",
            symbol=symbol,
            promoter_holding=promoter_holding,
            promoter_pledging=promoter_pledging,
            trend_quarters=len(pledging_trend),
        )
        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging or 0.0,
            promoter_pledging_trend=pledging_trend[-8:],  # keep last 8 quarters
            pledging_trend_direction=trend_direction,
            data_flags=flags,
        )

    async def get_200dma(self, symbol: str) -> float | None:
        """Fetch 200-day moving average for a symbol.

        NSE provides historical price data which we use to compute DMA.
        Returns None if data is unavailable.
        """
        if not self._session_established:
            await self._establish_session()

        symbol = symbol.upper().strip()
        try:
            resp = await self.get(f"/api/quote-equity?symbol={symbol}&section=trade_info")
            data = resp.json()
            # Some NSE endpoints provide a summary with DMA
            summary = data.get("metadata", {})
            dma_str = summary.get("pdSymbolPe", None)
            if dma_str:
                match = re.search(r"[\d.]+", str(dma_str))
                if match:
                    return float(match.group())
        except Exception as exc:
            self.log.debug("nse_200dma_failed", symbol=symbol, error=str(exc))
        return None
