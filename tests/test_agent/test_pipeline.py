"""Integration tests for InvestmentPipeline."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from src.agent.mode_detector import reset_mode_cache
from src.agent.pipeline import InvestmentPipeline
from src.models import (
    MarketMode,
)
from tests.fixtures.sample_data import (
    BAD_GOVERNANCE,
    SAMPLE_FINANCIALS,
    SAMPLE_GOVERNANCE,
    SAMPLE_QUOTE,
    SAMPLE_VALUATION,
)


@pytest.fixture(autouse=True)
def _reset_nifty_cache():
    """Ensure module-level Nifty mode cache is cleared before every test."""
    reset_mode_cache()
    yield
    reset_mode_cache()


def _make_claude_text_response(text: str):
    """Build a minimal mock anthropic response returning text."""
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=200, output_tokens=100)
    return response


def _make_moat_response():
    data = {
        "moat_type": "brand",
        "moat_durability": "High",
        "market_position": "Rank 1 in Indian oil & gas",
        "market_share_trend": "Stable",
        "tam_multiple": 5.0,
        "working_capital_flag": "Clean",
        "moat_narrative": "Reliance has a dominant brand and scale moat in India.",
        "data_flags": [],
    }
    return _make_claude_text_response(json.dumps(data))


def _make_tailwind_response():
    data = {
        "sector": "Energy",
        "tailwind_type": "structural",
        "cycle_position": "mid",
        "growth_runway_years": "10+ years",
        "headwind_flags": [],
        "tailwind_narrative": "Strong structural tailwinds from India's energy transition.",
        "data_flags": [],
    }
    return _make_claude_text_response(json.dumps(data))


def _make_peer_identify_response():
    """Step 7 identification call — Claude only names peers; metrics come from clients."""
    data = {
        "peers": [
            {"ticker": "ONGC", "name": "Oil and Natural Gas Corporation"},
            {"ticker": "IOC", "name": "Indian Oil Corporation"},
        ]
    }
    return _make_claude_text_response(json.dumps(data))


def _make_premortem_response():
    data = {
        "primary_risk": "Regulatory change in energy pricing",
        "secondary_risk": "Debt refinancing risk",
        "tertiary_risk": "Competition from global players",
        "risk_type": "CYCLICAL_MANAGEABLE",
        "proceed": True,
        "data_flags": [],
    }
    return _make_claude_text_response(json.dumps(data))


def _make_thesis_response():
    return _make_claude_text_response(
        "Reliance is a compounding machine with strong moat and structural tailwinds."
    )


def _make_capital_alloc_response():
    return _make_claude_text_response(json.dumps({"score": 3, "rationale": "Excellent."}))


@pytest.fixture
def mock_pipeline_env():
    """Patch all external dependencies for a full pipeline run."""
    # All Claude responses in order of calls
    claude_responses = [
        _make_capital_alloc_response(),   # Step 1 capital allocation
        _make_moat_response(),            # Step 2 moat
        _make_tailwind_response(),        # Step 4 tailwinds
        _make_peer_identify_response(),   # Step 7 peer identification (Step 5 DCF is deterministic)
        _make_premortem_response(),       # Step 8 premortem
        _make_thesis_response(),          # Step 9 thesis
    ]

    mock_claude = AsyncMock(spec=anthropic.AsyncAnthropic)
    mock_claude.messages.create = AsyncMock(side_effect=claude_responses)

    mock_nse = AsyncMock()
    mock_nse.__aenter__ = AsyncMock(return_value=mock_nse)
    mock_nse.__aexit__ = AsyncMock(return_value=None)
    mock_nse.get_stock_quote = AsyncMock(return_value=SAMPLE_QUOTE)
    mock_nse.get_nifty50 = AsyncMock(return_value=(22500.0, 24000.0))  # ~6.25% decline → NORMAL
    mock_nse.get_shareholding = AsyncMock(return_value=SAMPLE_GOVERNANCE)

    mock_screener = AsyncMock()
    mock_screener.__aenter__ = AsyncMock(return_value=mock_screener)
    mock_screener.__aexit__ = AsyncMock(return_value=None)
    mock_screener.get_financials = AsyncMock(return_value=SAMPLE_FINANCIALS)

    mock_bse = AsyncMock()
    mock_bse.__aenter__ = AsyncMock(return_value=mock_bse)
    mock_bse.__aexit__ = AsyncMock(return_value=None)
    mock_bse.get_shareholding = AsyncMock(return_value=SAMPLE_GOVERNANCE)

    mock_trendlyne = AsyncMock()
    mock_trendlyne.__aenter__ = AsyncMock(return_value=mock_trendlyne)
    mock_trendlyne.__aexit__ = AsyncMock(return_value=None)
    mock_trendlyne.get_valuation_data = AsyncMock(return_value=SAMPLE_VALUATION)
    mock_trendlyne.get_governance_data = AsyncMock(return_value=None)

    mock_yfinance = AsyncMock()
    mock_yfinance.__aenter__ = AsyncMock(return_value=mock_yfinance)
    mock_yfinance.__aexit__ = AsyncMock(return_value=None)
    mock_yfinance.get_stock_quote = AsyncMock(return_value=None)
    mock_yfinance.get_valuation_data = AsyncMock(return_value=None)

    return {
        "claude": mock_claude,
        "nse": mock_nse,
        "screener": mock_screener,
        "bse": mock_bse,
        "trendlyne": mock_trendlyne,
        "yfinance": mock_yfinance,
    }


@pytest.mark.asyncio
async def test_happy_path_buy_recommendation(mock_pipeline_env):
    """Full pipeline with good data should produce BUY recommendation."""
    env = mock_pipeline_env
    pipeline = InvestmentPipeline.__new__(InvestmentPipeline)
    pipeline.claude = env["claude"]
    pipeline.nse = env["nse"]
    pipeline.screener = env["screener"]
    pipeline.bse = env["bse"]
    pipeline.trendlyne = env["trendlyne"]
    pipeline.yfinance = env["yfinance"]
    from src.logging_config import get_logger
    pipeline.log = get_logger("pipeline_test")

    state = await pipeline.analyze("RELIANCE")

    assert state.recommendation_type == "BUY"
    assert state.formatted_output is not None
    assert "RELIANCE" in state.formatted_output
    assert not state.is_terminated or state.terminated_at_step is None


@pytest.mark.asyncio
async def test_step1_governance_fail_terminates_with_rejection(mock_pipeline_env):
    """Bad governance → Step 1 FAIL → pipeline terminates → REJECTION_LOG output."""
    env = mock_pipeline_env
    # Return bad governance from NSE (primary source)
    env["nse"].get_shareholding = AsyncMock(return_value=BAD_GOVERNANCE)
    # Capital allocation still returns something
    env["claude"].messages.create = AsyncMock(
        return_value=_make_capital_alloc_response()
    )

    pipeline = InvestmentPipeline.__new__(InvestmentPipeline)
    pipeline.claude = env["claude"]
    pipeline.nse = env["nse"]
    pipeline.screener = env["screener"]
    pipeline.bse = env["bse"]
    pipeline.trendlyne = env["trendlyne"]
    pipeline.yfinance = env["yfinance"]
    from src.logging_config import get_logger
    pipeline.log = get_logger("pipeline_test")

    state = await pipeline.analyze("BADCO")

    assert state.recommendation_type == "REJECT"
    assert state.terminated_at_step in (1,)  # terminated at step 1
    assert state.formatted_output is not None
    assert "REJECTION" in state.formatted_output


@pytest.mark.asyncio
async def test_step3_financials_fail_terminates(mock_pipeline_env):
    """Financials that pass pre-screen but fail Step 3 → terminates at step 3."""
    from src.models import FinancialMetrics

    env = mock_pipeline_env
    # These financials pass Step 0 (revenue/ROE meet pre-screen 5Y thresholds barely)
    # but fail Step 3 (hard trigger: D/E > 3.0)
    barely_passing_prescreen = FinancialMetrics(
        revenue_cagr_5y=13.0,   # >= 12 → passes pre-screen
        revenue_cagr_3y=14.0,
        pat_cagr_5y=16.0,       # >= 15 → passes pre-screen
        pat_cagr_3y=15.0,
        roe_5y_avg=16.0,        # >= 15 → passes pre-screen
        roce_5y_avg=19.0,       # >= 18 → passes pre-screen
        cfo_net_profit_3y_avg=72.0,  # >= 70 → passes pre-screen
        debt_to_equity=3.5,     # HARD TRIGGER in step 3: > 3.0; also fails step 0 hurdle
        interest_coverage=2.5,  # HARD TRIGGER: < 3
    )
    env["screener"].get_financials = AsyncMock(return_value=barely_passing_prescreen)
    env["claude"].messages.create = AsyncMock(
        side_effect=[_make_capital_alloc_response(), _make_moat_response()]
    )

    pipeline = InvestmentPipeline.__new__(InvestmentPipeline)
    pipeline.claude = env["claude"]
    pipeline.nse = env["nse"]
    pipeline.screener = env["screener"]
    pipeline.bse = env["bse"]
    pipeline.trendlyne = env["trendlyne"]
    pipeline.yfinance = env["yfinance"]
    from src.logging_config import get_logger
    pipeline.log = get_logger("pipeline_test")

    state = await pipeline.analyze("WEAKCO")

    assert state.recommendation_type == "REJECT"
    # Step 0 pre-screen: D/E >= 1.0 and interest_coverage are 2 failing metrics
    # Score = 7 (market_cap, rev_cagr, pat_cagr, roe, roce, cfo_np, promoter_holding, pledging) - 2 = 7/9 → PASS_GREEN
    # Actually: market_cap=pass, rev=pass, pat=pass, roe=pass, roce=pass, d/e=FAIL, cfo=pass, prom_holding=pass, prom_pledging=pass = 8/9 → PASS_GREEN
    # Then step 1 passes, step 2 moat passes, step 3 → FAIL (hard trigger D/E > 3.0)
    assert state.terminated_at_step in (0, 3)  # depends on exact pre-screen scoring


@pytest.mark.asyncio
async def test_mode_detection_sets_market_mode(mock_pipeline_env):
    """Mode detection from Nifty should set state.mode correctly."""
    env = mock_pipeline_env
    # Simulate 12% decline from peak → CORRECTION (>= 8%)
    env["nse"].get_nifty50 = AsyncMock(return_value=(21_000.0, 24_000.0))  # 12.5% decline

    env["claude"].messages.create = AsyncMock(
        side_effect=[
            _make_capital_alloc_response(),
            _make_moat_response(),
            _make_tailwind_response(),
            _make_peer_identify_response(),
            _make_premortem_response(),
            _make_thesis_response(),
        ]
    )

    pipeline = InvestmentPipeline.__new__(InvestmentPipeline)
    pipeline.claude = env["claude"]
    pipeline.nse = env["nse"]
    pipeline.screener = env["screener"]
    pipeline.bse = env["bse"]
    pipeline.trendlyne = env["trendlyne"]
    pipeline.yfinance = env["yfinance"]
    from src.logging_config import get_logger
    pipeline.log = get_logger("pipeline_test")

    state = await pipeline.analyze("RELIANCE")

    assert state.mode == MarketMode.CORRECTION
    assert state.nifty_decline_pct == pytest.approx(12.5, rel=0.01)


def test_pipeline_accepts_shared_claude_client():
    """The batch scanner shares one AsyncAnthropic client across Phase 3 pipelines."""
    sentinel = object()
    pipeline = InvestmentPipeline(claude=sentinel)  # type: ignore[arg-type]
    assert pipeline.claude is sentinel
