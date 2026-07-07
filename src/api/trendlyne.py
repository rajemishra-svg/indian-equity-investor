"""Trendlyne client for historical valuation data and governance information."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.api.base import BaseHTTPClient
from src.models import GovernanceData, ValuationData


class TrendlyneClient(BaseHTTPClient):
    """Client for Trendlyne — fetches historical P/E range and ratios."""

    def __init__(self) -> None:
        super().__init__(base_url="https://trendlyne.com")

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers["Referer"] = "https://trendlyne.com"
        return headers

    async def get_valuation_data(self, ticker: str) -> ValuationData | None:
        """Fetch valuation multiples and historical ranges from Trendlyne.

        Args:
            ticker: NSE ticker symbol.

        Returns:
            ValuationData or None on failure.
        """
        ticker = ticker.upper().strip()
        url = f"/fundamentals/{ticker}/"
        flags: list[str] = []

        try:
            resp = await self.get(url)
        except Exception as exc:
            self.log.warning(
                "trendlyne_fetch_failed",
                ticker=ticker,
                error=str(exc),
                error_tag="ER-03",
            )
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_valuation(soup, ticker, flags)

    def _parse_valuation(
        self, soup: BeautifulSoup, ticker: str, flags: list[str]
    ) -> ValuationData:
        """Parse valuation metrics from Trendlyne HTML."""
        pe_current: float | None = None
        ev_ebitda_current: float | None = None
        pbv_current: float | None = None
        pe_10y_low: float | None = None
        pe_10y_high: float | None = None

        # Look for ratio cards / metric tables
        for element in soup.find_all(class_=re.compile(r"ratio|metric|valuation", re.I)):
            text = element.get_text(separator=" ", strip=True)
            # P/E ratio
            pe_match = re.search(r"P/E\s*[:\-]?\s*([\d.]+)", text, re.I)
            if pe_match and pe_current is None:
                try:
                    pe_current = float(pe_match.group(1))
                except ValueError:
                    pass
            # EV/EBITDA
            ev_match = re.search(r"EV/EBITDA\s*[:\-]?\s*([\d.]+)", text, re.I)
            if ev_match and ev_ebitda_current is None:
                try:
                    ev_ebitda_current = float(ev_match.group(1))
                except ValueError:
                    pass
            # P/B ratio
            pb_match = re.search(r"P/B\s*[:\-]?\s*([\d.]+)", text, re.I)
            if pb_match and pbv_current is None:
                try:
                    pbv_current = float(pb_match.group(1))
                except ValueError:
                    pass

        # Historical P/E range (10Y)
        for element in soup.find_all(string=re.compile(r"10.?year|historical", re.I)):
            parent = element.parent
            if parent:
                text = parent.get_text(separator=" ", strip=True)
                low_match = re.search(r"low\s*[:\-]?\s*([\d.]+)", text, re.I)
                high_match = re.search(r"high\s*[:\-]?\s*([\d.]+)", text, re.I)
                if low_match:
                    try:
                        pe_10y_low = float(low_match.group(1))
                    except ValueError:
                        pass
                if high_match:
                    try:
                        pe_10y_high = float(high_match.group(1))
                    except ValueError:
                        pass

        # Compute 10Y percentile
        pe_10y_percentile: float | None = None
        if (
            pe_10y_low is not None
            and pe_10y_high is not None
            and pe_current is not None
            and pe_10y_high != pe_10y_low
        ):
            pe_10y_percentile = (pe_current - pe_10y_low) / (pe_10y_high - pe_10y_low) * 100

        # Flag missing critical fields
        if pe_current is None:
            flags.append("[DATA UNVERIFIED: pe_current]")
        if pe_10y_low is None or pe_10y_high is None:
            flags.append("[DATA UNVERIFIED: pe_10y_range]")

        return ValuationData(
            pe_current=pe_current,
            ev_ebitda_current=ev_ebitda_current,
            pbv_current=pbv_current,
            pe_10y_percentile=pe_10y_percentile,
            pe_10y_low=pe_10y_low,
            pe_10y_high=pe_10y_high,
            data_flags=flags,
        )

    async def get_governance_data(self, ticker: str) -> GovernanceData | None:
        """Fetch governance data from Trendlyne fundamentals/governance page.

        Extracts auditor name, promoter pledging %, and governance scores.
        Used as a fallback when BSE/NSE shareholding APIs fail.

        Args:
            ticker: NSE ticker symbol.

        Returns:
            GovernanceData with available fields, or None on failure.
        """
        ticker = ticker.upper().strip()
        flags: list[str] = []

        try:
            resp = await self.get(f"/fundamentals/governance/{ticker}/")
        except Exception as exc:
            self.log.warning(
                # Governance enrichment is not a shareholding source — ER-04 does not apply.
                "trendlyne_governance_failed", ticker=ticker, error=str(exc)
            )
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_governance(soup, ticker, flags)

    def _parse_governance(
        self, soup: BeautifulSoup, ticker: str, flags: list[str]
    ) -> GovernanceData:
        """Parse auditor, pledging, and governance score from Trendlyne HTML."""
        auditor_name: str | None = None
        promoter_holding: float | None = None
        promoter_pledging: float | None = None

        full_text = soup.get_text(separator=" ", strip=True)

        # --- Auditor name ---
        # Trendlyne shows auditor in a labelled row; try broad patterns first
        auditor_patterns = [
            r"[Ss]tatutory\s+[Aa]uditor[s]?\s*[:\-]?\s*([A-Z][A-Za-z &.,\-]+?)(?:\s{2,}|\n|<)",
            r"[Aa]uditor[s]?\s*[:\-]\s*([A-Z][A-Za-z &.,\-]{5,60})",
            r"[Aa]udited\s+by\s+([A-Z][A-Za-z &.,\-]{5,60})",
        ]
        for pat in auditor_patterns:
            m = re.search(pat, full_text)
            if m:
                candidate = m.group(1).strip().rstrip(".,")
                if 4 < len(candidate) < 80:
                    auditor_name = candidate
                    break

        # Structured HTML: look for table/div rows with "Auditor" label
        if auditor_name is None:
            for tag in soup.find_all(string=re.compile(r"auditor", re.I)):
                parent = tag.find_parent()
                if parent:
                    sibling = parent.find_next_sibling()
                    if sibling:
                        text = sibling.get_text(strip=True)
                        if 4 < len(text) < 80 and text[0].isupper():
                            auditor_name = text
                            break

        # --- Promoter holding / pledging ---
        hold_m = re.search(
            r"[Pp]romoter\s+[Hh]olding\s*[:\-]?\s*([\d.]+)\s*%", full_text
        )
        if hold_m:
            try:
                promoter_holding = float(hold_m.group(1))
            except ValueError:
                pass

        pledge_m = re.search(
            r"[Pp]ledge[d]?\s*(?:shares?\s*)?[:\-]?\s*([\d.]+)\s*%", full_text
        )
        if pledge_m:
            try:
                promoter_pledging = float(pledge_m.group(1))
            except ValueError:
                pass

        # Fallback: scan all metric rows for pledging
        if promoter_pledging is None:
            for element in soup.find_all(string=re.compile(r"pledge", re.I)):
                parent = element.find_parent()
                if parent:
                    pct_match = re.search(r"([\d.]+)\s*%", parent.get_text())
                    if pct_match:
                        try:
                            promoter_pledging = float(pct_match.group(1))
                            break
                        except ValueError:
                            pass

        if auditor_name is None:
            flags.append("[DATA UNVERIFIED: auditor_name — Trendlyne parse]")
        if promoter_holding is None:
            flags.append("[DATA UNVERIFIED: promoter_holding — Trendlyne parse]")
        if promoter_pledging is None:
            flags.append("[PLEDGING UNKNOWN — Trendlyne parse]")

        self.log.info(
            "trendlyne_governance_parsed",
            ticker=ticker,
            auditor_name=auditor_name,
            promoter_holding=promoter_holding,
            promoter_pledging=promoter_pledging,
        )
        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging or 0.0,
            auditor_name=auditor_name,
            data_flags=flags,
        )
