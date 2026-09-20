"""Shared test fixtures."""
from __future__ import annotations

from unittest.mock import AsyncMock

import anthropic
import pytest
import respx

from src.api.nse import NSEClient
from src.config import settings
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


@pytest.fixture(autouse=True)
def _isolated_db_path(tmp_path, monkeypatch):
    """Force every test onto an isolated SQLite file — never the real investor.db.

    Several code paths (batch_scanner's save_snapshot/get_fresh_snapshot,
    the pipeline's post-analysis persistence) read ``settings.db_path``
    directly rather than taking an explicit path parameter. A test that
    mocks the HTTP/Claude clients but not persistence would silently write
    mock fixtures (SAMPLE_QUOTE, BADCO, WEAKCO — see tests/fixtures/sample_data.py)
    into the project's real investor.db on every ``uv run pytest``.

    This is what actually happened: the RELIANCE quote in investor.db was
    frozen at SAMPLE_QUOTE's values (cmp=2850.50, data_timestamp=2026-05-15)
    for months, silently overwriting real analyses each time the suite ran,
    and BADCO/WEAKCO — fixture-only tickers that don't exist on NSE — ended
    up as full rows in a production portfolio-tracking database.

    Autouse + tmp_path (unique per test) closes this at the root: no
    individual test has to remember to patch persistence for isolation to
    hold. Tests that pass their own explicit ``db_path`` (e.g. via a
    tmp_path-backed fixture) are unaffected — this only redirects the
    ``settings.db_path`` singleton that untouched code falls back to.
    """
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "isolated_test.db"))


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
