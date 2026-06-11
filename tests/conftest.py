"""Shared test fixtures."""
from __future__ import annotations

from unittest.mock import AsyncMock

import anthropic
import pytest
import respx

from src.api.nse import NSEClient
from src.models import AnalysisState
from tests.fixtures.sample_data import (
    BAD_GOVERNANCE,
    SAMPLE_FINANCIALS,
    SAMPLE_GOVERNANCE,
    SAMPLE_QUOTE,
    SAMPLE_TECHNICAL,
    SAMPLE_VALUATION,
    WEAK_FINANCIALS,
)


@pytest.fixture
def sample_state() -> AnalysisState:
    """AnalysisState pre-loaded with RELIANCE sample data (all steps should pass)."""
    state = AnalysisState(ticker="RELIANCE")
    state.quote = SAMPLE_QUOTE
    state.financials = SAMPLE_FINANCIALS
    state.governance_data = SAMPLE_GOVERNANCE
    state.valuation_data = SAMPLE_VALUATION
    state.technical_data = SAMPLE_TECHNICAL
    state.company_name = "Reliance Industries Limited"
    return state


@pytest.fixture
def bad_governance_state() -> AnalysisState:
    """State with bad governance data — Step 1 should FAIL."""
    state = AnalysisState(ticker="BADCO")
    state.quote = SAMPLE_QUOTE
    state.financials = SAMPLE_FINANCIALS
    state.governance_data = BAD_GOVERNANCE
    state.company_name = "BadCo Industries"
    return state


@pytest.fixture
def weak_financials_state() -> AnalysisState:
    """State with weak financials — Step 3 should FAIL."""
    state = AnalysisState(ticker="WEAKCO")
    state.quote = SAMPLE_QUOTE
    state.financials = WEAK_FINANCIALS
    state.governance_data = SAMPLE_GOVERNANCE
    state.company_name = "WeakCo Ltd"
    return state


@pytest.fixture
def mock_claude() -> AsyncMock:
    """Mock anthropic.AsyncAnthropic client."""
    client = AsyncMock(spec=anthropic.AsyncAnthropic)
    return client


@pytest.fixture
def mock_nse_client() -> AsyncMock:
    """Mock NSEClient."""
    return AsyncMock(spec=NSEClient)


@pytest.fixture
def respx_mock():
    """HTTP mocking context via respx."""
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def mock_clients(mock_nse_client):
    """Dict of mocked API clients."""
    from unittest.mock import AsyncMock
    return {
        "nse": mock_nse_client,
        "screener": AsyncMock(),
        "bse": AsyncMock(),
        "trendlyne": AsyncMock(),
    }
