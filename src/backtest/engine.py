"""Backtesting harness — validate the deterministic gates against history.

Every analysis and scan stores point-in-time raw data in ``data_snapshots``.
This engine replays those snapshots through the deterministic steps
(0 pre-screen, 3 financials, 5 valuation — zero LLM calls, zero API cost
beyond two price lookups) to reproduce the verdict the pipeline *would* have
given on that date, then measures the forward price return to today and the
excess over the Nifty 50 across the same window.

If the gates carry signal, VALUATION_GREEN samples should outperform
VALUATION_FAIL / REJECT buckets and the index.  Where they don't, the
thresholds (pre-screen cut-off, MoS bands, WACC) deserve re-tuning.

Known limitations (deterministic replay only):
- Governance (Step 1) and moat (Step 2) LLM judgments are not replayed; the
  deterministic governance *data* still feeds the Step 0 promoter checks.
- Market mode defaults to NORMAL — historical Nifty drawdown state is not
  reconstructed, so required-MoS uses the normal-mode bands.
"""
from __future__ import annotations

import asyncio
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median

from src.agent.steps.step0_prescreen import Step0PreScreen
from src.agent.steps.step3_financials import Step3Financials
from src.agent.steps.step5_valuation import Step5Valuation
from src.db.repository import get_snapshot_groups_before
from src.logging_config import get_logger
from src.models import (
    AnalysisState,
    FinancialMetrics,
    GateResult,
    GovernanceData,
    StockQuote,
    ValuationData,
)
from src.sector.classifier import classify_sector_with_confidence

log = get_logger("backtest")

_NIFTY_SYMBOL = "^NSEI"
_PRICE_FETCH_CONCURRENCY = 4

# Display / aggregation order: best expected bucket first.
VERDICT_ORDER = [
    "VALUATION_GREEN",
    "VALUATION_CONDITIONAL",
    "VALUATION_FAIL",
    "REJECT_FINANCIALS",
    "REJECT_PRESCREEN",
]

_GATE_TO_VERDICT = {
    GateResult.PASS_GREEN: "VALUATION_GREEN",
    GateResult.PASS_CONDITIONAL: "VALUATION_CONDITIONAL",
    GateResult.FAIL: "VALUATION_FAIL",
}


@dataclass
class BacktestSample:
    """One historical (ticker, snapshot_date) replayed through the gates."""

    ticker: str
    snapshot_date: str
    verdict: str
    prescreen_score: int | None
    mos_pct: float | None
    start_price: float          # CMP stored in the snapshot — exactly what the pipeline saw
    holding_days: int
    end_price: float | None = None
    fwd_return_pct: float | None = None
    nifty_return_pct: float | None = None
    excess_return_pct: float | None = None


async def replay_group(group: dict) -> BacktestSample | None:
    """Reconstruct AnalysisState from one snapshot group and run Steps 0/3/5.

    Returns None when the group lacks quote+financials or fails validation.
    """
    data = group["data"]
    if "quote" not in data or "financials" not in data:
        return None
    try:
        quote = StockQuote.model_validate(data["quote"])
        financials = FinancialMetrics.model_validate(data["financials"])
    except Exception as exc:
        log.debug(
            "backtest_snapshot_invalid",
            ticker=group["ticker"],
            snapshot_date=group["snapshot_date"],
            error=str(exc),
        )
        return None
    if quote.cmp is None or quote.cmp <= 0:
        return None

    state = AnalysisState(ticker=group["ticker"])
    state.quote = quote
    state.company_name = quote.company_name
    state.financials = financials
    if "governance" in data:
        try:
            state.governance_data = GovernanceData.model_validate(data["governance"])
        except Exception:
            pass
    if "valuation" in data:
        try:
            state.valuation_data = ValuationData.model_validate(data["valuation"])
        except Exception:
            pass
    state.sector_name, _ = classify_sector_with_confidence(
        company_name=state.company_name or "", ticker=state.ticker
    )

    # Deterministic gates only — claude client is never touched by these steps.
    state = await Step0PreScreen(None, {}).run(state)  # type: ignore[arg-type]
    if state.is_terminated:
        verdict = "REJECT_PRESCREEN"
    else:
        state = await Step3Financials(None, {}).run(state)  # type: ignore[arg-type]
        if state.is_terminated:
            verdict = "REJECT_FINANCIALS"
        else:
            state = await Step5Valuation(None, {}).run(state)  # type: ignore[arg-type]
            gate = state.valuation.gate if state.valuation else GateResult.NOT_RUN
            verdict = _GATE_TO_VERDICT.get(gate, "VALUATION_CONDITIONAL")

    return BacktestSample(
        ticker=group["ticker"],
        snapshot_date=group["snapshot_date"],
        verdict=verdict,
        prescreen_score=state.pre_screen.score if state.pre_screen else None,
        mos_pct=state.valuation.margin_of_safety_pct if state.valuation else None,
        start_price=quote.cmp,
        holding_days=(date.today() - date.fromisoformat(group["snapshot_date"])).days,
    )


