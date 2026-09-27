"""Tests for Step 6 — technical entry confirmation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agent.steps.step6_technical import Step6Technical
from src.agent.steps.step9_output import Step9Output
from src.models import AnalysisState, StockQuote


def _state(
    cmp: float = 1000.0,
    w52_high: float = 1200.0,
    w52_low: float = 900.0,
    dma_200: float | None = 1050.0,
    rsi_14: float | None = 35.0,
    volume: str | None = "declining",
    sector: str | None = None,
) -> AnalysisState:
    state = AnalysisState(ticker="TESTCO", sector_name=sector)
    state.quote = StockQuote(
        ticker="TESTCO",
        company_name="Test Co",
        cmp=cmp,
        w52_high=w52_high,
        w52_low=w52_low,
        dma_200=dma_200,
        market_cap_cr=10_000.0,
        rsi_14=rsi_14,
        volume_trend_down_days=volume,
    )
    return state


async def _run(state: AnalysisState) -> AnalysisState:
    return await Step6Technical(MagicMock(), {}).run(state)


# Extended stock: far above the 52W low, near the high, above the 200-DMA,
# RSI hot, heavy volume on down days → 0/5 signals.
_EXTENDED = dict(
    cmp=1000.0, w52_high=1010.0, w52_low=700.0, rsi_14=65.0, volume="increasing"
)


@pytest.mark.asyncio
async def test_rsi_from_quote_counts_as_signal():
    state = await _run(_state(rsi_14=32.0))
    assert state.technical.signal_details["rsi_below_40"] is True
    assert "[DATA UNVERIFIED: rsi_14]" not in state.all_data_flags


@pytest.mark.asyncio
async def test_zero_52w_range_is_unverified_not_auto_pass():
    """A missing range (0/0) used to make 'within 15% of 52W low' always true."""
    state = await _run(_state(w52_high=0.0, w52_low=0.0))
    details = state.technical.signal_details
    assert details["within_15pct_52w_low"] is False
    assert details["price_ge_20pct_below_52w_high"] is False
    assert "[DATA UNVERIFIED: w52_low]" in state.all_data_flags
    assert "[DATA UNVERIFIED: w52_high]" in state.all_data_flags


@pytest.mark.asyncio
async def test_green_setup_enters_t1_at_cmp_with_default_discounts():
    state = await _run(_state())
    t = state.technical
    assert t.entry_guidance == "GREEN"
    assert t.entry_deferred is False
    assert t.tranche_1_price == 1000.0
    assert t.tranche_2_price == 920.0
    assert t.tranche_3_price == 850.0


@pytest.mark.asyncio
async def test_cyclical_sector_uses_wider_tranche_discounts():
    state = await _run(_state(sector="commodities_cyclical"))
    t = state.technical
    assert t.tranche_2_price == 880.0   # 12%
    assert t.tranche_3_price == 780.0   # 22%


@pytest.mark.asyncio
async def test_red_defers_t1_to_200dma_pullback():
    state = await _run(_state(dma_200=950.0, **_EXTENDED))
    t = state.technical
    assert t.entry_guidance == "RED"
    assert t.entry_deferred is True
    assert t.tranche_1_price == 950.0
    assert any(f.startswith("[ENTRY DEFERRED") for f in state.all_data_flags)


@pytest.mark.asyncio
async def test_red_t1_never_deeper_than_t2():
    """200-DMA far below CMP → T1 waits only as far as the T2 level."""
    state = await _run(_state(dma_200=800.0, **_EXTENDED))
    assert state.technical.tranche_1_price == state.technical.tranche_2_price == 920.0


@pytest.mark.asyncio
async def test_red_without_dma_waits_at_t2():
    state = await _run(_state(dma_200=None, **_EXTENDED))
    assert state.technical.entry_deferred is True
    assert state.technical.tranche_1_price == 920.0


@pytest.mark.asyncio
async def test_step9_tranche_condition_reflects_deferral():
    state = await _run(_state(dma_200=950.0, **_EXTENDED))
    state.suggested_allocation_pct = 5.0
    Step9Output(MagicMock(), {})._build_tranches(state)
    t1 = state.tranches[0]
    assert t1.price == 950.0
    assert t1.condition.startswith("Wait")
    # T2/T3 are still measured from CMP, not from the deferred T1
    assert state.tranches[1].price == 920.0


@pytest.mark.asyncio
async def test_step9_green_condition_unchanged():
    state = await _run(_state())
    state.suggested_allocation_pct = 5.0
    Step9Output(MagicMock(), {})._build_tranches(state)
    assert state.tranches[0].condition == "Enter now at CMP"
