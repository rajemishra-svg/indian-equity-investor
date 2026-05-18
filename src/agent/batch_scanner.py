"""Batch scanner — screens a stock universe and surfaces the best opportunities.

Two-phase process:
  Phase 1 — Cheap pre-screen (Step 0, deterministic, no Claude calls).
             Runs concurrently across the full universe with an HTTP semaphore.
  Phase 2 — Full 9-step pipeline on the shortlisted candidates only.
             Run sequentially to avoid overwhelming Screener / Claude API.
"""
from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.agent.pipeline import InvestmentPipeline
from src.agent.steps.step0_prescreen import Step0PreScreen
from src.api import BSEClient, NSEClient, ScreenerClient, YFinanceClient
from src.api.cache import data_cache
from src.config import settings
from src.logging_config import get_logger
from src.models import AnalysisState, GateResult, GovernanceData

log = get_logger("batch_scanner")

# ---------------------------------------------------------------------------
# NSE archives CSV — fetches live index constituents without browser cookies.
# Used as first fallback when the NSE API (JSON) returns 403.
# ---------------------------------------------------------------------------

_NSE_ARCHIVES_CSV: dict[str, str] = {
    "NIFTY 50":      "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY 100":     "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY 200":     "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY 500":     "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTY NEXT 50": "https://archives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY MIDCAP 100": "https://archives.nseindia.com/content/indices/ind_niftymidcap100list.csv",
    "NIFTY SMALLCAP 100": "https://archives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
}

_NSE_ARCHIVES_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}


async def _fetch_constituents_from_archives(index: str) -> list[str]:
    """Download the NSE archives CSV for *index* and return symbol list."""
    url = _NSE_ARCHIVES_CSV.get(index.upper().strip())
    if not url:
        raise ValueError(f"No archives URL mapped for index '{index}'")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_NSE_ARCHIVES_HEADERS, follow_redirects=True)
        resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    symbols = [row.get("Symbol", "").strip() for row in reader]
    return [s for s in symbols if s]


# ---------------------------------------------------------------------------
# Hard-coded Nifty 50 fallback — last resort when both NSE API and archives fail
# ---------------------------------------------------------------------------

NIFTY50_FALLBACK: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK",
    "INFOSYS", "SBIN", "HINDUNILVR", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "NTPC", "ONGC", "TECHM", "BAJFINANCE",
    "WIPRO", "HCLTECH", "ADANIENT", "ULTRACEMCO", "POWERGRID",
    "TATASTEEL", "JSWSTEEL", "TITAN", "BAJAJFINSV", "NESTLEIND",
    "DIVISLAB", "DRREDDY", "CIPLA", "EICHERMOT", "BPCL",
    "COALINDIA", "GRASIM", "HINDALCO", "M&M", "SBILIFE",
    "APOLLOHOSP", "ADANIPORTS", "BAJAJ-AUTO", "BRITANNIA", "HDFCLIFE",
    "INDUSINDBK", "LTIM", "TATACONSUM", "UPL", "VEDL",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PreScreenSummary:
    """Step 0 result for a single ticker."""

    ticker: str
    score: int
    gate: GateResult
    failed_metrics: list[str] = field(default_factory=list)
    data_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# BatchScanner
# ---------------------------------------------------------------------------


