"""Step 5 — Valuation & Margin of Safety (deterministic + Claude DCF)."""
from __future__ import annotations

import json
from typing import Optional

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import AnalysisState, GateResult, ValuationData, ValuationResult
from src.sector.profiles import get_sector_profile


# Verdict bands for each method
_PE_PERCENTILE_BANDS = [
    (30, "EXCELLENT"),
    (60, "FAIR"),
    (80, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

_PEG_BANDS = [
    (1.0, "EXCELLENT"),
    (1.3, "FAIR"),
    (1.7, "EXPENSIVE"),
    (float("inf"), "AVOID"),
]

_FCF_YIELD_BANDS = [
    (3.0, "EXPENSIVE"),  # below 3% → EXPENSIVE (inverted)
    (5.0, "FAIR"),
    (float("inf"), "ATTRACTIVE"),
]

# EV/EBITDA bands — lower is cheaper. Sector-agnostic defaults.
# Financial services and pre-profit companies skip this method.
_EV_EBITDA_BANDS = [
    (12, "EXCELLENT"),   # < 12x — historically attractive for quality Ind. companies
    (20, "FAIR"),        # 12–20x — reasonable
    (28, "EXPENSIVE"),   # 20–28x — stretched
    (float("inf"), "AVOID"),
]

BUY_ZONE_VERDICTS = {"EXCELLENT", "FAIR"}


def _pe_percentile_verdict(percentile: Optional[float]) -> str:
    if percentile is None:
        return "UNKNOWN"
    for threshold, label in _PE_PERCENTILE_BANDS:
        if percentile < threshold:
            return label
    return "AVOID"


def _peg_verdict(peg: Optional[float]) -> str:
    if peg is None:
        return "UNKNOWN"
    for threshold, label in _PEG_BANDS:
        if peg < threshold:
            return label
    return "AVOID"


def _fcf_yield_verdict(fcf_yield: Optional[float]) -> str:
    if fcf_yield is None:
        return "UNKNOWN"
    if fcf_yield < 3.0:
        return "EXPENSIVE"
    elif fcf_yield < 5.0:
        return "FAIR"
    else:
        return "ATTRACTIVE"


def _ev_ebitda_verdict(ev_ebitda: Optional[float]) -> str:
    if ev_ebitda is None:
        return "UNKNOWN"
    for threshold, label in _EV_EBITDA_BANDS:
        if ev_ebitda < threshold:
            return label
    return "AVOID"


def _compute_mos(cmp: float, intrinsic: Optional[float]) -> Optional[float]:
    if intrinsic is None or intrinsic <= 0:
        return None
    return round((intrinsic - cmp) / intrinsic * 100, 2)


class Step5Valuation(BaseStep):
    """Valuation gate — 4 methods, DCF computed by Claude, deterministic gate."""

    step_number = 5
    step_name = "Valuation & Margin of Safety"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Evaluate all valuation methods and determine gate."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        v = state.valuation_data
        f = state.financials
        q = state.quote
        data_flags: list[str] = []
        methods_in_buy_zone = 0

        cmp = q.cmp if q else 0.0

        # --- Method 1: Historical P/E percentile ---
        pe_pct_verdict = _pe_percentile_verdict(v.pe_10y_percentile if v else None)
        if v is None or v.pe_10y_percentile is None:
            data_flags.append("[DATA UNVERIFIED: pe_10y_percentile]")
        elif pe_pct_verdict in BUY_ZONE_VERDICTS:
            methods_in_buy_zone += 1

        # --- Method 2: PEG ratio ---
        peg_v = _peg_verdict(v.peg_ratio if v else None)
        if v is None or v.peg_ratio is None:
            data_flags.append("[DATA UNVERIFIED: peg_ratio]")
        elif peg_v in BUY_ZONE_VERDICTS:
            methods_in_buy_zone += 1

        # --- Method 3: DCF (Claude-computed) ---
        dcf_base: Optional[float] = None
        dcf_bull: Optional[float] = None
        dcf_bear: Optional[float] = None
        dcf_weighted: Optional[float] = None

        if f is not None and q is not None:
            try:
                dcf_base, dcf_bull, dcf_bear, dcf_weighted = await self._run_dcf(state)
            except Exception as exc:
                self.log.warning("dcf_failed", ticker=state.ticker, error=str(exc))
                data_flags.append("[DATA UNVERIFIED: dcf_intrinsic]")

        if dcf_weighted is not None and cmp > 0:
            mos = _compute_mos(cmp, dcf_weighted)
            if mos is not None and mos >= state.required_mos_pct:
                methods_in_buy_zone += 1
        else:
            mos = None

        # --- Method 4: FCF Yield ---
        fcf_yield_v = _fcf_yield_verdict(v.fcf_yield_pct if v else None)
        if v is None or v.fcf_yield_pct is None:
            data_flags.append("[DATA UNVERIFIED: fcf_yield]")
        elif fcf_yield_v in BUY_ZONE_VERDICTS or fcf_yield_v == "ATTRACTIVE":
            methods_in_buy_zone += 1

        # --- Method 5: EV/EBITDA (skipped when sector profile marks it inapplicable) ---
        profile = get_sector_profile(state.sector_name)
        if not profile.ev_ebitda_applicable:
            ev_ebitda_v = "N/A"
            data_flags.append(
                f"[SECTOR: EV/EBITDA method skipped — not applicable for {state.sector_name}]"
            )
        else:
            ev_ebitda_v = _ev_ebitda_verdict(v.ev_ebitda_current if v else None)
            if v is None or v.ev_ebitda_current is None:
                data_flags.append("[DATA UNVERIFIED: ev_ebitda]")
            elif ev_ebitda_v == "UNKNOWN":
                data_flags.append("[DATA UNVERIFIED: ev_ebitda]")
            else:
                if ev_ebitda_v in BUY_ZONE_VERDICTS:
                    methods_in_buy_zone += 1

        # MoS check
        mos_met = mos is not None and mos >= state.required_mos_pct

        # Gate — at least 2 of 5 methods in buy zone AND DCF MoS met
        if methods_in_buy_zone >= 2 and mos_met:
            gate = GateResult.PASS_GREEN
        elif methods_in_buy_zone >= 1:
            gate = GateResult.PASS_CONDITIONAL
        else:
            gate = GateResult.FAIL  # DO_NOT_BUY — add to watchlist Tier 2

        result = ValuationResult(
            gate=gate,
            dcf_intrinsic_base=dcf_base,
            dcf_intrinsic_bull=dcf_bull,
            dcf_intrinsic_bear=dcf_bear,
            dcf_intrinsic_weighted=dcf_weighted,
            margin_of_safety_pct=mos,
            required_mos_pct=state.required_mos_pct,
            mos_met=mos_met,
            methods_in_buy_zone=methods_in_buy_zone,
            max_methods=5,
            pe_percentile_verdict=pe_pct_verdict,
            peg_verdict=peg_v,
            fcf_yield_verdict=fcf_yield_v,
            ev_ebitda_verdict=ev_ebitda_v,
            data_flags=data_flags,
        )
        state.valuation = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            methods_in_buy_zone=methods_in_buy_zone,
            mos_pct=mos,
            required_mos_pct=state.required_mos_pct,
        )

        for flag in data_flags:
            state.add_flag(flag)

        # Valuation miss → Watchlist Tier 2, do NOT terminate pipeline beyond here
        if gate == GateResult.FAIL:
            state.recommendation_type = "WATCHLIST"
            state.watchlist_tier = 2  # type: ignore[assignment]
            state.termination_reason = (
                f"Valuation not in buy zone: {methods_in_buy_zone} methods in zone, "
                f"MoS {mos:.1f}% vs required {state.required_mos_pct:.1f}%"
                if mos is not None
                else "Valuation not in buy zone: insufficient data"
            )
            state.terminated_at_step = self.step_number
            self.log.info(
                "pipeline_watchlist",
                step=self.step_number,
                ticker=state.ticker,
                tier=2,
                reason=state.termination_reason,
            )

        return state

    async def _run_dcf(
        self, state: AnalysisState
    ) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """Ask Claude to compute a 3-scenario DCF and return intrinsic values."""
        f = state.financials
        q = state.quote
        v = state.valuation_data

        # WACC: risk-adjusted by cap size + sector profile adjustment
        cap_wacc = {"large_cap": 13.0, "mid_cap": 15.0, "small_cap": 16.5}.get(state.cap_size, 15.0)
        # Sector profile carries an explicit WACC adjustment (e.g. +1.0 for cyclicals/infra)
        sector_profile = get_sector_profile(state.sector_name)
        wacc = cap_wacc + sector_profile.wacc_adjustment
        # Terminal growth: conservative — India long-run nominal GDP ~10%, sustainable company share < 70%
        terminal_growth = 6.0

        context = {
            "ticker": state.ticker,
            "cap_size": state.cap_size,
            "sector_name": state.sector_name or "default",
            "sector": state.tailwind.sector if state.tailwind else "[NOT AVAILABLE]",
            "cmp": q.cmp if q else "[NOT AVAILABLE]",
            "market_cap_cr": q.market_cap_cr if q else "[NOT AVAILABLE]",
            "revenue_cagr_5y": f.revenue_cagr_5y if f else "[NOT AVAILABLE]",
            "revenue_cagr_3y": f.revenue_cagr_3y if f else "[NOT AVAILABLE]",
            "pat_cagr_5y": f.pat_cagr_5y if f else "[NOT AVAILABLE]",
            "roe_5y_avg": f.roe_5y_avg if f else "[NOT AVAILABLE]",
            "ebitda_margin": f.ebitda_margin_latest if f else "[NOT AVAILABLE]",
            "debt_to_equity": f.debt_to_equity if f else "[NOT AVAILABLE]",
            "interest_coverage": f.interest_coverage if f else "[NOT AVAILABLE]",
            "fcf_latest_cr": v.fcf_latest_cr if v else "[NOT AVAILABLE]",
            "net_debt_cr": v.net_debt_cr if v else "[NOT AVAILABLE]",
            "shares_outstanding_cr": v.shares_outstanding_cr if v else "[NOT AVAILABLE]",
            "wacc_pct": wacc,
            "terminal_growth_pct": terminal_growth,
        }

        system = (
            "You are a CFA-level equity valuation analyst. "
            "Compute a DCF intrinsic value for an Indian stock using the provided data.\n\n"
            "RULES:\n"
            "1. Use three scenarios: Base (50% weight), Bull (25%), Bear (25%).\n"
            "2. Project FCF for 10 years, then compute terminal value.\n"
            "3. Use the provided WACC and terminal growth rate.\n"
            "4. FCF = CFO - Capex. If FCF is [NOT AVAILABLE], estimate from EBITDA * margin.\n"
            "5. NEVER fabricate data. Use [NOT AVAILABLE] for missing items.\n"
            "6. Return ONLY valid JSON:\n"
            "{\n"
            '  "base_intrinsic": <float or null>,\n'
            '  "bull_intrinsic": <float or null>,\n'
            '  "bear_intrinsic": <float or null>,\n'
            '  "weighted_intrinsic": <float or null>,\n'
            '  "assumptions": "<2 sentences summarising key assumptions>"\n'
            "}"
        )
        message = (
            f"Compute DCF intrinsic value for {state.ticker}.\n"
            f"Financial context:\n{json.dumps(context, indent=2)}"
        )

        response_text = await self._call_claude(
            system=system,
            messages=[{"role": "user", "content": message}],
            model=settings.model_light,
            max_tokens=settings.max_tokens_short,
        )

        data = self._parse_json_response(response_text)
        return (
            data.get("base_intrinsic"),
            data.get("bull_intrinsic"),
            data.get("bear_intrinsic"),
            data.get("weighted_intrinsic"),
        )
