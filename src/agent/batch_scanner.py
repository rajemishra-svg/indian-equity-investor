"""Batch scanner — screens a stock universe and surfaces the best opportunities.

Two-phase process:
  Phase 1 — Cheap pre-screen (Step 0, deterministic, no Claude calls).
             Runs concurrently across the full universe with an HTTP semaphore.
  Phase 2 — Full 9-step pipeline on the shortlisted candidates only.
             Runs with bounded concurrency (3 parallel pipelines) to balance
             speed against Screener / Claude API rate limits.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from src.agent.pipeline import InvestmentPipeline
from src.agent.steps.step0_prescreen import Step0PreScreen
from src.api import BSEClient, NSEClient, ScreenerClient, YFinanceClient
from src.api.cache import data_cache
from src.config import settings
from src.db.repository import get_fresh_snapshot, save_snapshot
from src.logging_config import get_logger
from src.models import AnalysisState, FinancialMetrics, GateResult, GovernanceData

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
# Nifty 50 fallback — loaded from config/nifty50_fallback.json.
# Last resort when both NSE API and archives fail.
# Update config/nifty50_fallback.json when index composition changes.
# ---------------------------------------------------------------------------

_FALLBACK_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "nifty50_fallback.json"


def _load_nifty50_fallback() -> list[str]:
    """Load Nifty 50 fallback list from config file with staleness warning."""
    try:
        with open(_FALLBACK_CONFIG_PATH) as f:
            data = json.load(f)
        symbols: list[str] = data.get("symbols", [])
        last_updated_str: str = data.get("last_updated", "")
        warning_days: int = data.get("staleness_warning_days", 90)
        if last_updated_str:
            try:
                last_updated = date.fromisoformat(last_updated_str)
                age_days = (date.today() - last_updated).days
                if age_days > warning_days:
                    log.warning(
                        "nifty50_fallback_stale",
                        last_updated=last_updated_str,
                        age_days=age_days,
                        warning_days=warning_days,
                        msg="Update config/nifty50_fallback.json — index composition may have changed",
                    )
            except ValueError:
                pass
        return symbols
    except Exception as exc:
        log.warning("nifty50_fallback_load_failed", error=str(exc))
        return []


NIFTY50_FALLBACK: list[str] = _load_nifty50_fallback()


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
    error: str | None = None
    # Tie-breakers for the Phase 3 candidate cut — captured from data already
    # fetched in Phase 2, so they cost nothing extra.
    roce_5y: float | None = None             # capital efficiency (higher better)
    cfo_np_3y: float | None = None           # earnings quality (higher better)
    pct_below_52w_high: float | None = None  # entry attractiveness (higher better)


def candidate_sort_key(s: PreScreenSummary) -> tuple:
    """Deterministic ordering for the Phase 3 candidate cut.

    Step 0 scores are 0–9 integers, so dozens of tickers tie at 8–9 and a
    score-only sort would cut the expensive full-analysis list at arbitrary
    universe order.  Ties are broken by quality and entry attractiveness,
    using data Phase 2 already fetched:

      1. score (desc)
      2. ROCE 5Y avg (desc) — capital efficiency
      3. CFO/NP 3Y (desc) — earnings quality
      4. % below 52W high (desc) — better entry odds among already-vetted names
      5. ticker (asc) — total determinism

    Missing metrics sort after present ones at the same level.
    """

    def _desc(value: float | None) -> float:
        return -value if value is not None else float("inf")

    return (
        -s.score,
        _desc(s.roce_5y),
        _desc(s.cfo_np_3y),
        _desc(s.pct_below_52w_high),
        s.ticker,
    )


# ---------------------------------------------------------------------------
# BatchScanner
# ---------------------------------------------------------------------------


class BatchScanner:
    """Screen a stock universe and run full analysis on the best candidates."""

    def __init__(self, concurrency: int | None = None) -> None:
        """
        Args:
            concurrency: Max parallel HTTP requests during the pre-screen phase.
                Defaults to ``settings.scan_concurrency`` (8).

                Tuning guide:
                  - Cold scan (no DB cache): keep ≤ 5 to avoid Screener 429s.
                  - Warm scan (DB cache seeded): safe to use 10–15; most tickers
                    skip Screener entirely so the rate-limit risk is much lower.
                  - Override via ``--concurrency`` CLI flag or
                    ``SCAN_CONCURRENCY=N`` env var.
        """
        self._concurrency = concurrency if concurrency is not None else settings.scan_concurrency

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
        for ticker, result in zip(tickers, raw, strict=True):
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

        # Sort with quality tie-breakers (ROCE → CFO/NP → % below 52W high) so
        # the [:max_full_analyses] cut below picks the genuinely best names
        # among tied Step 0 scores instead of arbitrary universe order.
        candidates = sorted(
            [s for s in summaries if s.score >= prescreen_min_score and not s.error],
            key=candidate_sort_key,
        )
        log.info(
            "prescreen_complete",
            total=len(tickers),
            passed=len(candidates),
            proceeding=min(len(candidates), max_full_analyses),
        )

        if prescreen_only or not candidates:
            return summaries, []

        # Phase 3: full pipeline — bounded concurrency (3 parallel analyses).
        # One AsyncAnthropic client is shared by every pipeline (stateless,
        # concurrency-safe — avoids a connection pool per candidate); HTTP
        # clients stay per-pipeline because each analyze() owns their async
        # context-manager lifecycle. Screener's module-level semaphore caps
        # domain-level request rate independently.
        import anthropic

        shared_claude = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        pipeline_concurrency = 3
        pipeline_sem = asyncio.Semaphore(pipeline_concurrency)

        async def _run_one(summary: PreScreenSummary) -> AnalysisState | None:
            async with pipeline_sem:
                log.info(
                    "full_analysis_start",
                    ticker=summary.ticker,
                    prescreen_score=summary.score,
                )
                try:
                    pipeline = InvestmentPipeline(claude=shared_claude)
                    return await pipeline.analyze(summary.ticker)
                except Exception as exc:
                    log.warning("full_analysis_failed", ticker=summary.ticker, error=str(exc))
                    return None

        tasks = [_run_one(s) for s in candidates[:max_full_analyses]]
        raw_results = await asyncio.gather(*tasks)
        full_results: list[AnalysisState] = [r for r in raw_results if r is not None]

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
            f = state.financials
            q = state.quote
            pct_below_high: float | None = None
            if q and q.w52_high and q.w52_high > 0:
                pct_below_high = round((q.w52_high - q.cmp) / q.w52_high * 100, 2)
            return PreScreenSummary(
                ticker=ticker,
                score=pre.score if pre else 0,
                gate=pre.gate if pre else GateResult.NOT_RUN,
                failed_metrics=pre.failed_metrics if pre else [],
                data_flags=state.all_data_flags,
                roce_5y=f.roce_5y_avg if f else None,
                cfo_np_3y=f.cfo_net_profit_3y_avg if f else None,
                pct_below_52w_high=pct_below_high,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_prescreen_data(state: AnalysisState, clients: dict) -> None:
    """Fetch quote + financials + shareholding for Step 0 (skips trendlyne).

    Three-layer cache for financials and shareholding:
      1. In-memory DataCache (process-lifetime, TTL 24 h) — fastest
      2. SQLite warm cache (cross-run, TTL configured via settings) — avoids
         Screener requests on repeated scans within the same week
      3. Live HTTP fetch (Screener / BSE / YFinance) — writes to both caches
    """
    ticker = state.ticker
    today = __import__("datetime").date.today().isoformat()

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
        # 1. In-memory hit
        if cached_f is not None:
            return cached_f

        # 2. SQLite warm-cache hit (cross-run)
        raw = await _db_get_snapshot("financials", settings.cache_ttl_db_financials_hours)
        if raw is not None:
            try:
                result = FinancialMetrics.model_validate(raw)
                data_cache.set(data_cache.financials_key(ticker), result, settings.cache_ttl_financials)
                log.debug("prescreen_financials_db_cache_hit", ticker=ticker)
                return result
            except Exception:
                pass  # corrupt snapshot — fall through to live fetch

        # 3. Live fetch
        screener = clients.get("screener")
        if screener is None:
            return None
        result = await screener.get_financials(ticker)
        if result is not None:
            data_cache.set(data_cache.financials_key(ticker), result, settings.cache_ttl_financials)
            await _db_save_snapshot("financials", result.model_dump(), "screener")
        return result

    async def _shareholding() -> object:
        # 1. In-memory hit
        if cached_s is not None:
            return cached_s

        # 2. SQLite warm-cache hit (cross-run)
        raw = await _db_get_snapshot("governance", settings.cache_ttl_db_governance_hours)
        if raw is not None:
            try:
                result = GovernanceData.model_validate(raw)
                data_cache.set(data_cache.shareholding_key(ticker), result, settings.cache_ttl_financials)
                log.debug("prescreen_governance_db_cache_hit", ticker=ticker)
                return result
            except Exception:
                pass  # corrupt snapshot — fall through to live fetch

        # 3. Live fetch
        bse = clients.get("bse")
        result = await bse.get_shareholding(ticker) if bse is not None else None
        if result is None:
            # BSE API commonly returns empty in non-browser environments; fall back to Screener
            screener = clients.get("screener")
            if screener is not None:
                result = await screener.get_shareholding(ticker)
        if result is not None:
            data_cache.set(data_cache.shareholding_key(ticker), result, settings.cache_ttl_financials)
            await _db_save_snapshot("governance", result.model_dump(), "bse_or_screener")
        return result

    # --- SQLite helpers (close over ticker / today / db_path) ---------------

    async def _db_get_snapshot(data_type: str, max_age_hours: int) -> object:
        try:
            return await get_fresh_snapshot(
                settings.db_path, ticker, data_type, max_age_hours
            )
        except Exception as exc:
            log.debug("db_snapshot_read_error", ticker=ticker, data_type=data_type, error=str(exc))
            return None

    async def _db_save_snapshot(data_type: str, data: dict, source: str) -> None:
        try:
            await save_snapshot(settings.db_path, ticker, today, data_type, data, source)
        except Exception as exc:
            log.debug("db_snapshot_write_error", ticker=ticker, data_type=data_type, error=str(exc))

    # -------------------------------------------------------------------------

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
