"""Tests for the backtesting engine (src/backtest/engine.py)."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.engine import (
    GOVERNANCE_ORDER,
    BacktestSample,
    _close_on_or_after,
    _governance_bucket,
    _replay_governance,
    replay_group,
    run_backtest,
    summarize,
    summarize_by_governance,
)
from src.db.repository import save_snapshot
from src.models import GovernanceData, StockQuote
from tests.fixtures.sample_data import (
    BAD_GOVERNANCE,
    SAMPLE_FINANCIALS,
    SAMPLE_GOVERNANCE,
    SAMPLE_QUOTE,
    SAMPLE_VALUATION,
    WEAK_FINANCIALS,
)


def _group(ticker="RELIANCE", snapshot_date="2025-06-01", **overrides) -> dict:
    data = {
        "quote": SAMPLE_QUOTE.model_dump(mode="json"),
        "financials": SAMPLE_FINANCIALS.model_dump(mode="json"),
        "governance": SAMPLE_GOVERNANCE.model_dump(mode="json"),
        "valuation": SAMPLE_VALUATION.model_dump(mode="json"),
    }
    data.update(overrides)
    return {"ticker": ticker, "snapshot_date": snapshot_date, "data": data}


# ---------------------------------------------------------------------------
# replay_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_strong_company_reaches_valuation_bucket():
    sample = await replay_group(_group())
    assert sample is not None
    assert sample.verdict.startswith("VALUATION_")
    assert sample.prescreen_score is not None and sample.prescreen_score >= 5
    assert sample.start_price == pytest.approx(SAMPLE_QUOTE.cmp)
    assert sample.holding_days == (date.today() - date(2025, 6, 1)).days


@pytest.mark.asyncio
async def test_replay_weak_company_rejected():
    weak = _group(financials=WEAK_FINANCIALS.model_dump(mode="json"))
    sample = await replay_group(weak)
    assert sample is not None
    assert sample.verdict.startswith("REJECT_")


@pytest.mark.asyncio
async def test_replay_requires_quote_and_financials():
    group = _group()
    del group["data"]["financials"]
    assert await replay_group(group) is None


@pytest.mark.asyncio
async def test_replay_skips_corrupt_snapshot():
    group = _group(quote={"garbage": True})
    assert await replay_group(group) is None


# ---------------------------------------------------------------------------
# run_backtest — end-to-end with a seeded DB and mocked price source
# ---------------------------------------------------------------------------


class _MockYF:
    """Stand-in for YFinanceClient: fixed end quote and Nifty series."""

    def __init__(self, end_cmp: float, nifty_series: dict[str, float]):
        self._end_cmp = end_cmp
        self._nifty = nifty_series

    async def get_close_series(self, symbol: str, start: str):
        return self._nifty

    async def get_stock_quote(self, ticker: str):
        q = SAMPLE_QUOTE.model_dump()
        q["cmp"] = self._end_cmp
        return StockQuote(**q)


@pytest.mark.asyncio
async def test_run_backtest_computes_forward_and_excess_returns(tmp_path):
    db_path = str(tmp_path / "bt.db")
    snap_date = "2025-06-01"
    for dtype, model in (
        ("quote", SAMPLE_QUOTE),
        ("financials", SAMPLE_FINANCIALS),
        ("governance", SAMPLE_GOVERNANCE),
        ("valuation", SAMPLE_VALUATION),
    ):
        await save_snapshot(
            db_path, "RELIANCE", snap_date, dtype, model.model_dump(mode="json"), "test"
        )

    start = SAMPLE_QUOTE.cmp
    mock_yf = _MockYF(
        end_cmp=round(start * 1.20, 2),                       # stock +20%
        nifty_series={"2025-06-01": 100.0, "2026-06-01": 110.0},  # index +10%
    )
    samples = await run_backtest(db_path, min_age_days=90, yf_client=mock_yf)

    assert len(samples) == 1
    s = samples[0]
    assert s.fwd_return_pct == pytest.approx(20.0, abs=0.1)
    assert s.nifty_return_pct == pytest.approx(10.0, abs=0.1)
    assert s.excess_return_pct == pytest.approx(10.0, abs=0.2)


@pytest.mark.asyncio
async def test_run_backtest_skips_too_recent_snapshots(tmp_path):
    db_path = str(tmp_path / "bt.db")
    today = date.today().isoformat()
    await save_snapshot(
        db_path, "RELIANCE", today, "quote", SAMPLE_QUOTE.model_dump(mode="json"), "t"
    )
    await save_snapshot(
        db_path, "RELIANCE", today, "financials",
        SAMPLE_FINANCIALS.model_dump(mode="json"), "t",
    )
    samples = await run_backtest(
        db_path, min_age_days=90, yf_client=_MockYF(100.0, {})
    )
    assert samples == []


# ---------------------------------------------------------------------------
# summarize / helpers
# ---------------------------------------------------------------------------


def _sample(verdict: str, ret: float | None, excess: float | None = None) -> BacktestSample:
    return BacktestSample(
        ticker="X",
        snapshot_date="2025-01-01",
        verdict=verdict,
        prescreen_score=7,
        mos_pct=None,
        start_price=100.0,
        holding_days=365,
        fwd_return_pct=ret,
        excess_return_pct=excess,
    )


def test_summarize_buckets_and_win_rate():
    samples = [
        _sample("VALUATION_GREEN", 30.0, 18.0),
        _sample("VALUATION_GREEN", -10.0, -22.0),
        _sample("VALUATION_GREEN", None),          # unpriced — counted separately
        _sample("REJECT_PRESCREEN", -25.0, -37.0),
    ]
    rows = summarize(samples)

    green = next(r for r in rows if r["verdict"] == "VALUATION_GREEN")
    assert green["samples"] == 3
    assert green["priced"] == 2
    assert green["median_return_pct"] == pytest.approx(10.0)
    assert green["win_rate_pct"] == pytest.approx(50.0)
    assert green["median_excess_pct"] == pytest.approx(-2.0)

    # Buckets come out in VERDICT_ORDER: GREEN before REJECT_PRESCREEN.
    assert [r["verdict"] for r in rows] == ["VALUATION_GREEN", "REJECT_PRESCREEN"]


def test_close_on_or_after_skips_weekend_gap():
    series = {"2025-06-02": 101.0, "2025-06-03": 102.0}
    # 2025-06-01 was a Sunday — first trading day after is used.
    assert _close_on_or_after(series, "2025-06-01") == 101.0
    assert _close_on_or_after(series, "2025-06-03") == 102.0
    assert _close_on_or_after(series, "2025-06-04") is None


# ---------------------------------------------------------------------------
# Governance replay (P1 recommendation: validate Step 1's deterministic score)
# ---------------------------------------------------------------------------


def test_replay_governance_strong_company_scores_high():
    """SAMPLE_GOVERNANCE: pledging 0% (3), audit PWC clean (3), RPT 5.2% (3),
    regulatory not-checked default (2) → 11/12, no hard trigger."""
    score, hard_trigger = _replay_governance(SAMPLE_GOVERNANCE)
    assert score == 11
    assert hard_trigger is False


def test_replay_governance_bad_company_hits_hard_trigger():
    """BAD_GOVERNANCE: pledging 18.5% > 10% → hard trigger fires regardless of score."""
    score, hard_trigger = _replay_governance(BAD_GOVERNANCE)
    assert hard_trigger is True
    assert score is not None  # score is still computed even when triggered


def test_replay_governance_none_data_returns_none():
    score, hard_trigger = _replay_governance(None)
    assert score is None
    assert hard_trigger is False


def test_governance_bucket_boundaries():
    assert _governance_bucket(9, False) == "GOV_HIGH"
    assert _governance_bucket(12, False) == "GOV_HIGH"
    assert _governance_bucket(8, False) == "GOV_MEDIUM"
    assert _governance_bucket(6, False) == "GOV_MEDIUM"
    assert _governance_bucket(5, False) == "GOV_LOW"
    assert _governance_bucket(0, False) == "GOV_LOW"
    assert _governance_bucket(None, False) is None


def test_governance_bucket_hard_trigger_overrides_score():
    """A hard trigger always buckets as GOV_HARD_TRIGGER, even with a high raw score."""
    assert _governance_bucket(12, True) == "GOV_HARD_TRIGGER"


@pytest.mark.asyncio
async def test_replay_group_populates_governance_fields():
    sample = await replay_group(_group())
    assert sample is not None
    assert sample.governance_score == 11
    assert sample.governance_bucket == "GOV_HIGH"
    assert sample.governance_hard_trigger is False


@pytest.mark.asyncio
async def test_replay_group_hard_trigger_governance_bucketed_separately():
    group = _group(governance=BAD_GOVERNANCE.model_dump(mode="json"))
    sample = await replay_group(group)
    assert sample is not None
    assert sample.governance_hard_trigger is True
    assert sample.governance_bucket == "GOV_HARD_TRIGGER"


@pytest.mark.asyncio
async def test_replay_group_no_governance_snapshot_leaves_fields_none():
    group = _group()
    del group["data"]["governance"]
    sample = await replay_group(group)
    assert sample is not None
    assert sample.governance_score is None
    assert sample.governance_bucket is None
    assert sample.governance_hard_trigger is False


def _gov_sample(bucket: str | None, ret: float | None) -> BacktestSample:
    return BacktestSample(
        ticker="X",
        snapshot_date="2025-01-01",
        verdict="VALUATION_GREEN",
        prescreen_score=7,
        mos_pct=None,
        start_price=100.0,
        holding_days=365,
        fwd_return_pct=ret,
        governance_bucket=bucket,
    )


def test_summarize_by_governance_buckets_and_orders():
    samples = [
        _gov_sample("GOV_HIGH", 25.0),
        _gov_sample("GOV_HIGH", 15.0),
        _gov_sample("GOV_LOW", -10.0),
        _gov_sample(None, 5.0),  # no governance data — excluded
    ]
    rows = summarize_by_governance(samples)

    high = next(r for r in rows if r["governance_bucket"] == "GOV_HIGH")
    assert high["samples"] == 2
    assert high["median_return_pct"] == pytest.approx(20.0)

    # None-bucket samples don't appear at all.
    assert all(r["governance_bucket"] is not None for r in rows)
    assert [r["governance_bucket"] for r in rows] == ["GOV_HIGH", "GOV_LOW"]


def test_summarize_by_governance_empty_returns_empty():
    assert summarize_by_governance([]) == []


def test_governance_order_constant_matches_bucket_function_outputs():
    """Every value _governance_bucket can return must be listed in GOVERNANCE_ORDER."""
    possible = {
        _governance_bucket(10, False),
        _governance_bucket(7, False),
        _governance_bucket(2, False),
        _governance_bucket(10, True),
    }
    assert possible == set(GOVERNANCE_ORDER)


def test_replay_governance_uses_real_governance_data_instance():
    """Sanity check the function accepts a live GovernanceData, not just fixtures."""
    g = GovernanceData(promoter_pledging_pct=0.0, pledging_trend_direction="stable")
    score, hard_trigger = _replay_governance(g)
    assert hard_trigger is False
    assert score is not None
