"""Growth analysis pipeline — identifies high-growth multibagger candidates.

Mirrors InvestmentPipeline but routes to growth-specific steps and computes
GrowthMetrics from prefetched data.  All data fetching and caching logic is
inherited unchanged; only the step sequence and growth metric computation differ.
"""
from __future__ import annotations

import time

import anthropic

from src.agent.mode_detector import detect_mode
from src.agent.pipeline import InvestmentPipeline
from src.agent.steps import (
    Step0GrowthPreScreen,
    Step1Governance,
    Step2Moat,
    Step3GrowthFinancials,
    Step4Tailwinds,
    Step5GrowthValuation,
    Step5MMultibagger,
    Step6Technical,
    Step7Peers,
    Step8Premortem,
    Step9Output,
)
from src.config import settings
from src.logging_config import get_logger
from src.models import AnalysisMode, AnalysisState, GrowthMetrics
from src.sector.classifier import classify_sector_with_confidence

_SECTOR_RECLASS_THRESHOLD = 0.7

log = get_logger("growth_pipeline")


def compute_growth_metrics(state: AnalysisState) -> None:
    """Derive GrowthMetrics from already-prefetched AnalysisState data.

    All inputs come from state.financials, state.valuation_data, and state.quote.
    No additional HTTP calls.  Safe to call with partial data (valuation_data may
    be None during batch pre-screening).
    """
    f = state.financials
    v = state.valuation_data
    q = state.quote

    gm = GrowthMetrics()

    if f:
        gm.gross_margin_pct = f.gross_profit_margin_pct

        # Gross margin trend: compare latest 2 years vs prior 2 years
        series = f.gross_profit_margin_series
        if len(series) >= 4:
            recent = sum(series[-2:]) / 2
            prior = sum(series[-4:-2]) / 2
            diff = recent - prior
            gm.gross_margin_trend = (
                "expanding" if diff > 1.0 else
                "contracting" if diff < -1.0 else
                "stable"
            )
        elif len(series) >= 2:
            diff = series[-1] - series[-2]
            gm.gross_margin_trend = (
                "expanding" if diff > 1.0 else
                "contracting" if diff < -1.0 else
                "stable"
            )

        # 1Y revenue CAGR from consecutive annual figures
        if f.revenue_1y_ago_cr and f.trailing_revenue_cr and f.revenue_1y_ago_cr > 0:
            gm.revenue_cagr_1y = round(
                (f.trailing_revenue_cr / f.revenue_1y_ago_cr - 1) * 100, 1
            )

        # Rule of 40: revenue CAGR 3Y + EBITDA margin
        if f.revenue_cagr_3y is not None and f.ebitda_margin_latest is not None:
            gm.rule_of_40_score = round(f.revenue_cagr_3y + f.ebitda_margin_latest, 1)

        # ROIIC proxy: CFO/NP fraction × PAT CAGR
        if f.cfo_net_profit_3y_avg and f.pat_cagr_3y:
            gm.roiic_proxy_cfo_revenue = round(
                (f.cfo_net_profit_3y_avg / 100) * f.pat_cagr_3y, 1
            )

        # Cash burn and runway
        if v and v.fcf_latest_cr is not None and v.fcf_latest_cr < 0:
            gm.burn_rate_cr_month = round(abs(v.fcf_latest_cr) / 12, 1)
        if f.cash_cr_latest is not None and gm.burn_rate_cr_month and gm.burn_rate_cr_month > 0:
            gm.cash_runway_months = round(f.cash_cr_latest / gm.burn_rate_cr_month, 1)

    # P/S and EV/Revenue ratios (prefer live quote market cap over Screener estimate)
    market_cap_cr = (q.market_cap_cr if q else None) or (f.market_cap_cr if f else None)
    revenue_cr = f.trailing_revenue_cr if f else None

    if market_cap_cr and revenue_cr and revenue_cr > 0:
        gm.ps_ratio = round(market_cap_cr / revenue_cr, 2)
        if v and v.net_debt_cr is not None:
            gm.ev_revenue_ratio = round((market_cap_cr + v.net_debt_cr) / revenue_cr, 2)

    # Promoter holding trend: use insider buying signal as a short-term proxy.
    if state.governance_data:
        insider = state.governance_data.insider_net_buying_3m
        if insider == "buying":
            gm.promoter_holding_trend_5y = "increasing"
        elif insider == "selling":
            gm.promoter_holding_trend_5y = "declining"

    # equity_dilution_3y_pct: requires historical shares outstanding — not yet tracked.
    # Left as None; Step 5M applies benefit of doubt.

    # Estimate listing age from Screener financial history length.
    # Screener only populates prior-year revenue (revenue_1y_ago_cr) for companies with
    # ≥ 1 full fiscal year of listed data, and computes 3Y CAGR only after 3 years.
    # These are conservative lower bounds — a company could have more history than Screener shows.
    if f:
        if f.revenue_1y_ago_cr is None:
            gm.listing_years = 0.5   # no prior-year revenue → likely < 1 year listed
        elif f.revenue_cagr_3y is None:
            gm.listing_years = 2.0   # 1Y available but no 3Y CAGR → listed 1-3 years
        # else: ≥ 3 years of data; listing_years stays None (not "recently listed")

    # Data quality flags for key unavailable metrics
    if gm.gross_margin_pct is None:
        gm.data_flags.append(
            "[DATA UNVERIFIED: gross_margin_pct — COGS not extracted from Screener for this company]"
        )
    if gm.rule_of_40_score is None:
        gm.data_flags.append(
            "[DATA UNVERIFIED: rule_of_40 — revenue CAGR 3Y or EBITDA margin unavailable]"
        )
    if gm.revenue_cagr_1y is None:
        gm.data_flags.append(
            "[DATA UNVERIFIED: revenue_cagr_1y — prior-year revenue not extracted; "
            "HT-G1 deceleration check skipped]"
        )

    for flag in gm.data_flags:
        state.add_flag(flag)

    state.growth_metrics = gm

    log.info(
        "growth_metrics_computed",
        ticker=state.ticker,
        gross_margin_pct=gm.gross_margin_pct,
        gross_margin_trend=gm.gross_margin_trend,
        rule_of_40=gm.rule_of_40_score,
        revenue_cagr_1y=gm.revenue_cagr_1y,
        ps_ratio=gm.ps_ratio,
        ev_revenue_ratio=gm.ev_revenue_ratio,
        roiic_proxy=gm.roiic_proxy_cfo_revenue,
        cash_runway_months=gm.cash_runway_months,
    )


