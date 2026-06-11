"""Tests for BatchScanner — universe fetch, pre-screen, ranking."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.batch_scanner import BatchScanner, PreScreenSummary, rank_results
from src.models import (
    AnalysisState,
    ConvictionLevel,
    GateResult,
    GovernanceScore,
    PreScreenResult,
    ValuationResult,
)
from tests.fixtures.sample_data import SAMPLE_FINANCIALS, SAMPLE_GOVERNANCE, SAMPLE_QUOTE


def _mock_yfinance_client(quote=None):
    """Build a mock YFinanceClient that returns None by default (NSE data available)."""
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    mock.get_stock_quote = AsyncMock(return_value=quote)
    return mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prescreen_result(score: int) -> PreScreenResult:
    gate = (
        GateResult.PASS_GREEN if score >= 7
        else GateResult.PASS_CONDITIONAL if score >= 5
        else GateResult.FAIL
    )
    return PreScreenResult(score=score, gate=gate)


def _make_state(
    ticker: str,
    rec: str = "BUY",
    conviction: str = "high",
    mos: float = 30.0,
    gov_score: int = 13,
) -> AnalysisState:
    state = AnalysisState(ticker=ticker)
    state.recommendation_type = rec
    state.conviction = ConvictionLevel(conviction)
    state.valuation = ValuationResult(
        gate=GateResult.PASS_GREEN,
        margin_of_safety_pct=mos,
        methods_in_buy_zone=3,
    )
    state.governance = GovernanceScore(score=gov_score, gate=GateResult.PASS_GREEN)
    return state


# ---------------------------------------------------------------------------
# rank_results
# ---------------------------------------------------------------------------


def test_rank_results_buy_before_watchlist():
    buy = _make_state("TCS", rec="BUY", conviction="high", mos=35.0)
    watch = _make_state("INFY", rec="WATCHLIST", conviction="high", mos=5.0)
    ranked = rank_results([watch, buy])
    assert ranked[0].ticker == "TCS"
    assert ranked[1].ticker == "INFY"


def test_rank_results_higher_conviction_first():
    high = _make_state("TCS", rec="BUY", conviction="high", mos=30.0)
    low = _make_state("INFY", rec="BUY", conviction="low", mos=30.0)
    ranked = rank_results([low, high])
    assert ranked[0].ticker == "TCS"


def test_rank_results_higher_mos_first_among_equal_conviction():
    a = _make_state("AAA", rec="BUY", conviction="medium", mos=40.0)
    b = _make_state("BBB", rec="BUY", conviction="medium", mos=25.0)
    ranked = rank_results([b, a])
    assert ranked[0].ticker == "AAA"


def test_rank_results_empty_list():
    assert rank_results([]) == []


def test_rank_results_all_rejects():
    states = [_make_state(t, rec="REJECT") for t in ["X", "Y", "Z"]]
    ranked = rank_results(states)
    assert len(ranked) == 3


# ---------------------------------------------------------------------------
# BatchScanner.get_universe — fallback chain
# ---------------------------------------------------------------------------


def _mock_nse_client(side_effect=None, return_value=None):
    """Return a patched NSEClient context manager."""
    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        instance.get_index_constituents.side_effect = side_effect
    else:
        instance.get_index_constituents.return_value = return_value
    return instance


@pytest.mark.asyncio
async def test_get_universe_returns_nse_tickers_on_success():
    scanner = BatchScanner(concurrency=2)
    expected = ["TCS", "HDFCBANK", "INFY"]

    with patch("src.agent.batch_scanner.NSEClient") as mock_nse_cls:
        mock_nse_cls.return_value = _mock_nse_client(return_value=expected)
        tickers = await scanner.get_universe("NIFTY 50")

    assert tickers == expected


@pytest.mark.asyncio
async def test_get_universe_falls_back_to_archives_on_nse_failure():
    """NSE API fails → archives CSV is tried and succeeds."""
    scanner = BatchScanner(concurrency=2)
    archives_tickers = ["RELIANCE", "TCS", "HDFCBANK"]

    with patch("src.agent.batch_scanner.NSEClient") as mock_nse_cls, \
         patch("src.agent.batch_scanner._fetch_constituents_from_archives", AsyncMock(return_value=archives_tickers)):
        mock_nse_cls.return_value = _mock_nse_client(side_effect=ValueError("403 Forbidden"))
        tickers = await scanner.get_universe("NIFTY 50")

    assert tickers == archives_tickers


@pytest.mark.asyncio
async def test_get_universe_falls_back_to_hardcoded_when_both_fail():
    """NSE API + archives both fail → hardcoded Nifty 50 list."""
    scanner = BatchScanner(concurrency=2)

    with patch("src.agent.batch_scanner.NSEClient") as mock_nse_cls, \
         patch("src.agent.batch_scanner._fetch_constituents_from_archives", AsyncMock(side_effect=Exception("timeout"))):
        mock_nse_cls.return_value = _mock_nse_client(side_effect=ValueError("403 Forbidden"))
        tickers = await scanner.get_universe("NIFTY 500")

    assert len(tickers) == 50
    assert "RELIANCE" in tickers


# ---------------------------------------------------------------------------
# BatchScanner.prescreen_universe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prescreen_universe_returns_summary_per_ticker():
    scanner = BatchScanner(concurrency=2)
    tickers = ["TCS", "INFY"]

    # Mock all three API clients
    mock_nse = AsyncMock()
    mock_nse.__aenter__ = AsyncMock(return_value=mock_nse)
    mock_nse.__aexit__ = AsyncMock(return_value=False)
    mock_nse.get_stock_quote = AsyncMock(return_value=SAMPLE_QUOTE)

    mock_screener = AsyncMock()
    mock_screener.__aenter__ = AsyncMock(return_value=mock_screener)
    mock_screener.__aexit__ = AsyncMock(return_value=False)
    mock_screener.get_financials = AsyncMock(return_value=SAMPLE_FINANCIALS)

    mock_bse = AsyncMock()
    mock_bse.__aenter__ = AsyncMock(return_value=mock_bse)
    mock_bse.__aexit__ = AsyncMock(return_value=False)
    mock_bse.get_shareholding = AsyncMock(return_value=SAMPLE_GOVERNANCE)

    with (
        patch("src.agent.batch_scanner.NSEClient", return_value=mock_nse),
        patch("src.agent.batch_scanner.ScreenerClient", return_value=mock_screener),
        patch("src.agent.batch_scanner.BSEClient", return_value=mock_bse),
        patch("src.agent.batch_scanner.YFinanceClient", return_value=_mock_yfinance_client()),
    ):
        summaries = await scanner.prescreen_universe(tickers)

    assert len(summaries) == 2
    assert all(isinstance(s, PreScreenSummary) for s in summaries)
    assert {s.ticker for s in summaries} == {"TCS", "INFY"}


@pytest.mark.asyncio
async def test_prescreen_handles_data_fetch_failure_gracefully():
    """When all data sources return None, Step 0 scores 0/9 FAIL — no crash."""
    scanner = BatchScanner(concurrency=1)

    mock_nse = AsyncMock()
    mock_nse.__aenter__ = AsyncMock(return_value=mock_nse)
    mock_nse.__aexit__ = AsyncMock(return_value=False)
    mock_nse.get_stock_quote = AsyncMock(return_value=None)

    mock_screener = AsyncMock()
    mock_screener.__aenter__ = AsyncMock(return_value=mock_screener)
    mock_screener.__aexit__ = AsyncMock(return_value=False)
    mock_screener.get_financials = AsyncMock(return_value=None)
    mock_screener.get_shareholding = AsyncMock(return_value=None)

    mock_bse = AsyncMock()
    mock_bse.__aenter__ = AsyncMock(return_value=mock_bse)
    mock_bse.__aexit__ = AsyncMock(return_value=False)
    mock_bse.get_shareholding = AsyncMock(return_value=None)

    with (
        patch("src.agent.batch_scanner.NSEClient", return_value=mock_nse),
        patch("src.agent.batch_scanner.ScreenerClient", return_value=mock_screener),
        patch("src.agent.batch_scanner.BSEClient", return_value=mock_bse),
        patch("src.agent.batch_scanner.YFinanceClient", return_value=_mock_yfinance_client()),
    ):
        summaries = await scanner.prescreen_universe(["NOTICKER"])

    s = summaries[0]
    assert s.error is None          # handled gracefully, not an unhandled exception
    assert s.score == 0
    assert s.gate == GateResult.FAIL


@pytest.mark.asyncio
async def test_prescreen_records_error_when_prescreen_one_raises():
    """If _prescreen_one itself raises (e.g. bug inside Step0), the error is captured."""
    scanner = BatchScanner(concurrency=1)

    mock_nse = AsyncMock()
    mock_nse.__aenter__ = AsyncMock(return_value=mock_nse)
    mock_nse.__aexit__ = AsyncMock(return_value=False)

    mock_screener = AsyncMock()
    mock_screener.__aenter__ = AsyncMock(return_value=mock_screener)
    mock_screener.__aexit__ = AsyncMock(return_value=False)

    mock_bse = AsyncMock()
    mock_bse.__aenter__ = AsyncMock(return_value=mock_bse)
    mock_bse.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.agent.batch_scanner.NSEClient", return_value=mock_nse),
        patch("src.agent.batch_scanner.ScreenerClient", return_value=mock_screener),
        patch("src.agent.batch_scanner.BSEClient", return_value=mock_bse),
        patch("src.agent.batch_scanner.YFinanceClient", return_value=_mock_yfinance_client()),
        patch.object(scanner, "_prescreen_one", side_effect=RuntimeError("boom")),
    ):
        summaries = await scanner.prescreen_universe(["CRASHER"])

    assert summaries[0].error is not None
    assert "boom" in summaries[0].error
    assert summaries[0].gate == GateResult.NOT_RUN


# ---------------------------------------------------------------------------
# BatchScanner.scan — prescreen_only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_prescreen_only_skips_full_pipeline():
    scanner = BatchScanner(concurrency=2)

    with patch.object(scanner, "get_universe", return_value=["TCS"]):
        mock_summary = PreScreenSummary(
            ticker="TCS", score=8, gate=GateResult.PASS_GREEN
        )
        with patch.object(scanner, "prescreen_universe", return_value=[mock_summary]):
            summaries, results = await scanner.scan(prescreen_only=True)

    assert results == []
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_scan_filters_by_min_score():
    scanner = BatchScanner(concurrency=2)
    summaries_fixture = [
        PreScreenSummary(ticker="TCS", score=8, gate=GateResult.PASS_GREEN),
        PreScreenSummary(ticker="JUNK", score=2, gate=GateResult.FAIL),
    ]

    pipeline_mock = AsyncMock()
    pipeline_mock.analyze = AsyncMock(return_value=_make_state("TCS"))

    with (
        patch.object(scanner, "get_universe", return_value=["TCS", "JUNK"]),
        patch.object(scanner, "prescreen_universe", return_value=summaries_fixture),
        patch("src.agent.batch_scanner.InvestmentPipeline", return_value=pipeline_mock),
    ):
        summaries, results = await scanner.scan(prescreen_min_score=5, prescreen_only=False)

    # Only TCS (score=8) should have gone to full analysis
    pipeline_mock.analyze.assert_awaited_once_with("TCS")
    assert len(results) == 1
    assert results[0].ticker == "TCS"


@pytest.mark.asyncio
async def test_scan_respects_max_full_analyses():
    scanner = BatchScanner(concurrency=2)
    summaries_fixture = [
        PreScreenSummary(ticker=f"STOCK{i}", score=8, gate=GateResult.PASS_GREEN)
        for i in range(5)
    ]

    pipeline_mock = MagicMock()
    pipeline_mock.analyze = AsyncMock(
        side_effect=[_make_state(f"STOCK{i}") for i in range(5)]
    )

    with (
        patch.object(scanner, "get_universe", return_value=[s.ticker for s in summaries_fixture]),
        patch.object(scanner, "prescreen_universe", return_value=summaries_fixture),
        patch("src.agent.batch_scanner.InvestmentPipeline", return_value=pipeline_mock),
    ):
        _, results = await scanner.scan(max_full_analyses=2, prescreen_only=False)

    assert pipeline_mock.analyze.await_count == 2
    assert len(results) == 2


# ---------------------------------------------------------------------------
# candidate_sort_key — deterministic Phase 3 cut among tied Step 0 scores
# ---------------------------------------------------------------------------


def _tied_summary(ticker, score=8, roce=None, cfo=None, below=None):
    return PreScreenSummary(
        ticker=ticker,
        score=score,
        gate=GateResult.PASS_GREEN,
        roce_5y=roce,
        cfo_np_3y=cfo,
        pct_below_52w_high=below,
    )


def test_score_dominates_tiebreakers():
    from src.agent.batch_scanner import candidate_sort_key

    low = _tied_summary("LOW", score=7, roce=40.0)
    high = _tied_summary("HIGH", score=9, roce=5.0)
    assert sorted([low, high], key=candidate_sort_key)[0].ticker == "HIGH"


def test_tied_scores_break_on_roce():
    from src.agent.batch_scanner import candidate_sort_key

    ordered = sorted(
        [_tied_summary("AAA", roce=15.0), _tied_summary("BBB", roce=25.0)],
        key=candidate_sort_key,
    )
    assert [s.ticker for s in ordered] == ["BBB", "AAA"]


def test_roce_tie_breaks_on_cfo_np():
    from src.agent.batch_scanner import candidate_sort_key

    a = _tied_summary("AAA", roce=20.0, cfo=60.0)
    b = _tied_summary("BBB", roce=20.0, cfo=90.0)
    assert sorted([a, b], key=candidate_sort_key)[0].ticker == "BBB"


def test_cfo_tie_breaks_on_distance_below_52w_high():
    from src.agent.batch_scanner import candidate_sort_key

    a = _tied_summary("AAA", roce=20.0, cfo=80.0, below=5.0)
    b = _tied_summary("BBB", roce=20.0, cfo=80.0, below=25.0)
    assert sorted([a, b], key=candidate_sort_key)[0].ticker == "BBB"


def test_missing_metric_sorts_after_present():
    from src.agent.batch_scanner import candidate_sort_key

    has_data = _tied_summary("ZZZ", roce=10.0)
    missing = _tied_summary("AAA", roce=None)  # alphabetically first, but no data
    assert sorted([has_data, missing], key=candidate_sort_key)[0].ticker == "ZZZ"


def test_full_tie_falls_back_to_ticker_for_determinism():
    from src.agent.batch_scanner import candidate_sort_key

    assert (
        sorted([_tied_summary("ZED"), _tied_summary("ALPHA")], key=candidate_sort_key)[0].ticker
        == "ALPHA"
    )


@pytest.mark.asyncio
async def test_prescreen_populates_tiebreaker_fields():
    """_prescreen_one must capture ROCE / CFO-NP / %-below-high from Phase 2 data."""
    scanner = BatchScanner(concurrency=2)

    mock_nse = AsyncMock()
    mock_nse.__aenter__ = AsyncMock(return_value=mock_nse)
    mock_nse.__aexit__ = AsyncMock(return_value=False)
    mock_nse.get_stock_quote = AsyncMock(return_value=SAMPLE_QUOTE)

    mock_screener = AsyncMock()
    mock_screener.__aenter__ = AsyncMock(return_value=mock_screener)
    mock_screener.__aexit__ = AsyncMock(return_value=False)
    mock_screener.get_financials = AsyncMock(return_value=SAMPLE_FINANCIALS)

    mock_bse = AsyncMock()
    mock_bse.__aenter__ = AsyncMock(return_value=mock_bse)
    mock_bse.__aexit__ = AsyncMock(return_value=False)
    mock_bse.get_shareholding = AsyncMock(return_value=SAMPLE_GOVERNANCE)

    with (
        patch("src.agent.batch_scanner.NSEClient", return_value=mock_nse),
        patch("src.agent.batch_scanner.ScreenerClient", return_value=mock_screener),
        patch("src.agent.batch_scanner.BSEClient", return_value=mock_bse),
        patch("src.agent.batch_scanner.YFinanceClient", return_value=_mock_yfinance_client()),
    ):
        summaries = await scanner.prescreen_universe(["RELIANCE"])

    s = summaries[0]
    assert s.roce_5y == pytest.approx(SAMPLE_FINANCIALS.roce_5y_avg)
    assert s.cfo_np_3y == pytest.approx(SAMPLE_FINANCIALS.cfo_net_profit_3y_avg)
    expected_below = (SAMPLE_QUOTE.w52_high - SAMPLE_QUOTE.cmp) / SAMPLE_QUOTE.w52_high * 100
    assert s.pct_below_52w_high == pytest.approx(expected_below, abs=0.01)
