"""Screener.in HTML scraper for financial metrics."""
from __future__ import annotations

import asyncio
import random
import re

import httpx
from bs4 import BeautifulSoup

from src.api.base import BaseHTTPClient
from src.models import FinancialMetrics, GovernanceData

# Domain-level concurrency cap — shared across all ScreenerClient instances in the process.
# Prevents thundering-herd 429s when batch scanner runs concurrent pre-screens.
_SCREENER_SEMAPHORE = asyncio.Semaphore(2)


class ScreenerClient(BaseHTTPClient):
    """Scraper for Screener.in consolidated financial data."""

    def __init__(self) -> None:
        super().__init__(base_url="https://www.screener.in")

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers["Referer"] = "https://www.screener.in"
        return headers

    async def get_financials(self, ticker: str) -> FinancialMetrics | None:
        """Scrape financial metrics from Screener.in consolidated page.

        Args:
            ticker: NSE/BSE ticker symbol.

        Returns:
            FinancialMetrics or None on failure.
        """
        ticker = ticker.upper().strip()
        url = f"/company/{ticker}/consolidated/"

        try:
            resp = await self._fetch_with_rate_limit_handling(url)
        except Exception as exc:
            self.log.warning("screener_fetch_failed", ticker=ticker, error=str(exc), error_tag="ER-02")
            return None

        if resp is None:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_financials(soup, ticker)

    async def _fetch_with_rate_limit_handling(self, url: str) -> httpx.Response | None:
        """Fetch with exponential back-off + jitter on Screener.in 429 responses.

        Back-off schedule (base seconds, ±20 % jitter):
            Attempt 1 → immediate
            Attempt 2 → ~10 s  (was flat 90 s)
            Attempt 3 → ~30 s
            Attempt 4 → ~90 s  (original wait, now only on 3rd retry)

        This recovers 3–8× faster on brief rate-limit windows while still
        honouring Screener's throttle on sustained hammering.
        """
        backoff_base = [10, 30, 90]  # seconds before each retry

        async with _SCREENER_SEMAPHORE:
            for attempt, base_wait in enumerate(backoff_base):
                try:
                    return await self.get(url)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 429:
                        raise
                    jitter = random.uniform(-0.2 * base_wait, 0.2 * base_wait)
                    wait = round(base_wait + jitter, 1)
                    self.log.warning(
                        "screener_rate_limited",
                        wait_seconds=wait,
                        attempt=attempt + 1,
                        max_attempts=len(backoff_base) + 1,
                    )
                    await asyncio.sleep(wait)

            # Final attempt after all back-off steps exhausted
            return await self.get(url)

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
            metrics.update(self._extract_pl_values(pl_section))   # P1-3 / P1-4
        if ratios_section:
            metrics.update(self._extract_ratios(ratios_section))
        if balance_section:
            metrics.update(self._extract_balance_sheet(balance_section))
        if cashflow_section and pl_section:
            metrics.update(self._extract_cashflow_ratio(cashflow_section, pl_section))

        # P1-3: Working capital — cross-compute debtor/inventory days from
        # internal keys populated by _extract_pl_values + _extract_balance_sheet
        sales_vals: list[float] = metrics.pop("_sales_vals", [])
        receivables_vals: list[float] = metrics.pop("_receivables_vals", [])
        inventory_vals: list[float] = metrics.pop("_inventory_vals", [])

        if sales_vals and receivables_vals and sales_vals[-1] > 0:
            metrics["debtor_days_latest"] = round(
                receivables_vals[-1] / sales_vals[-1] * 365, 1
            )
            if len(sales_vals) >= 3 and len(receivables_vals) >= 3 and sales_vals[-3] > 0:
                metrics["debtor_days_3y_ago"] = round(
                    receivables_vals[-3] / sales_vals[-3] * 365, 1
                )

        if sales_vals and inventory_vals and sales_vals[-1] > 0:
            metrics["inventory_days_latest"] = round(
                inventory_vals[-1] / sales_vals[-1] * 365, 1
            )

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
            # P1-3: Working capital
            debtor_days_latest=metrics.get("debtor_days_latest"),
            debtor_days_3y_ago=metrics.get("debtor_days_3y_ago"),
            inventory_days_latest=metrics.get("inventory_days_latest"),
            # P1-4: Earnings quality
            other_income_pct_revenue=metrics.get("other_income_pct_revenue"),
            # P1-1: Bank/NBFC sector KPIs
            gnpa_pct=metrics.get("gnpa_pct"),
            nnpa_pct=metrics.get("nnpa_pct"),
            nim_pct=metrics.get("nim_pct"),
            roa_pct=metrics.get("roa_pct"),
            car_pct=metrics.get("car_pct"),
            # P2-3: Trend directions
            roce_trend=metrics.get("roce_trend"),
            roe_trend=metrics.get("roe_trend"),
            ebitda_margin_trend=metrics.get("ebitda_margin_trend"),
            # P2-4: Cyclical normalization
            ebitda_margin_5y_avg=metrics.get("ebitda_margin_5y_avg"),
            # Revenue (absolute) — used for P/S ratio on pre-profit companies
            trailing_revenue_cr=metrics.get("trailing_revenue_cr"),
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

    def _extract_pl_values(self, section: BeautifulSoup) -> dict:
        """Extract raw P&L values needed for working-capital and earnings-quality metrics.

        Populates (all as internal keys consumed by _parse_financials):
          ``_sales_vals``         — list of annual Sales figures (oldest → newest)
          ``other_income_pct_revenue`` — Other Income as % of latest-year revenue

        Screener puts raw annual figures in the ``data-table responsive-text-nowrap``
        table inside the profit-loss section.  Rows of interest:
          • Sales+  /  Revenue from Operations
          • Other Income+
        """
        result: dict = {}
        sales_vals: list[float] = []
        other_income_vals: list[float] = []

        main_pl = section.find("table", class_=re.compile(r"data-table"))
        if not main_pl:
            return result

        for row in main_pl.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            label = cells[0].get_text(strip=True).lower().replace("+", "").strip()
            vals: list[float] = []
            for c in cells[1:]:
                try:
                    vals.append(float(c.get_text(strip=True).replace(",", "")))
                except ValueError:
                    pass
            if not vals:
                continue

            if label in ("sales", "revenue from operations", "net sales", "revenue"):
                sales_vals = vals
            elif "other income" in label:
                other_income_vals = vals

        if sales_vals:
            result["_sales_vals"] = sales_vals  # consumed later in _parse_financials
            # Expose latest annual revenue for P/S ratio computation
            result["trailing_revenue_cr"] = sales_vals[-1]

        # Other income as % of revenue (latest year only)
        if other_income_vals and sales_vals and sales_vals[-1] > 0:
            result["other_income_pct_revenue"] = round(
                abs(other_income_vals[-1]) / sales_vals[-1] * 100, 1
            )

        return result

    def _extract_ratios(self, section: BeautifulSoup) -> dict:
        """Extract ROE, ROCE, and other ratios from the Ratios section."""
        result: dict = {}
        tables = section.find_all("table", class_=re.compile(r"data-table"))
        for table in tables:
            header_row = table.find("tr")
            if not header_row:
                continue
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
                    # P2-3: trend — recent 2Y vs prior 3Y
                    if len(valid) >= 5:
                        recent = sum(valid[-2:]) / 2
                        prior = sum(valid[-5:-2]) / 3
                        diff = recent - prior
                        result["roe_trend"] = (
                            "improving" if diff > 2.5 else
                            "deteriorating" if diff < -2.5 else
                            "stable"
                        )

                elif "return on capital" in label or "roce" in label:
                    valid = [v for v in float_vals if v is not None]
                    if len(valid) >= 5:
                        result["roce_5y_avg"] = sum(valid[-5:]) / 5
                    elif valid:
                        result["roce_5y_avg"] = sum(valid) / len(valid)
                    # P2-3: ROCE trend
                    if len(valid) >= 5:
                        recent = sum(valid[-2:]) / 2
                        prior = sum(valid[-5:-2]) / 3
                        diff = recent - prior
                        result["roce_trend"] = (
                            "improving" if diff > 2.5 else
                            "deteriorating" if diff < -2.5 else
                            "stable"
                        )

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
                    # P2-3: EBITDA margin trend; P2-4: 5Y avg for cyclical normalization
                    valid = [v for v in float_vals if v is not None]
                    if len(valid) >= 5:
                        result["ebitda_margin_5y_avg"] = round(sum(valid[-5:]) / 5, 1)
                        recent = sum(valid[-2:]) / 2
                        prior = sum(valid[-5:-2]) / 3
                        diff = recent - prior
                        result["ebitda_margin_trend"] = (
                            "expanding" if diff > 1.5 else
                            "compressing" if diff < -1.5 else
                            "stable"
                        )
                    elif valid:
                        result["ebitda_margin_5y_avg"] = round(sum(valid) / len(valid), 1)

                # ── P1-1: Bank / NBFC sector KPIs ────────────────────────
                elif "net interest margin" in label or label.strip() in ("nim", "nim %"):
                    if float_vals and float_vals[-1] is not None:
                        result["nim_pct"] = float_vals[-1]

                elif "gross npa" in label or label.strip() in ("gnpa %", "gnpa"):
                    if float_vals and float_vals[-1] is not None:
                        result["gnpa_pct"] = float_vals[-1]

                elif "net npa" in label or label.strip() in ("nnpa %", "nnpa"):
                    if float_vals and float_vals[-1] is not None:
                        result["nnpa_pct"] = float_vals[-1]

                elif "return on asset" in label or label.strip() in ("roa", "roa %"):
                    if float_vals and float_vals[-1] is not None:
                        result["roa_pct"] = float_vals[-1]

                elif "capital adequacy" in label or label.strip() in ("car", "car %", "crar %"):
                    if float_vals and float_vals[-1] is not None:
                        result["car_pct"] = float_vals[-1]

        return result

    def _extract_balance_sheet(self, section: BeautifulSoup) -> dict:
        """Extract D/E ratio, trade receivables, and inventory from balance sheet.

        D/E  = Borrowings / (Equity Capital + Reserves)
        Also stores ``_receivables_vals`` and ``_inventory_vals`` as internal
        keys (lists of annual values, oldest→newest) so _parse_financials can
        compute debtor days and inventory days.
        """
        result: dict = {}
        borrowings: float | None = None
        equity_capital: float | None = None
        reserves: float | None = None
        receivables_vals: list[float] = []
        inventory_vals: list[float] = []

        main_table = section.find("table", class_="data-table")
        if main_table:
            for row in main_table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).lower().replace("+", "").strip()
                vals: list[float] = []
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
                # P1-3: Trade receivables (various Screener label forms)
                elif label in (
                    "trade receivables", "debtors", "sundry debtors",
                    "receivables", "trade and other receivables",
                ):
                    receivables_vals = vals
                # P1-3: Inventory
                elif label in ("inventories", "inventory", "stock"):
                    inventory_vals = vals

        if borrowings is not None and equity_capital is not None and reserves is not None:
            total_equity = equity_capital + reserves
            if total_equity > 0:
                result["debt_to_equity"] = round(borrowings / total_equity, 2)

        if receivables_vals:
            result["_receivables_vals"] = receivables_vals  # consumed by _parse_financials
        if inventory_vals:
            result["_inventory_vals"] = inventory_vals

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

    async def get_shareholding(self, ticker: str) -> GovernanceData | None:
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
        promoter_holding: float | None = None
        promoter_pledging: float | None = None

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
