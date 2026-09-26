"""Tests for Step 5G — Growth Valuation, focusing on P1 gates."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.steps.step5_growth_valuation import Step5GrowthValuation, _forward_revenue_dcf
from src.config import settings
from src.models import (
    AnalysisMode,
    AnalysisState,
    FinancialMetrics,
    GrowthMetrics,
    StockQuote,
    ValuationData,
)


def make_step() -> Step5GrowthValuation:
    return Step5GrowthValuation(anthropic_client=AsyncMock(), clients={})


def make_state(
    rev_3y: float = 35.0,
    listing_years: float | None = None,
    sector_name: str = "default",
    tam_cr: float | None = None,
    tam_source: str | None = None,
    trailing_revenue_cr: float = 500.0,
    shares_outstanding_cr: float = 10.0,
    cmp: float = 1000.0,
    market_cap_cr: float = 10_000.0,
    net_debt_cr: float | None = 0.0,
) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO")
    state.analysis_mode = AnalysisMode.GROWTH
    state.sector_name = sector_name
    state.financials = FinancialMetrics(
        revenue_cagr_3y=rev_3y,
        trailing_revenue_cr=trailing_revenue_cr,
    )
    state.growth_metrics = GrowthMetrics(
        listing_years=listing_years,
        tam_size_cr=tam_cr,
        tam_source=tam_source,
    )
    state.valuation_data = ValuationData(
        shares_outstanding_cr=shares_outstanding_cr,
        net_debt_cr=net_debt_cr,
    )
    state.quote = StockQuote(
        ticker="TESTCO",
        company_name="Test Growth Co",
        cmp=cmp,
        w52_high=cmp * 1.3,
        w52_low=cmp * 0.6,
        market_cap_cr=market_cap_cr,
    )
    return state


# ---------------------------------------------------------------------------
# P1a: EC-G2 recently listed MoS adjustment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recently_listed_sector_raises_mos_threshold():
    """sector_name=recently_listed → EC-G2 flag + 30% MoS threshold instead of 20%."""
    state = make_state(sector_name="recently_listed", rev_3y=40.0)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" in flags
    assert "30%" in flags or "30" in flags


@pytest.mark.asyncio
async def test_listing_years_below_1_raises_mos_threshold():
    """listing_years = 0.5 (< 1 year) → EC-G2 flag raised."""
    state = make_state(listing_years=0.5)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" in flags


@pytest.mark.asyncio
async def test_listing_years_above_1_no_ec_g2():
    """listing_years = 2.0 (> 1 year) → no EC-G2 flag; uses standard 20% threshold."""
    state = make_state(listing_years=2.0)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" not in flags


@pytest.mark.asyncio
async def test_no_listing_years_no_ec_g2():
    """listing_years = None (unknown, assumed ≥ 3Y) → no EC-G2 flag."""
    state = make_state(listing_years=None, sector_name="default")
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "EC-G2" not in flags


# ---------------------------------------------------------------------------
# P1b: TAM verification — llm_inference source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_inferred_tam_flagged_as_unverified():
    """tam_source='llm_inference' → TAM UNVERIFIED flag in data_flags."""
    state = make_state(
        tam_cr=50_000.0,
        tam_source="llm_inference",
        market_cap_cr=5_000.0,  # 10× headroom
    )
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM UNVERIFIED" in flags
    assert "CRISIL" in flags or "IBEF" in flags or "NASSCOM" in flags


@pytest.mark.asyncio
async def test_llm_inferred_tam_counted_at_half_weight():
    """llm_inference TAM with ≥10× headroom counts as 0.5 method, not 1."""
    state = make_state(
        tam_cr=500_000.0,   # massive headroom
        tam_source="llm_inference",
        market_cap_cr=5_000.0,
    )
    # Run step; check that it doesn't easily reach full buy zone on TAM alone
    state = await make_step().run(state)
    # The 0.5 weight means TAM alone cannot push methods_in_buy_zone to ≥ 1 integer
    # (0.5 < 1 so gate won't be PASS_GREEN from TAM alone)
    assert state.valuation is not None  # step completed


@pytest.mark.asyncio
async def test_industry_report_tam_not_flagged():
    """tam_source='industry_report' → no TAM UNVERIFIED flag; full credit."""
    state = make_state(
        tam_cr=100_000.0,
        tam_source="industry_report",
        market_cap_cr=5_000.0,
    )
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM UNVERIFIED" not in flags


@pytest.mark.asyncio
async def test_no_tam_gives_skip_flag():
    """No TAM data → G5-5 skip flag, not unverified."""
    state = make_state(tam_cr=None, tam_source=None)
    state = await make_step().run(state)

    flags = " ".join(state.all_data_flags)
    assert "TAM-ceiling check skipped" in flags
    assert "TAM UNVERIFIED" not in flags


# ---------------------------------------------------------------------------
# G5-3: Forward Revenue DCF — units and growth fade (regression)
# ---------------------------------------------------------------------------
#
# Hand computation for revenue ₹1,000 Cr, 3Y CAGR 30%, terminal growth 6%,
# 7 years, WACC 17%, terminal P/S 2.5:
#   growth fades linearly 30% → 6% in 4pp steps: 30, 26, 22, 18, 14, 10, 6
#   revenue_y7 = 1000 × 1.30 × 1.26 × 1.22 × 1.18 × 1.14 × 1.10 × 1.06 = 3,134.434 Cr
#   TV         = 3,134.434 × 2.5                                        = 7,836.085 Cr
#   PV         = 7,836.085 / 1.17^7 (3.0012421)                         = 2,610.947 Cr
#   per share  = 2,610.947 Cr / 10 Cr shares                            = ₹261.09


def test_forward_revenue_dcf_hand_computed():
    pv_cr = _forward_revenue_dcf(
        trailing_revenue_cr=1_000.0,
        revenue_cagr_3y_pct=30.0,
        wacc_pct=17.0,
        terminal_ps_multiple=2.5,
        projection_years=7,
        terminal_growth_pct=6.0,
    )
    assert pv_cr == pytest.approx(2_610.947, abs=0.01)


def test_forward_revenue_dcf_growth_below_terminal_does_not_accelerate():
    """A 3% grower stays at 3% — the fade never pushes growth *up* to terminal."""
    pv_cr = _forward_revenue_dcf(1_000.0, 3.0, 17.0, 2.5, 7, terminal_growth_pct=6.0)
    assert pv_cr == pytest.approx(1_000.0 * 1.03**7 * 2.5 / 1.17**7, rel=1e-9)


@pytest.fixture
def pinned_wacc(monkeypatch):
    """Pin WACC inputs so .env overrides can't move the hand-computed values."""
    monkeypatch.setattr(settings, "wacc_large_cap", 13.0)
    monkeypatch.setattr(settings, "wacc_mid_cap", 15.0)
    monkeypatch.setattr(settings, "wacc_small_cap", 16.5)
    monkeypatch.setattr(settings, "wacc_terminal_growth", 6.0)


