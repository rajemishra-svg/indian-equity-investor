"""Per-run LLM cost accumulator for the investor pipeline.

Mirrors the pattern used in the tradedesk pipeline/costs.py.
Each call to record_usage() accumulates token counts and USD cost
into a module-level dict that is reset at the start of every pipeline
run and snapshotted for DB persistence at the end.
"""
from __future__ import annotations

from typing import Any

from src.config import settings

# USD per million tokens: (input, output).
# Cache reads bill at 0.1× input; cache writes at 1.25× input.
_PRICING: dict[str, tuple[float, float]] = {
    settings.model_heavy: (3.0, 15.0),   # Sonnet
    settings.model_light: (1.0, 5.0),    # Haiku
}
_DEFAULT_PRICING = (3.0, 15.0)

_run_usage: dict = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_usd": 0.0,
    "cost_usd_sonnet": 0.0,
    "cost_usd_haiku": 0.0,
    "by_step": {},
}


def record_usage(model: str, usage: Any, step_name: str | None = None) -> None:
    """Accumulate one API call's token usage into the run totals."""
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    in_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
    cost = (
        in_tok * in_price
        + cache_read * in_price * 0.1
        + cache_write * in_price * 1.25
        + out_tok * out_price
    ) / 1_000_000

    _run_usage["input_tokens"] += in_tok
    _run_usage["output_tokens"] += out_tok
    _run_usage["cache_read_tokens"] += cache_read
    _run_usage["cache_write_tokens"] += cache_write
    _run_usage["cost_usd"] = round(_run_usage["cost_usd"] + cost, 6)

    if model == settings.model_heavy:
        _run_usage["cost_usd_sonnet"] = round(_run_usage["cost_usd_sonnet"] + cost, 6)
    elif model == settings.model_light:
        _run_usage["cost_usd_haiku"] = round(_run_usage["cost_usd_haiku"] + cost, 6)

    if step_name:
        slot = _run_usage["by_step"].setdefault(
            step_name,
            {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        )
        slot["input_tokens"] += in_tok + cache_read + cache_write
        slot["output_tokens"] += out_tok
        slot["cost_usd"] = round(slot["cost_usd"] + cost, 6)


def reset_run_usage() -> None:
    """Reset all accumulators — call at the start of each pipeline run."""
    _run_usage.update({
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
        "cost_usd_sonnet": 0.0,
        "cost_usd_haiku": 0.0,
        "by_step": {},
    })


def run_usage() -> dict:
    """Return a snapshot of the current run's accumulated usage."""
    snap = dict(_run_usage)
    snap["by_step"] = {k: dict(v) for k, v in _run_usage["by_step"].items()}
    return snap
