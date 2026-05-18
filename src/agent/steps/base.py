"""Abstract base class for all pipeline steps."""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.agent.tools import TOOLS, execute_tool
from src.config import settings
from src.logging_config import get_logger
from src.models import AnalysisState


def _is_retryable_anthropic_error(exc: BaseException) -> bool:
    """Return True for transient Anthropic API errors worth retrying."""
    return isinstance(exc, (anthropic.RateLimitError, anthropic.APIStatusError))


class BaseStep(ABC):
    """Abstract base class for all 9 analysis steps."""

    def __init__(
        self, anthropic_client: anthropic.AsyncAnthropic, clients: dict
    ) -> None:
        self.claude = anthropic_client
        self.clients = clients
        self.log = get_logger(self.__class__.__name__)
        # Set to True by _agentic_loop when max_iterations is exhausted without end_turn.
        # Callers can check this flag to add ER-06 error tags.
        self._last_loop_hit_max: bool = False

    @property
    @abstractmethod
    def step_number(self) -> int:
        """Step number (0–9)."""
        ...

    @property
    @abstractmethod
    def step_name(self) -> str:
        """Human-readable step name."""
        ...

    @abstractmethod
    async def run(self, state: AnalysisState) -> AnalysisState:
        """Execute this step and return the updated state."""
        ...

    # ------------------------------------------------------------------
    # Claude helpers
    # ------------------------------------------------------------------

    async def _call_claude(
        self,
        system: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Call Claude with an optional tool list. Returns first text block.

        Uses prompt caching (ephemeral) on the system prompt.

        Args:
            model: Override model; defaults to settings.model_heavy.
            max_tokens: Override token limit; defaults to settings.max_tokens.
        """
        _model = model if model is not None else settings.model_heavy
        _max_tokens = max_tokens if max_tokens is not None else settings.max_tokens
        start = time.monotonic()

        kwargs: dict[str, Any] = dict(
            model=_model,
            max_tokens=_max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        @retry(
            retry=retry_if_exception_type(_is_retryable_anthropic_error),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=5, max=60),
            reraise=True,
        )
        async def _create() -> Any:
            return await self.claude.messages.create(**kwargs)

        response = await _create()
        elapsed = time.monotonic() - start

        self.log.info(
            "claude_api_call",
            step=self.step_number,
            step_name=self.step_name,
            model=_model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            elapsed_seconds=round(elapsed, 2),
        )

        for block in response.content:
            if hasattr(block, "text"):
                return block.text  # type: ignore[return-value]
        return ""

    async def _agentic_loop(
        self,
        system: str,
        initial_message: str,
        tools: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_iterations: int = 12,
    ) -> str:
        """Run an agentic Claude loop that uses tools until stop_reason is end_turn.

        Args:
            system: System prompt string (cached via ephemeral cache_control).
            initial_message: First user message.
            tools: Tool schemas to expose. Defaults to TOOLS.
            model: Override model; defaults to settings.model_heavy.
            max_tokens: Override token limit; defaults to settings.max_tokens.
            max_iterations: Hard cap on tool-use iterations (per-step tuning).

        Returns:
            Final text response from Claude.
        """
        _model = model if model is not None else settings.model_heavy
        _max_tokens = max_tokens if max_tokens is not None else settings.max_tokens
        active_tools = tools if tools is not None else TOOLS
        messages: list[dict] = [{"role": "user", "content": initial_message}]
        total_input_tokens = 0
        total_output_tokens = 0
        iteration = 0
        self._last_loop_hit_max = False

        @retry(
            retry=retry_if_exception_type(_is_retryable_anthropic_error),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=5, max=60),
            reraise=True,
        )
        async def _loop_create(**kw: Any) -> Any:
            return await self.claude.messages.create(**kw)

        while iteration < max_iterations:
            iteration += 1
            start = time.monotonic()

            response = await _loop_create(
                model=_model,
                max_tokens=_max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
                tools=active_tools,
            )

            elapsed = time.monotonic() - start
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            self.log.info(
                "claude_agentic_step",
                step=self.step_number,
                step_name=self.step_name,
                model=_model,
                iteration=iteration,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                elapsed_seconds=round(elapsed, 2),
            )

            # End of agentic loop
            if response.stop_reason == "end_turn":
                break

            # Collect tool use blocks
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                # No tool calls but stop_reason isn't end_turn — treat as done
                break

            # Execute each tool call
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                result_content = await execute_tool(
                    block.name, block.input, self.clients
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                    }
                )

            # Append assistant turn and tool results
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            # If we just used the last iteration on a tool call, force one final
            # synthesis turn without tools so we always get a text JSON response.
            if iteration >= max_iterations:
                self._last_loop_hit_max = True
                self.log.warning(
                    "agentic_loop_max_iterations_hit",
                    step=self.step_number,
                    step_name=self.step_name,
                    iteration=iteration,
                    error_tag="ER-06",
                )
                start = time.monotonic()
                response = await _loop_create(
                    model=_model,
                    max_tokens=_max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": system,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages + [
                        {
                            "role": "user",
                            "content": (
                                "You have reached the research iteration limit. "
                                "Based on everything you have found so far, "
                                "respond NOW with only the required JSON object. "
                                "Do not call any more tools."
                            ),
                        }
                    ],
                )
                elapsed = time.monotonic() - start
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                self.log.info(
                    "claude_agentic_step",
                    step=self.step_number,
                    step_name=self.step_name,
                    model=_model,
                    iteration=iteration + 1,
                    stop_reason=response.stop_reason,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    elapsed_seconds=round(elapsed, 2),
                )
                break

        self.log.info(
            "claude_api_call",
            step=self.step_number,
            step_name=self.step_name,
            model=_model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            iterations=iteration,
        )

        # Extract text from the last response
        for block in response.content:  # type: ignore[union-attr]
            if hasattr(block, "text"):
                return block.text  # type: ignore[return-value]
        return ""

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        """Robustly extract the first JSON object from a Claude response.

        Handles:
        - Markdown code fences (```json ... ```)
        - Trailing explanatory text after the closing }
        - Extra whitespace / newlines
        """
        # Strip all markdown fences first
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
        # Find the first { and use raw_decode so trailing text is ignored
        start = cleaned.find("{")
        if start == -1:
            raise ValueError(f"No JSON object in response: {text[:120]!r}")
        obj, _ = json.JSONDecoder().raw_decode(cleaned, start)
        return obj