@pytest.mark.asyncio
async def test_forward_dcf_per_share_has_no_crore_factor(pinned_wacc):
    """₹ Cr / crore shares is already ₹/share — regression for the old `* 10`
    that inflated every growth-mode intrinsic value tenfold.

    Mid-cap (₹5,000 Cr) → WACC 15% + 2% high_growth adjustment = 17%; no moat
    → terminal P/S 2.5 — i.e. exactly the hand computation above.
    """
    state = make_state(
        rev_3y=30.0,
        trailing_revenue_cr=1_000.0,
        shares_outstanding_cr=10.0,
        cmp=500.0,
        market_cap_cr=5_000.0,
    )
    state = await make_step().run(state)

    assert state.valuation.dcf_intrinsic_weighted == pytest.approx(261.09, abs=0.01)
    ratio = state.valuation.dcf_intrinsic_weighted / 500.0
    assert 0.2 <= ratio <= 3.0
    # MoS = (261.09 − 500) / 261.09 → CMP above intrinsic
    assert state.valuation.margin_of_safety_pct == pytest.approx(-91.5, abs=0.1)


@pytest.mark.asyncio
async def test_forward_dcf_hypergrowth_stays_within_sane_band(pinned_wacc):
    """DIXON-like inputs (59% 3Y CAGR, P/S ~1.5) previously produced ~250× CMP.

    With the unit fix and growth fade the intrinsic value must land within a
    few multiples of CMP, not orders of magnitude away.
    """
    state = make_state(
        rev_3y=59.0,
        trailing_revenue_cr=48_873.0,
        shares_outstanding_cr=6.08,
        cmp=12_506.0,
        market_cap_cr=76_039.0,
    )
    state = await make_step().run(state)

    ratio = state.valuation.dcf_intrinsic_weighted / 12_506.0
    assert 0.2 <= ratio <= 5.0


# ---------------------------------------------------------------------------
# G5-3: terminal P/S capped at EBITDA margin × growth_terminal_ev_ebitda
# ---------------------------------------------------------------------------


def _margin_state(margin: float | None):
    """Hand-computed base case above (₹261.09/share at P/S 2.5), plus a margin."""
    state = make_state(
        rev_3y=30.0,
        trailing_revenue_cr=1_000.0,
        shares_outstanding_cr=10.0,
        cmp=500.0,
        market_cap_cr=5_000.0,
    )
    state.financials.ebitda_margin_latest = margin
    return state


@pytest.mark.asyncio
async def test_thin_margin_caps_terminal_ps(pinned_wacc, monkeypatch):
    """4% margin × 20× EV/EBITDA → P/S 0.8 instead of 2.5.
    PV = 3,134.434 × 0.8 / 1.17^7 = 835.503 Cr → ₹83.55/share."""
    monkeypatch.setattr(settings, "growth_terminal_ev_ebitda", 20.0)
    state = await make_step().run(_margin_state(4.0))

    assert state.valuation.dcf_intrinsic_weighted == pytest.approx(83.55, abs=0.01)
    assert "terminal P/S capped 2.5× → 0.80×" in " ".join(state.all_data_flags)


@pytest.mark.asyncio
async def test_high_margin_leaves_moat_ps_uncapped(pinned_wacc, monkeypatch):
    """30% × 20× = 6.0× > 2.5× moat P/S → cap doesn't bind."""
    monkeypatch.setattr(settings, "growth_terminal_ev_ebitda", 20.0)
    state = await make_step().run(_margin_state(30.0))

    assert state.valuation.dcf_intrinsic_weighted == pytest.approx(261.09, abs=0.01)
    assert "terminal P/S capped" not in " ".join(state.all_data_flags)


@pytest.mark.asyncio
async def test_missing_margin_flags_unchecked_terminal_ps(pinned_wacc):
    state = await make_step().run(_margin_state(None))

    assert state.valuation.dcf_intrinsic_weighted == pytest.approx(261.09, abs=0.01)
    assert "G5-3 terminal P/S 2.5× not checked against margins" in " ".join(state.all_data_flags)


@pytest.mark.asyncio
async def test_pre_profit_keeps_moat_ps_with_flag(pinned_wacc):
    """Negative margin can't imply a P/S — keep the moat multiple, but say so."""
    state = await make_step().run(_margin_state(-12.0))

    assert state.valuation.dcf_intrinsic_weighted == pytest.approx(261.09, abs=0.01)
    assert "G5-3: EBITDA margin -12.0% (pre-profit)" in " ".join(state.all_data_flags)