class BatchScanner:
    """Screen a stock universe and run full analysis on the best candidates."""

    def __init__(self, concurrency: int = 5) -> None:
        """
        Args:
            concurrency: Max parallel HTTP requests during the pre-screen phase.
                         Keep ≤ 5 to avoid triggering Screener.in rate limits.
        """
        self._concurrency = concurrency

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_universe(self, index: str = "NIFTY 500") -> list[str]:
        """Fetch index constituents.

        Tries in order:
        1. NSE JSON API (fast, may return 403 in non-browser envs)
        2. NSE archives CSV (no auth required, works in all envs)
        3. Hardcoded Nifty 50 list (last resort)
        """
        async with NSEClient() as nse:
            try:
                tickers = await nse.get_index_constituents(index)
                log.info("universe_fetched", index=index, source="nse_api", count=len(tickers))
                return tickers
            except Exception as exc:
                log.warning(
                    "nse_api_universe_failed",
                    index=index,
                    error=str(exc),
                    fallback="nse_archives_csv",
                )

        try:
            tickers = await _fetch_constituents_from_archives(index)
            log.info("universe_fetched", index=index, source="nse_archives_csv", count=len(tickers))
            return tickers
        except Exception as exc:
            log.warning(
                "nse_archives_universe_failed",
                index=index,
                error=str(exc),
                fallback="nifty50_hardcoded",
            )
            return list(NIFTY50_FALLBACK)

    async def prescreen_universe(self, tickers: list[str]) -> list[PreScreenSummary]:
        """Run Step 0 on all tickers concurrently (rate-limited by semaphore).

        Trendlyne is skipped — Step 0 only needs quote, financials, shareholding.
        """
        semaphore = asyncio.Semaphore(self._concurrency)

        async with NSEClient() as nse, ScreenerClient() as screener, BSEClient() as bse, YFinanceClient() as yfinance:
            clients = {"nse": nse, "screener": screener, "bse": bse, "trendlyne": None, "yfinance": yfinance}
            tasks = [
                self._prescreen_one(ticker, clients, semaphore) for ticker in tickers
            ]
            raw = await asyncio.gather(*tasks, return_exceptions=True)

        summaries: list[PreScreenSummary] = []
        for ticker, result in zip(tickers, raw):
            if isinstance(result, Exception):
                log.warning("prescreen_error", ticker=ticker, error=str(result))
                summaries.append(
                    PreScreenSummary(
                        ticker=ticker,
                        score=0,
                        gate=GateResult.NOT_RUN,
                        error=str(result),
                    )
                )
            else:
                summaries.append(result)
        return summaries

    async def scan(
        self,
        index: str = "NIFTY 500",
        prescreen_min_score: int = 5,
        max_full_analyses: int = 20,
        prescreen_only: bool = False,
    ) -> tuple[list[PreScreenSummary], list[AnalysisState]]:
        """Run the full two-phase scan.

        Args:
            index: NSE index name to scan.
            prescreen_min_score: Minimum Step 0 score (0–9) to proceed to full analysis.
            max_full_analyses: Cap on full pipeline runs (Claude API cost control).
            prescreen_only: If True, skip Phase 2 and return after pre-screening.

        Returns:
            (all_prescreen_summaries, ranked_full_analysis_results)
        """
        # Phase 1: universe
        log.info("universe_fetch_start", index=index)
        tickers = await self.get_universe(index)
        log.info("universe_ready", index=index, count=len(tickers))

        # Phase 2: pre-screen
        log.info("prescreen_start", ticker_count=len(tickers), min_score=prescreen_min_score)
        summaries = await self.prescreen_universe(tickers)

        candidates = sorted(
            [s for s in summaries if s.score >= prescreen_min_score and not s.error],
            key=lambda s: s.score,
            reverse=True,
        )
        log.info(
            "prescreen_complete",
            total=len(tickers),
            passed=len(candidates),
            proceeding=min(len(candidates), max_full_analyses),
        )

        if prescreen_only or not candidates:
            return summaries, []

        # Phase 3: full pipeline — sequential to respect Screener + Claude rate limits
        pipeline = InvestmentPipeline()
        full_results: list[AnalysisState] = []

        for summary in candidates[:max_full_analyses]:
            log.info(
                "full_analysis_start",
                ticker=summary.ticker,
                prescreen_score=summary.score,
            )
            try:
                state = await pipeline.analyze(summary.ticker)
                full_results.append(state)
            except Exception as exc:
                log.warning("full_analysis_failed", ticker=summary.ticker, error=str(exc))

        # Phase 4: rank
        ranked = rank_results(full_results)
        log.info(
            "scan_complete",
            full_analyses=len(full_results),
            buy_count=sum(1 for s in ranked if s.recommendation_type == "BUY"),
            watchlist_count=sum(1 for s in ranked if s.recommendation_type == "WATCHLIST"),
        )
        return summaries, ranked

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _prescreen_one(
        self,
        ticker: str,
        clients: dict,
        semaphore: asyncio.Semaphore,
    ) -> PreScreenSummary:
        """Fetch data and run Step 0 for one ticker under the semaphore."""
        async with semaphore:
            state = AnalysisState(ticker=ticker)
            await _fetch_prescreen_data(state, clients)

            # Step0PreScreen is purely deterministic — pass None for claude client
            step0 = Step0PreScreen(None, clients)  # type: ignore[arg-type]
            state = await step0.run(state)

            pre = state.pre_screen
            return PreScreenSummary(
                ticker=ticker,
                score=pre.score if pre else 0,
                gate=pre.gate if pre else GateResult.NOT_RUN,
                failed_metrics=pre.failed_metrics if pre else [],
                data_flags=state.all_data_flags,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_prescreen_data(state: AnalysisState, clients: dict) -> None:
    """Fetch quote + financials + shareholding for Step 0 (skips trendlyne)."""
    ticker = state.ticker

    cached_q = data_cache.get(data_cache.quote_key(ticker))
    cached_f = data_cache.get(data_cache.financials_key(ticker))
    cached_s = data_cache.get(data_cache.shareholding_key(ticker))

    async def _quote() -> object:
        if cached_q is not None:
            return cached_q
        nse = clients.get("nse")
        result = await nse.get_stock_quote(ticker) if nse is not None else None
        if result is None:
            # NSE commonly returns 403 in non-browser environments; fall back to Yahoo Finance
            yf_client = clients.get("yfinance")
            if yf_client is not None:
                result = await yf_client.get_stock_quote(ticker)
        if result is not None:
            data_cache.set(data_cache.quote_key(ticker), result, settings.cache_ttl_quote)
        return result

    async def _financials() -> object:
        if cached_f is not None:
            return cached_f
        screener = clients.get("screener")
        if screener is None:
            return None
        result = await screener.get_financials(ticker)
        data_cache.set(data_cache.financials_key(ticker), result, settings.cache_ttl_financials)
        return result

    async def _shareholding() -> object:
        if cached_s is not None:
            return cached_s
        bse = clients.get("bse")
        result = await bse.get_shareholding(ticker) if bse is not None else None
        if result is None:
            # BSE API commonly returns empty in non-browser environments; fall back to Screener
            screener = clients.get("screener")
            if screener is not None:
                result = await screener.get_shareholding(ticker)
        if result is not None:
            data_cache.set(data_cache.shareholding_key(ticker), result, settings.cache_ttl_financials)
        return result

    quote, financials, shareholding = await asyncio.gather(
        _quote(), _financials(), _shareholding(), return_exceptions=True
    )

    if not isinstance(quote, Exception) and quote is not None:
        state.quote = quote  # type: ignore[assignment]
        state.company_name = quote.company_name  # type: ignore[union-attr]
    if not isinstance(financials, Exception) and financials is not None:
        state.financials = financials  # type: ignore[assignment]
    if not isinstance(shareholding, Exception) and shareholding is not None:
        state.governance_data = shareholding  # type: ignore[assignment]
    else:
        state.governance_data = GovernanceData(data_flags=["[DATA UNVERIFIED: shareholding]"])


def rank_results(results: list[AnalysisState]) -> list[AnalysisState]:
    """Sort full-pipeline results: BUY first, then by conviction, MoS, governance."""

    def _key(s: AnalysisState) -> tuple:
        rtype = {"BUY": 0, "WATCHLIST": 1, "PEER_SWITCH": 2, "REJECT": 3}.get(
            s.recommendation_type or "REJECT", 3
        )
        conviction = {"high": 0, "medium": 1, "low": 2}.get(
            s.conviction.value if s.conviction else "", 2
        )
        mos = -(s.valuation.margin_of_safety_pct or 0.0) if s.valuation else 0.0
        gov = -(s.governance.score if s.governance else 0)
        return (rtype, conviction, mos, gov)

    return sorted(results, key=_key)
