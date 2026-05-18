"""Main investment analysis pipeline orchestrator."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import anthropic

from src.agent.mode_detector import detect_mode
from src.agent.steps import (
    Step0PreScreen,
    Step1Governance,
    Step2Moat,
    Step3Financials,
    Step4Tailwinds,
    Step5Valuation,
    Step6Technical,
    Step7Peers,
    Step8Premortem,
    Step9Output,
)
from src.api import BSEClient, NSEClient, ScreenerClient, TrendlyneClient, YFinanceClient
from src.api.cache import data_cache
from src.config import settings
from src.logging_config import get_logger
from src.models import AnalysisState, GovernanceData
from src.sector.classifier import classify_sector, is_conglomerate


async def _noop(value):
    """Return a cached value as an awaitable, skipping the real HTTP call."""
    return value


class InvestmentPipeline:
    """Orchestrates the 9-step investment analysis pipeline."""

    def __init__(self) -> None:
        self.claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.nse = NSEClient()
        self.screener = ScreenerClient()
        self.bse = BSEClient()
        self.trendlyne = TrendlyneClient()
        self.yfinance = YFinanceClient()
        self.log = get_logger("pipeline")

    async def analyze(self, ticker: str) -> AnalysisState:
        """Run the complete 9-step analysis pipeline for a ticker.

        Args:
            ticker: NSE ticker symbol (case-insensitive).

        Returns:
            Fully populated AnalysisState with recommendation.
        """
        ticker = ticker.upper().strip()
        state = AnalysisState(ticker=ticker)

        async with self.nse, self.screener, self.bse, self.trendlyne, self.yfinance:
            clients = {
                "nse": self.nse,
                "screener": self.screener,
                "bse": self.bse,
                "trendlyne": self.trendlyne,
                "yfinance": self.yfinance,
            }

            self.log.info("pipeline_start", ticker=ticker)

            # --- Mode detection ---
            state.mode = await detect_mode(self.nse, state)
            self.log.info(
                "mode_detected",
                mode=state.mode.value,
                nifty_decline_pct=state.nifty_decline_pct,
            )

            # --- Data prefetch ---
            await self._prefetch_data(state, clients)

            # --- Step instantiation ---
            steps = [
                Step0PreScreen(self.claude, clients),
                Step1Governance(self.claude, clients),
                Step2Moat(self.claude, clients),
                Step3Financials(self.claude, clients),
                Step4Tailwinds(self.claude, clients),
                Step5Valuation(self.claude, clients),
                Step6Technical(self.claude, clients),
                Step7Peers(self.claude, clients),
                Step8Premortem(self.claude, clients),
                Step9Output(self.claude, clients),
            ]

            # --- Sequential step execution ---
            terminated_logged = False
            for step in steps:
                if state.is_terminated:
                    # Run Step9 (output) even on terminated state so we get a report
                    if not isinstance(step, Step9Output):
                        if not terminated_logged:
                            self.log.info(
                                "pipeline_terminated",
                                at_step=state.terminated_at_step,
                                reason=state.termination_reason,
                            )
                            terminated_logged = True
                        continue

                step_start = time.monotonic()

                try:
                    state = await step.run(state)
                except Exception as exc:
                    self.log.error(
                        "step_error",
                        step=step.step_number,
                        step_name=step.step_name,
                        error=str(exc),
                        ticker=ticker,
                    )
                    state.add_error(f"STEP_{step.step_number}_ERROR")
                    # Continue pipeline — individual steps handle their own errors

                elapsed = time.monotonic() - step_start
                self.log.info(
                    "step_complete",
                    step=step.step_number,
                    step_name=step.step_name,
                    elapsed_seconds=round(elapsed, 2),
                    ticker=ticker,
                )

            self.log.info(
                "pipeline_complete",
                ticker=ticker,
                recommendation=state.recommendation_type,
                conviction=state.conviction.value if state.conviction else None,
            )

            # Persist result to SQLite (failures are non-fatal)
            try:
                from src.db.repository import save_analysis
                await save_analysis(settings.db_path, state)
            except Exception as db_exc:
                self.log.warning(
                    "db_save_failed", ticker=ticker, error=str(db_exc)
                )

            return state

    async def _prefetch_data(self, state: AnalysisState, clients: dict) -> None:
        """Fetch all raw data concurrently before step execution.

        Checks the in-process TTL cache first; only makes HTTP calls for stale/missing entries.
        On failure, stores None and adds the appropriate error tag.
        """
        ticker = state.ticker

        # Build coroutines only for cache misses
        cached_quote = data_cache.get(data_cache.quote_key(ticker))
        cached_financials = data_cache.get(data_cache.financials_key(ticker))
        cached_shareholding = data_cache.get(data_cache.shareholding_key(ticker))
        cached_valuation = data_cache.get(data_cache.valuation_key(ticker))

        coros = [
            clients["nse"].get_stock_quote(ticker) if cached_quote is None else _noop(cached_quote),
            clients["screener"].get_financials(ticker) if cached_financials is None else _noop(cached_financials),
            # Shareholding: NSE is primary — fallback chain runs below after gather
            clients["nse"].get_shareholding(ticker) if cached_shareholding is None else _noop(cached_shareholding),
            clients["trendlyne"].get_valuation_data(ticker) if cached_valuation is None else _noop(cached_valuation),
        ]

        results = await asyncio.gather(*coros, return_exceptions=True)

        # Populate cache for any newly fetched values
        if cached_quote is None and not isinstance(results[0], Exception):
            data_cache.set(data_cache.quote_key(ticker), results[0], settings.cache_ttl_quote)
        if cached_financials is None and not isinstance(results[1], Exception):
            data_cache.set(data_cache.financials_key(ticker), results[1], settings.cache_ttl_financials)
        if cached_shareholding is None and not isinstance(results[2], Exception) and results[2] is not None:
            data_cache.set(data_cache.shareholding_key(ticker), results[2], settings.cache_ttl_financials)
        if cached_valuation is None and not isinstance(results[3], Exception):
            data_cache.set(data_cache.valuation_key(ticker), results[3], settings.cache_ttl_quote)

        # Track which source was actually used — stored in DB snapshot metadata
        quote_source = "nse"
        valuation_source = "trendlyne"

        # Quote — fall back to Yahoo Finance if NSE is blocked
        quote_result = results[0]
        if isinstance(quote_result, Exception) or quote_result is None:
            if isinstance(quote_result, Exception):
                self.log.warning(
                    "prefetch_quote_failed",
                    ticker=ticker,
                    error=str(quote_result),
                    error_tag="ER-01",
                )
                state.add_error("ER-01")
            self.log.info("prefetch_quote_yfinance_fallback", ticker=ticker)
            quote_result = await clients["yfinance"].get_stock_quote(ticker)
            if quote_result is not None:
                quote_source = "yfinance"
                data_cache.set(data_cache.quote_key(ticker), quote_result, settings.cache_ttl_quote)
                state.add_flag("[stock_quote via Yahoo Finance — ~15 min delayed]")
            else:
                state.add_flag("[DATA UNVERIFIED: stock_quote]")
        if quote_result is not None:
            state.quote = quote_result
            state.company_name = quote_result.company_name

        # Financials
        financials_result = results[1]
        if isinstance(financials_result, Exception):
            self.log.warning(
                "prefetch_financials_failed",
                ticker=ticker,
                error=str(financials_result),
                error_tag="ER-02",
            )
            state.add_error("ER-02")
            state.add_flag("[DATA UNVERIFIED: financials]")
        else:
            state.financials = financials_result

        # Shareholding / Governance — 3-layer fallback: NSE → BSE → Screener
        shareholding_result = results[2]
        if isinstance(shareholding_result, Exception) or shareholding_result is None:
            if isinstance(shareholding_result, Exception):
                self.log.warning(
                    "nse_shareholding_failed",
                    ticker=ticker,
                    error=str(shareholding_result),
                    fallback="bse",
                )
            # Layer 2: BSE
            self.log.info("shareholding_fallback_bse", ticker=ticker)
            shareholding_result = await clients["bse"].get_shareholding(ticker)

        if shareholding_result is None:
            # Layer 3: Screener.in (same page as financials — cheap re-fetch, usually cached)
            self.log.info("shareholding_fallback_screener", ticker=ticker)
            shareholding_result = await clients["screener"].get_shareholding(ticker)

        if shareholding_result is None:
            self.log.warning(
                "prefetch_shareholding_all_sources_failed", ticker=ticker, error_tag="ER-04"
            )
            state.add_error("ER-04")
            state.add_flag("[PLEDGING UNKNOWN — all shareholding sources failed]")
            state.governance_data = GovernanceData(
                data_flags=["[DATA UNVERIFIED: shareholding — NSE/BSE/Screener all failed]"]
            )
        else:
            data_cache.set(data_cache.shareholding_key(ticker), shareholding_result, settings.cache_ttl_financials)
            state.governance_data = shareholding_result

        # Governance enrichment: fetch auditor + RPT from Trendlyne if not already populated
        # This runs after primary shareholding so we can merge into existing GovernanceData
        await self._enrich_governance_from_trendlyne(ticker, state, clients)

        # Valuation — fall back to Yahoo Finance if Trendlyne is blocked
        valuation_result = results[3]
        if isinstance(valuation_result, Exception) or valuation_result is None:
            if isinstance(valuation_result, Exception):
                self.log.warning(
                    "prefetch_valuation_failed",
                    ticker=ticker,
                    error=str(valuation_result),
                    error_tag="ER-03",
                )
                state.add_error("ER-03")
            self.log.info("prefetch_valuation_yfinance_fallback", ticker=ticker)
            valuation_result = await clients["yfinance"].get_valuation_data(ticker)
            if valuation_result is not None:
                valuation_source = "yfinance"
                data_cache.set(
                    data_cache.valuation_key(ticker), valuation_result, settings.cache_ttl_quote
                )
                state.add_flag("[valuation_data via Yahoo Finance — PE/PB only, no historical percentiles]")
            else:
                state.add_flag("[DATA UNVERIFIED: valuation_data]")
        if valuation_result is not None:
            state.valuation_data = valuation_result

        # Classify sector early so Step 0 (and all subsequent steps) can use it
        state.sector_name = classify_sector(
            company_name=state.company_name or "",
            ticker=ticker,
        )
        self.log.info(
            "sector_classified",
            ticker=ticker,
            sector=state.sector_name,
        )

        # P3-3: Conglomerate detection — flag for EC-04 sum-of-parts note in Step 5
        state.is_conglomerate = is_conglomerate(
            company_name=state.company_name or "",
            ticker=ticker,
        )
        if state.is_conglomerate:
            state.add_flag(
                "[EC-04: CONGLOMERATE detected — standard DCF may undervalue; "
                "sum-of-parts (SOTP) valuation recommended]"
            )
            self.log.info("conglomerate_detected", ticker=ticker, company=state.company_name)

        self.log.info(
            "prefetch_complete",
            ticker=ticker,
            has_quote=state.quote is not None,
            has_financials=state.financials is not None,
            has_governance=state.governance_data is not None,
            has_valuation=state.valuation_data is not None,
        )

        # Persist raw data snapshots to SQLite (non-fatal)
        try:
            from src.db.repository import save_snapshot
            from datetime import date as _date
            today_str = _date.today().isoformat()
            if state.quote:
                await save_snapshot(
                    settings.db_path, ticker, today_str, "quote",
                    state.quote.model_dump(mode="json"), quote_source
                )
            if state.financials:
                await save_snapshot(
                    settings.db_path, ticker, today_str, "financials",
                    state.financials.model_dump(mode="json"), "screener"
                )
            if state.governance_data:
                # Governance is sourced from NSE → BSE → Screener fallback chain
                # ER-04 = all shareholding sources failed, so we fell back to empty GovernanceData
                gov_source = "screener" if "ER-04" in state.error_tags else "bse"
                await save_snapshot(
                    settings.db_path, ticker, today_str, "governance",
                    state.governance_data.model_dump(mode="json"), gov_source
                )
            if state.valuation_data:
                await save_snapshot(
                    settings.db_path, ticker, today_str, "valuation",
                    state.valuation_data.model_dump(mode="json"), valuation_source
                )
        except Exception as snap_exc:
            self.log.warning("db_snapshot_failed", ticker=ticker, error=str(snap_exc))

    async def _enrich_governance_from_trendlyne(
        self, ticker: str, state: AnalysisState, clients: dict
    ) -> None:
        """Merge Trendlyne governance fields (auditor, pledging) into existing GovernanceData.

        Only fetches Trendlyne when the current governance data is missing auditor_name or
        promoter_holding_pct — avoids an unnecessary extra HTTP call for well-populated data.
        """
        g = state.governance_data
        if g is None:
            return

        # Skip if we already have auditor AND holding — nothing to gain
        if g.auditor_name is not None and g.promoter_holding_pct is not None:
            return

        self.log.info("governance_trendlyne_enrichment_start", ticker=ticker)
        tl_gov = await clients["trendlyne"].get_governance_data(ticker)
        if tl_gov is None:
            return

        # Merge: only fill fields that are currently missing
        if g.auditor_name is None and tl_gov.auditor_name:
            g.auditor_name = tl_gov.auditor_name
            self.log.info(
                "governance_auditor_from_trendlyne", ticker=ticker, auditor=tl_gov.auditor_name
            )
        if g.promoter_holding_pct is None and tl_gov.promoter_holding_pct is not None:
            g.promoter_holding_pct = tl_gov.promoter_holding_pct
        if (
            g.promoter_pledging_pct is None or g.promoter_pledging_pct == 0.0
        ) and tl_gov.promoter_pledging_pct is not None:
            g.promoter_pledging_pct = tl_gov.promoter_pledging_pct

        # Remove redundant flags that are now resolved
        resolved = set()
        if g.auditor_name:
            resolved.add("auditor")
        if g.promoter_holding_pct is not None:
            resolved.add("promoter_holding")
        g.data_flags = [
            f for f in g.data_flags
            if not any(r in f.lower() for r in resolved)
        ]
        g.data_flags.extend(
            f for f in tl_gov.data_flags
            if f not in g.data_flags
        )
