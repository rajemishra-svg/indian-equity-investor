"""Screener.in HTML scraper for financial metrics."""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from src.api.base import BaseHTTPClient
from src.models import FinancialMetrics, GovernanceData


class ScreenerClient(BaseHTTPClient):
    """Scraper for Screener.in consolidated financial data."""

    def __init__(self) -> None:
        super().__init__(base_url="https://www.screener.in")

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers["Referer"] = "https://www.screener.in"
        return headers

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """Scrape financial metrics from Screener.in consolidated page.

        Args:
            ticker: NSE/BSE ticker symbol.

        Returns:
            FinancialMetrics or None on failure.
        """
        ticker = ticker.upper().strip()
        url = f"/company/{ticker}/consolidated/"
        flags: list[str] = []

        try:
            resp = await self._fetch_with_rate_limit_handling(url)
        except Exception as exc:
            self.log.warning("screener_fetch_failed", ticker=ticker, error=str(exc), error_tag="ER-02")
            return None

        if resp is None:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_financials(soup, ticker)

    async def _fetch_with_rate_limit_handling(self, url: str) -> Optional[httpx.Response]:
        """Handle 429 rate limiting with a mandatory 90-second wait."""
        try:
            resp = await self.get(url)
            return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                self.log.warning("screener_rate_limited", wait_seconds=90)
                await asyncio.sleep(90)
                resp = await self.get(url)
                return resp
            raise

    def _parse_financials(
        self, soup: BeautifulSoup, ticker: str
    ) -> FinancialMetrics:
        """Parse financial data from Screener HTML page."""
        flags: list[str] = []
        metrics: dict = {}

        pl_section = soup.find("section", id="profit-loss")
        ratios_section = soup.find("section", id="ratios")
        balance_section = soup.find("section", id="balance-sheet")
        cashflow_section = soup.find("section", id="cash-flow")

        metrics.update(self._extract_top_ratios(soup))
        if pl_section:
            metrics.update(self._extract_growth_rates(pl_section))
        if ratios_section:
            metrics.update(self._extract_ratios(ratios_section))
        if balance_section:
            metrics.update(self._extract_balance_sheet(balance_section))
        if cashflow_section and pl_section:
            metrics.update(self._extract_cashflow_ratio(cashflow_section, pl_section))

        required = [
            "revenue_cagr_5y", "pat_cagr_5y", "roe_5y_avg",
            "roce_5y_avg", "debt_to_equity",
        ]
        for key in required:
            if metrics.get(key) is None:
                flags.append(f"[DATA UNVERIFIED: {key}]")

        return FinancialMetrics(
            market_cap_cr=metrics.get("market_cap_cr"),
            revenue_cagr_5y=metrics.get("revenue_cagr_5y"),
            revenue_cagr_3y=metrics.get("revenue_cagr_3y"),
            pat_cagr_5y=metrics.get("pat_cagr_5y"),
            pat_cagr_3y=metrics.get("pat_cagr_3y"),
            roe_5y_avg=metrics.get("roe_5y_avg"),
            roce_5y_avg=metrics.get("roce_5y_avg"),
            cfo_net_profit_3y_avg=metrics.get("cfo_net_profit_3y_avg"),
            debt_to_equity=metrics.get("debt_to_equity"),
            interest_coverage=metrics.get("interest_coverage"),
            current_ratio=metrics.get("current_ratio"),
            net_debt_ebitda=metrics.get("net_debt_ebitda"),
            ebitda_margin_latest=metrics.get("ebitda_margin_latest"),
            data_flags=flags,
        )

    def _extract_top_ratios(self, soup: BeautifulSoup) -> dict:
        """Extract market cap, ROE, ROCE from the top company summary strip."""
        result: dict = {}
        for li in soup.select("#top-ratios li"):
            text = li.get_text(separator=" ", strip=True)
            lower = text.lower()
            if "market cap" in lower:
                # e.g. "Market Cap ₹ 8,19,190 Cr."
                match = re.search(r"[\d,]+", text.replace("₹", ""))
                if match:
                    try:
                        result["market_cap_cr"] = float(match.group().replace(",", ""))
                    except ValueError:
                        pass
        return result

    def _extract_growth_rates(self, section: BeautifulSoup) -> dict:
        """Extract revenue/PAT CAGRs and ROE from the ranges-tables in P&L section.

        Screener uses <table class="ranges-table"> for Compounded Sales Growth,
        Compounded Profit Growth, and Return on Equity — each with a header cell
        and rows like "5 Years: 10%".
        """
        result: dict = {}
        for table in section.find_all("table", class_="ranges-table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header_cell = rows[0].find(["th", "td"])
            if not header_cell:
                continue
            header_text = header_cell.get_text(strip=True).lower()

            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(strip=True).lower()
                value_text = cells[1].get_text(strip=True).replace("%", "").strip()
                try:
                    value = float(value_text)
                except (ValueError, AttributeError):
                    continue

                if "sales" in header_text or "revenue" in header_text:
                    if "5 year" in label:
                        result["revenue_cagr_5y"] = value
                    elif "3 year" in label:
                        result["revenue_cagr_3y"] = value
                elif "profit" in header_text and "stock" not in header_text:
                    if "5 year" in label:
                        result["pat_cagr_5y"] = value
                    elif "3 year" in label:
                        result["pat_cagr_3y"] = value
                elif "return on equity" in header_text or "roe" in header_text:
                    if "5 year" in label:
                        result["roe_5y_avg"] = value
                    elif "last year" in label and "roe_5y_avg" not in result:
                        result["roe_5y_avg"] = value
        return result

    def _extract_ratios(self, section: BeautifulSoup) -> dict:
        """Extract ROE, ROCE, and other ratios from the Ratios section."""
        result: dict = {}
        tables = section.find_all("table", class_=re.compile(r"data-table"))
        for table in tables:
            header_row = table.find("tr")
            if not header_row:
                continue
            headers = [th.get_text(strip=True) for th in header_row.find_all("th")]
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).lower()
                values = [c.get_text(strip=True).replace("%", "").strip() for c in cells[1:]]
                float_vals = []
                for v in values:
                    try:
                        float_vals.append(float(v))
                    except ValueError:
                        float_vals.append(None)  # type: ignore[arg-type]

                if "return on equity" in label or "roe" in label:
                    valid = [v for v in float_vals if v is not None]
                    if len(valid) >= 5:
                        result["roe_5y_avg"] = sum(valid[-5:]) / 5
                    elif valid:
                        result["roe_5y_avg"] = sum(valid) / len(valid)

                elif "return on capital" in label or "roce" in label:
                    valid = [v for v in float_vals if v is not None]
                    if len(valid) >= 5:
                        result["roce_5y_avg"] = sum(valid[-5:]) / 5
                    elif valid:
                        result["roce_5y_avg"] = sum(valid) / len(valid)

                elif "debt / equity" in label or "d/e" in label:
                    if float_vals and float_vals[-1] is not None:
                        result["debt_to_equity"] = float_vals[-1]

                elif "interest coverage" in label:
                    if float_vals and float_vals[-1] is not None:
                        result["interest_coverage"] = float_vals[-1]

                elif "current ratio" in label:
                    if float_vals and float_vals[-1] is not None:
                        result["current_ratio"] = float_vals[-1]

                elif "ebitda margin" in label or "opm" in label:
                    if float_vals and float_vals[-1] is not None:
                        result["ebitda_margin_latest"] = float_vals[-1]

        return result

    def _extract_balance_sheet(self, section: BeautifulSoup) -> dict:
        """Compute D/E from balance sheet data (Borrowings / Equity+Reserves)."""
        result: dict = {}
        borrowings: Optional[float] = None
        equity_capital: Optional[float] = None
        reserves: Optional[float] = None

        main_table = section.find("table", class_="data-table")
        if main_table:
            for row in main_table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).lower()
                vals = []
                for c in cells[1:]:
                    try:
                        vals.append(float(c.get_text(strip=True).replace(",", "")))
                    except ValueError:
                        pass

                if not vals:
                    continue
                latest = vals[-1]

                if "borrowings" in label or "total debt" in label:
                    borrowings = latest
                elif label in ("equity capital", "share capital"):
                    equity_capital = latest
                elif "reserves" in label:
                    reserves = latest

        if borrowings is not None and equity_capital is not None and reserves is not None:
            total_equity = equity_capital + reserves
            if total_equity > 0:
                result["debt_to_equity"] = round(borrowings / total_equity, 2)

        return result

    def _extract_cashflow_ratio(
        self, cf_section: BeautifulSoup, pl_section: BeautifulSoup
    ) -> dict:
        """Compute CFO/Net Profit ratio using CFO from cash-flow and NP from P&L."""
        result: dict = {}
        cfo_vals: list[float] = []
        np_vals: list[float] = []

        # CFO from cash flow section
        for table in cf_section.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).lower()
                if "cash from operating" in label or "operating activities" in label:
                    for c in cells[1:]:
                        try:
                            cfo_vals.append(float(c.get_text(strip=True).replace(",", "")))
                        except ValueError:
                            pass

        # Net Profit from P&L main data-table
        main_pl = pl_section.find("table", class_="data-table")
        if main_pl:
            for row in main_pl.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).lower()
                if "net profit" in label:
                    for c in cells[1:]:
                        try:
                            np_vals.append(float(c.get_text(strip=True).replace(",", "")))
                        except ValueError:
                            pass

        if cfo_vals and np_vals:
            count = min(3, len(cfo_vals), len(np_vals))
            ratios = []
            for i in range(1, count + 1):
                cfo = cfo_vals[-i]
                np_ = np_vals[-i]
                if np_ and np_ != 0:
                    ratios.append((cfo / np_) * 100)
            if ratios:
                result["cfo_net_profit_3y_avg"] = round(sum(ratios) / len(ratios), 1)

        return result

    async def get_shareholding(self, ticker: str) -> Optional[GovernanceData]:
        """Scrape promoter holding from Screener.in (fallback source).

        Primary source is BSE; this is used as fallback.
        """
        ticker = ticker.upper().strip()
        url = f"/company/{ticker}/consolidated/"
        flags: list[str] = []

        try:
            resp = await self._fetch_with_rate_limit_handling(url)
        except Exception as exc:
            self.log.warning(
                "screener_shareholding_failed", ticker=ticker, error=str(exc), error_tag="ER-04"
            )
            return None

        if resp is None:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_shareholding(soup, flags)

    def _parse_shareholding(self, soup: BeautifulSoup, flags: list[str]) -> GovernanceData:
        """Parse promoter shareholding from Screener HTML."""
        promoter_holding: Optional[float] = None
        promoter_pledging: Optional[float] = None

        sh_section = soup.find("section", id="shareholding")
        if sh_section:
            for table in sh_section.find_all("table"):
                header_row = table.find("tr")
                if not header_row:
                    continue
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if not cells:
                        continue
                    label = cells[0].get_text(strip=True).lower()
                    if "promoter" in label and "pledg" not in label:
                        try:
                            # Take most recent quarter (last column)
                            promoter_holding = float(
                                cells[-1].get_text(strip=True).replace("%", "").strip()
                            )
                        except (ValueError, IndexError):
                            pass
                    elif "pledg" in label:
                        try:
                            promoter_pledging = float(
                                cells[-1].get_text(strip=True).replace("%", "").strip()
                            )
                        except (ValueError, IndexError):
                            pass

        if promoter_holding is None:
            flags.append("[DATA UNVERIFIED: promoter_holding]")
        if promoter_pledging is None:
            flags.append("[PLEDGING UNKNOWN]")

        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging,
            data_flags=flags,
        )
