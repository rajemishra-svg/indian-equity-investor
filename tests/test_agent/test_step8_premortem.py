"""Prompt-contract tests for Step 8 — Premortem Risk Analysis.

Step 8 is a single Haiku call, not deterministic logic — there's nothing to
unit-test in the traditional sense. What's tested here is the calibration
guidance added to curb over-classifying manageable risks as
STRUCTURAL_UNHEDGEABLE (see the module docstring / CLAUDE.md session notes):
a backtest across this session's stored analyses found the rejected group
outperformed the passed group, and a framing-effect was found where a
concretely-named threat (competitor, technology) got classified as
structural while an abstractly-described one of comparable severity didn't.
These tests just lock in that the guidance text survives future edits.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.agent.steps.step8_premortem import Step8Premortem
from src.models import AnalysisState


def _step() -> Step8Premortem:
    return Step8Premortem(MagicMock(), {})


def test_calibration_guidance_present_in_value_mode():
    prompt = _step()._build_system_prompt(None)
    assert "CALIBRATION" in prompt
    assert "should be RARE" in prompt
    assert "even if governance, financials, and valuation were all excellent" in prompt


def test_calibration_guidance_present_in_growth_mode():
    state = AnalysisState(ticker="TEST")
    state.analysis_mode = "growth"
    prompt = _step()._build_system_prompt(state)
    assert "CALIBRATION" in prompt
    assert "GROWTH MODE" in prompt


def test_calibration_warns_against_narrative_vividness_bias():
    """A named competitor/technology must not be treated as automatically more
    severe than an abstract risk of comparable underlying exposure — this is
    the specific bias a real SARDAEN-vs-TRENT comparison surfaced."""
    prompt = _step()._build_system_prompt(None)
    assert "NOT automatically more severe" in prompt


def test_calibration_precedes_categories_and_rules():
    """Ordering matters for a single-pass read: calibration must land before
    the category checklist and JSON rules, not after."""
    prompt = _step()._build_system_prompt(None)
    calibration_idx = prompt.index("CALIBRATION")
    categories_idx = prompt.index("Categories to evaluate")
    rules_idx = prompt.index("RULES:")
    assert calibration_idx < categories_idx < rules_idx