def _close_on_or_after(series: dict[str, float], target_iso: str) -> float | None:
    """First close on or after ``target_iso`` (handles weekends/holidays)."""
    keys = sorted(series)
    i = bisect_left(keys, target_iso)
    return series[keys[i]] if i < len(keys) else None


async def run_backtest(
    db_path: str,
    min_age_days: int = 90,
    ticker: str | None = None,
    yf_client=None,
) -> list[BacktestSample]:
    """Replay all snapshots older than ``min_age_days`` and attach forward returns.

    Start prices come from the stored snapshot CMP (point-in-time exact); end
    prices are live quotes; the Nifty 50 benchmark is fetched once for the
    whole window.  Samples whose end price cannot be fetched keep
    ``fwd_return_pct = None`` and are reported separately by ``summarize``.
    """
    cutoff = (date.today() - timedelta(days=min_age_days)).isoformat()
    groups = await get_snapshot_groups_before(db_path, cutoff, ticker)
    log.info("backtest_groups_loaded", groups=len(groups), cutoff=cutoff)

    samples: list[BacktestSample] = []
    for group in groups:
        sample = await replay_group(group)
        if sample is not None:
            samples.append(sample)
    if not samples:
        return []

    if yf_client is None:
        from src.api.yfinance_client import YFinanceClient

        yf_client = YFinanceClient()

    # Benchmark: one Nifty series covering every sample's window.
    earliest = min(s.snapshot_date for s in samples)
    nifty_series = await yf_client.get_close_series(_NIFTY_SYMBOL, earliest) or {}
    nifty_latest = nifty_series[max(nifty_series)] if nifty_series else None

    # Live end price per distinct ticker.
    semaphore = asyncio.Semaphore(_PRICE_FETCH_CONCURRENCY)

    async def _end_price(tkr: str) -> tuple[str, float | None]:
        async with semaphore:
            try:
                q = await yf_client.get_stock_quote(tkr)
            except Exception:
                q = None
            return tkr, (q.cmp if q else None)

    end_prices = dict(
        await asyncio.gather(*(_end_price(t) for t in {s.ticker for s in samples}))
    )

    for s in samples:
        s.end_price = end_prices.get(s.ticker)
        if s.end_price and s.start_price > 0:
            s.fwd_return_pct = round((s.end_price / s.start_price - 1) * 100, 2)
        if nifty_latest:
            nifty_start = _close_on_or_after(nifty_series, s.snapshot_date)
            if nifty_start:
                s.nifty_return_pct = round((nifty_latest / nifty_start - 1) * 100, 2)
        if s.fwd_return_pct is not None and s.nifty_return_pct is not None:
            s.excess_return_pct = round(s.fwd_return_pct - s.nifty_return_pct, 2)

    log.info(
        "backtest_complete",
        samples=len(samples),
        priced=sum(1 for s in samples if s.fwd_return_pct is not None),
    )
    return samples


def summarize(samples: list[BacktestSample]) -> list[dict]:
    """Aggregate samples per verdict bucket, in VERDICT_ORDER.

    win_rate counts positive absolute returns; median_excess compares to the
    Nifty over each sample's own window (the honest benchmark — different
    samples have different horizons).
    """
    rows: list[dict] = []
    for verdict in VERDICT_ORDER:
        bucket = [s for s in samples if s.verdict == verdict]
        if not bucket:
            continue
        returns = [s.fwd_return_pct for s in bucket if s.fwd_return_pct is not None]
        excess = [s.excess_return_pct for s in bucket if s.excess_return_pct is not None]
        rows.append(
            {
                "verdict": verdict,
                "samples": len(bucket),
                "priced": len(returns),
                "median_return_pct": round(median(returns), 2) if returns else None,
                "mean_return_pct": round(mean(returns), 2) if returns else None,
                "win_rate_pct": (
                    round(100 * sum(1 for r in returns if r > 0) / len(returns), 1)
                    if returns
                    else None
                ),
                "median_excess_pct": round(median(excess), 2) if excess else None,
                "avg_holding_days": round(mean(s.holding_days for s in bucket)),
            }
        )
    return rows
