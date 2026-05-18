"""Step 6 — Technical Entry Confirmation (deterministic)."""
from __future__ import annotations

from typing import Optional

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, TechnicalData, TechnicalSignal


class Step6Technical(BaseStep):
    """Technical entry signal — 4 signals, fully deterministic."""

    step_number = 6
    step_name = "Technical Entry Confirmation"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Score 4 technical signals and compute tranche entry prices."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        td = state.technical_data
        q = state.quote
        data_flags: list[str] = []

        # Derive TechnicalData from StockQuote if not pre-populated
        if td is None and q is not None:
            td = self._derive_technical_data(q)
            state.technical_data = td

        if td is None:
            # No data available
            result = TechnicalSignal(
                signals_met=0,
                signal_details={},
                entry_guidance="RED",
                data_flags=["[DATA UNVERIFIED: technical_data]"],
            )
            state.technical = result
            state.add_flag("[DATA UNVERIFIED: technical_data]")
            self.log.warning(
                "technical_data_unavailable",
                ticker=state.ticker,
            )
            return state

        signal_details: dict[str, bool] = {}

        # Signal 1: Within 15% of 52W low
        s1 = td.pct_from_52w_low <= 15.0
        signal_details["within_15pct_52w_low"] = s1

        # Signal 2: RSI < 40
        if td.rsi_14 is not None:
            s2 = td.rsi_14 < 40.0
            signal_details["rsi_below_40"] = s2
        else:
            s2 = False
            signal_details["rsi_below_40"] = False
            data_flags.append("[DATA UNVERIFIED: rsi_14]")

        # Signal 3: Price <= 200-DMA
        if td.dma_200 is not None:
            s3 = td.cmp <= td.dma_200
            signal_details["price_at_or_below_200dma"] = s3
        else:
            s3 = False
            signal_details["price_at_or_below_200dma"] = False
            data_flags.append("[DATA UNVERIFIED: dma_200]")

        # Signal 4: Volume declining on down-days
        if td.volume_trend_down_days is not None:
            s4 = td.volume_trend_down_days.lower() == "declining"
            signal_details["volume_declining_on_down_days"] = s4
        else:
            s4 = False
            signal_details["volume_declining_on_down_days"] = False
            data_flags.append("[DATA UNVERIFIED: volume_trend_down_days]")

        # Signal 5: Price >= 20% below 52W high (meaningful pullback from peak)
        # Buying near the 52W high is a timing risk even if fundamentals are strong.
        pct_from_high = (
            round((td.w52_high - td.cmp) / td.w52_high * 100, 2)
            if td.w52_high and td.w52_high > 0 else 0.0
        )
        s5 = pct_from_high >= 20.0
        signal_details["price_ge_20pct_below_52w_high"] = s5

        signals_met = sum([s1, s2, s3, s4, s5])

        # Entry guidance (scaled to 5 signals)
        if signals_met >= 3:
            entry_guidance = "GREEN"
        elif signals_met >= 1:
            entry_guidance = "AMBER"
        else:
            entry_guidance = "RED"

        # Tranche prices
        cmp = td.cmp
        t1 = round(cmp, 2)
        t2 = round(cmp * (1 - settings.tranche_t2_discount), 2)
        t3 = round(cmp * (1 - settings.tranche_t3_discount), 2)

        result = TechnicalSignal(
            signals_met=signals_met,
            signal_details=signal_details,
            entry_guidance=entry_guidance,
            tranche_1_price=t1,
            tranche_2_price=t2,
            tranche_3_price=t3,
            data_flags=data_flags,
        )
        state.technical = result

        for flag in data_flags:
            state.add_flag(flag)

        self.log.info(
            "technical_assessed",
            ticker=state.ticker,
            signals_met=signals_met,
            entry_guidance=entry_guidance,
            tranche_1=t1,
            tranche_2=t2,
            tranche_3=t3,
            signal_details=signal_details,
        )
        return state

    def _derive_technical_data(self, q) -> TechnicalData:
        """Derive TechnicalData from a StockQuote, including volume trend (P3-4)."""
        cmp = q.cmp
        low = q.w52_low
        pct_from_low = (
            round((cmp - low) / low * 100, 2) if low and low > 0 else 0.0
        )
        return TechnicalData(
            cmp=cmp,
            w52_high=q.w52_high,
            w52_low=low,
            pct_from_52w_low=pct_from_low,
            dma_200=q.dma_200,
            # P3-4: volume trend carried from StockQuote (populated by YFinanceClient)
            volume_trend_down_days=getattr(q, "volume_trend_down_days", None),
        )