class GrowthPipeline(InvestmentPipeline):
    """Growth analysis pipeline for identifying high-growth multibagger candidates.

    Inherits all data fetching, caching, and error recovery from InvestmentPipeline.
    Overrides the step sequence and adds growth metric computation after prefetch.
    """

    def __init__(self, claude: anthropic.AsyncAnthropic | None = None) -> None:
        super().__init__(claude)
        self.log = log

    async def analyze(self, ticker: str) -> AnalysisState:
        """Run the full growth analysis pipeline for a ticker."""
        ticker = ticker.upper().strip()
        state = AnalysisState(ticker=ticker)
        state.analysis_mode = AnalysisMode.GROWTH

        async with self.nse, self.screener, self.bse, self.trendlyne, self.yfinance:
            clients = {
                "nse": self.nse,
                "screener": self.screener,
                "bse": self.bse,
                "trendlyne": self.trendlyne,
                "yfinance": self.yfinance,
            }

            self.log.info("growth_pipeline_start", ticker=ticker)

            state.mode = await detect_mode(self.nse, state)
            self.log.info(
                "mode_detected",
                mode=state.mode.value,
                nifty_decline_pct=state.nifty_decline_pct,
            )

            # Base prefetch (quote, financials, shareholding, valuation) — from parent
            await self._prefetch_data(state, clients)

            # Compute GrowthMetrics from the prefetched data
            self._compute_growth_metrics(state)

            steps = [
                Step0GrowthPreScreen(self.claude, clients),
                Step1Governance(self.claude, clients),
                Step2Moat(self.claude, clients),
                Step3GrowthFinancials(self.claude, clients),
                Step4Tailwinds(self.claude, clients),
                Step5GrowthValuation(self.claude, clients),
                # P2: run peer dominance check before Step 5M and Step 6.
                # PEER_SWITCH terminates the pipeline, so running Step 7 here
                # saves the 5M multibagger Haiku call, Step 6 Technical, and
                # the full Step 7 fetch that would otherwise run anyway.
                Step7Peers(self.claude, clients),
                Step5MMultibagger(self.claude, clients),
                Step6Technical(self.claude, clients),
                Step8Premortem(self.claude, clients),
                Step9Output(self.claude, clients),
            ]

            if state.quote is None:
                state.add_flag(
                    "[ER-01: MARKET CAP UNKNOWN — quote unavailable; cap_size defaults to "
                    "mid_cap for WACC/MoS purposes; verify manually before acting on valuation]"
                )

            terminated_logged = False
            for step in steps:
                if state.is_terminated:
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

                elapsed = time.monotonic() - step_start
                self.log.info(
                    "step_complete",
                    step=step.step_number,
                    step_name=step.step_name,
                    elapsed_seconds=round(elapsed, 2),
                    ticker=ticker,
                )

                # Post-Step-2: re-classify sector if initial confidence was low.
                # Growth moat narrative often reveals a sector (e.g. a "tech" company
                # that is really a fintech NBFC).
                if isinstance(step, Step2Moat) and state.moat:
                    _, prior_confidence = classify_sector_with_confidence(
                        company_name=state.company_name or "",
                        ticker=ticker,
                    )
                    if prior_confidence < _SECTOR_RECLASS_THRESHOLD:
                        new_sector, new_conf = classify_sector_with_confidence(
                            company_name=state.company_name or "",
                            ticker=ticker,
                            moat_narrative=state.moat.moat_narrative or "",
                        )
                        if new_sector != state.sector_name:
                            old_sector = state.sector_name
                            state.sector_name = new_sector
                            state.add_flag(
                                f"[SECTOR RECLASSIFIED: '{old_sector}' → '{new_sector}' "
                                f"after moat analysis (confidence {new_conf:.0%})]"
                            )
                            self.log.info(
                                "sector_reclassified",
                                ticker=ticker,
                                from_sector=old_sector,
                                to_sector=new_sector,
                                new_confidence=new_conf,
                            )

            self.log.info(
                "pipeline_complete",
                ticker=ticker,
                recommendation=state.recommendation_type,
                conviction=state.conviction.value if state.conviction else None,
            )

            try:
                from src.db.repository import save_analysis
                await save_analysis(settings.db_path, state)
            except Exception as db_exc:
                self.log.warning("db_save_failed", ticker=ticker, error=str(db_exc))

            return state

    def _compute_growth_metrics(self, state: AnalysisState) -> None:
        compute_growth_metrics(state)
