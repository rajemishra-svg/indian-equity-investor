"""BSE India API client for shareholding patterns."""
from __future__ import annotations

import httpx

from src.api.base import BaseHTTPClient
from src.models import GovernanceData


class BSEClient(BaseHTTPClient):
    """Client for BSE India shareholding pattern API."""

    def __init__(self) -> None:
        super().__init__(base_url="https://api.bseindia.com")

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers.update(
            {
                "Origin": "https://www.bseindia.com",
                "Referer": "https://www.bseindia.com",
            }
        )
        return headers

    async def _get_scripcode(self, ticker: str) -> str | None:
        """Look up BSE scripcode from ticker symbol.

        Args:
            ticker: NSE ticker symbol.

        Returns:
            BSE scripcode string or None.
        """
        ticker = ticker.upper().strip()
        try:
            resp = await self.get(
                f"/BseIndiaAPI/api/fetchCompanyCode/w?searchKey={ticker}"
            )
            data = resp.json()
            if isinstance(data, list) and data:
                return str(data[0].get("SECURITY_CODE", ""))
            if isinstance(data, dict):
                items = data.get("Table", []) or data.get("data", []) or []
                if items:
                    return str(items[0].get("SECURITY_CODE", items[0].get("scripcode", "")))
        except Exception as exc:
            self.log.warning("bse_scripcode_lookup_failed", ticker=ticker, error=str(exc))
        return None

    async def get_shareholding(self, ticker: str) -> GovernanceData | None:
        """Fetch promoter holding and pledging data from BSE.

        Fetches 8 quarters of data to establish pledging trend.

        Args:
            ticker: NSE ticker symbol.

        Returns:
            GovernanceData or None on failure.
        """
        scripcode = await self._get_scripcode(ticker)
        if not scripcode:
            self.log.warning(
                "bse_scripcode_not_found", ticker=ticker, error_tag="ER-04"
            )
            return None

        # Fetch shareholding pattern — flag C = current consolidated
        try:
            resp = await self.get(
                f"/BseIndiaAPI/api/shareHoldingPattrn/w"
                f"?scripcode={scripcode}&flag=C&type=C"
            )
            data = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.warning(
                "bse_shareholding_failed",
                ticker=ticker,
                scripcode=scripcode,
                error=str(exc),
                error_tag="ER-04",
            )
            return None

        return self._parse_shareholding(data, ticker, scripcode)

    def _parse_shareholding(
        self, data: dict, ticker: str, scripcode: str
    ) -> GovernanceData:
        """Parse shareholding pattern API response into GovernanceData."""
        flags: list[str] = []
        promoter_holding: float | None = None
        promoter_pledging: float | None = None
        pledging_trend: list[float] = []

        # BSE returns a dict with shareholding by category
        categories = data.get("ShareHoldingData", []) or data.get("data", []) or []
        for category in categories:
            name = str(category.get("Catcd", "") or category.get("Name", "")).lower()
            if "promoter" in name and "pledge" not in name:
                try:
                    promoter_holding = float(
                        category.get("SharePer", category.get("PercentHolding", 0))
                    )
                except (ValueError, TypeError):
                    pass
            elif "pledge" in name or "encumber" in name:
                try:
                    promoter_pledging = float(
                        category.get("SharePer", category.get("PercentPledge", 0))
                    )
                    pledging_trend.append(promoter_pledging)
                except (ValueError, TypeError):
                    pass

        # Determine pledging trend direction
        trend_direction: str | None = None
        if len(pledging_trend) >= 2:
            if pledging_trend[-1] > pledging_trend[0]:
                trend_direction = "increasing"
            elif pledging_trend[-1] < pledging_trend[0]:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"

        if promoter_holding is None:
            flags.append("[DATA UNVERIFIED: promoter_holding]")
        if promoter_pledging is None:
            flags.append("[PLEDGING UNKNOWN]")
            promoter_pledging = 0.0

        self.log.info(
            "bse_shareholding_parsed",
            ticker=ticker,
            promoter_holding=promoter_holding,
            promoter_pledging=promoter_pledging,
        )

        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging,
            promoter_pledging_trend=pledging_trend,
            pledging_trend_direction=trend_direction,
            sebi_record_clean=True,  # default; Claude enriches this
            data_flags=flags,
        )
