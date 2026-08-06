"""Tests for BaseStep's Claude retry wiring.

Regression coverage for a bug where both `_call_claude` and `_agentic_loop`
passed a predicate *function* to tenacity's `retry_if_exception_type`, which
requires an exception *type* (or tuple of types) and internally does
`isinstance(exc, exception_types)`. Passing a function there doesn't raise at
decoration time — it only blows up the first time an exception actually occurs
inside the decorated call, with `TypeError: isinstance() arg 2 must be a type,
a tuple of types, or a union`. That masked the real error and caused Step 1
governance enrichment / capital allocation scoring (and anything else calling
`_call_claude` or `_agentic_loop`) to silently degrade instead of retrying.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest

from src.agent.steps.base import BaseStep, _is_retryable_anthropic_error
from src.models import AnalysisState


class _ConcreteStep(BaseStep):
    step_number = 1
    step_name = "Test Step"

    async def run(self, state: AnalysisState) -> AnalysisState:
        return state


def _text_response(text: str = "ok") -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    block.__class__ = type("TextBlock", (), {})
    # hasattr(block, "text") must be True; MagicMock already satisfies that.
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _rate_limit_error() -> anthropic.RateLimitError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _bad_request_error() -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.APIStatusError("bad request", response=response, body=None)


# ---------------------------------------------------------------------------
# _is_retryable_anthropic_error — pure predicate
# ---------------------------------------------------------------------------


def test_rate_limit_error_is_retryable():
    assert _is_retryable_anthropic_error(_rate_limit_error()) is True


def test_bad_request_error_is_not_retryable():
    assert _is_retryable_anthropic_error(_bad_request_error()) is False


def test_generic_exception_is_not_retryable():
    assert _is_retryable_anthropic_error(ValueError("boom")) is False


# ---------------------------------------------------------------------------
# _call_claude — end-to-end retry wiring (regression for the isinstance bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_claude_retries_on_rate_limit_then_succeeds():
    """A retryable error on the first attempt must not crash the retry
    decorator itself — it should retry and return the eventual success."""
    claude = AsyncMock()
    claude.messages.create = AsyncMock(
        side_effect=[_rate_limit_error(), _text_response("recovered")]
    )
    step = _ConcreteStep(anthropic_client=claude, clients={})

    result = await step._call_claude(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert result == "recovered"
    assert claude.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_call_claude_raises_immediately_on_non_retryable_error():
    """A non-retryable error must propagate as itself — not as the tenacity
    isinstance() TypeError the old retry_if_exception_type(predicate) bug
    produced, and without wasting retries on a permanent failure."""
    claude = AsyncMock()
    claude.messages.create = AsyncMock(side_effect=_bad_request_error())
    step = _ConcreteStep(anthropic_client=claude, clients={})

    with pytest.raises(anthropic.APIStatusError):
        await step._call_claude(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert claude.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_agentic_loop_retries_on_rate_limit_then_succeeds():
    claude = AsyncMock()
    claude.messages.create = AsyncMock(
        side_effect=[_rate_limit_error(), _text_response("recovered")]
    )
    step = _ConcreteStep(anthropic_client=claude, clients={})

    result = await step._agentic_loop(system="sys", initial_message="hi", tools=[])

    assert result == "recovered"
    assert claude.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_agentic_loop_raises_immediately_on_non_retryable_error():
    claude = AsyncMock()
    claude.messages.create = AsyncMock(side_effect=_bad_request_error())
    step = _ConcreteStep(anthropic_client=claude, clients={})

    with pytest.raises(anthropic.APIStatusError):
        await step._agentic_loop(system="sys", initial_message="hi", tools=[])

    assert claude.messages.create.await_count == 1
