"""Step 5M — Multibagger Potential Scoring (deterministic, no Claude).

Runs after Step 5G regardless of valuation gate outcome.  Produces a composite
0-10 score across five dimensions that differentiate a true multi-year compounder
from a regular growth stock.

Key design principle (per planning analysis):
  • TAM unknown → score 0/2 on tam_runway, but does NOT block MULTIBAGGER_CANDIDATE.
    The absence of an established TAM can itself signal an undiscovered market.
  • ROIIC from proxy (CFO/Revenue) → reinvestment_runway capped at 1 pt (not 2).
  • Valuation gap is the biggest separator: PEG < 1.0 at > 25% CAGR is the Bajaj
    Finance signal — "reasonable valuation before market recognises the story."

Verdict thresholds:
  8-10 → MULTIBAGGER_CANDIDATE (high conviction; rare)
  6-7  → GROWTH_BUY (strong growth, market partially aware)
  4-5  → GROWTH_WATCHLIST (story intact but premium priced in)
  0-3  → GROWTH_REJECT (growth premium not justified)
"""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.models import AnalysisState, GrowthMetrics, MultibaggerScore, WatchlistTier


def _tam_confidence_weight(tam_source: str | None) -> float:
    """Convert TAM source quality to a confidence multiplier for tam_runway_score."""
    return {
        "industry_report": 1.0,
        "mgmt_filing":     0.75,   # management may inflate TAM
        "llm_inference":   0.5,    # trained-data estimate — use with caution
        None:              0.0,
    }.get(tam_source or "", 0.0)


class Step5MMultibagger(BaseStep):
    """Multibagger potential scoring — composite 0-10 across five dimensions."""

    step_number = 55  # logical position: after 5 (5G), before 6
    step_name = "Multibagger Potential Scoring"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:  # noqa: C901
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )

        f = state.financials
        v = state.valuation_data
        gm = state.growth_metrics or GrowthMetrics()
        g = state.governance_data

        data_flags: list[str] = []
        score = MultibaggerScore()

        rev_3y = f.revenue_cagr_3y if f else None
        rev_cr = f.trailing_revenue_cr if f else None

        # ==================================================================
        # Component 1 — Valuation Gap (0-3)
        # "Market hasn't priced in the growth."  PEG < 1.0 at 25%+ CAGR is
        # the defining signal — cheap relative to delivered growth rate.
        # ==================================================================
        peg = v.peg_ratio if v else None
        ev_rev = gm.ev_revenue_ratio

        if peg is not None and rev_3y is not None:
            if peg < 0.5 and rev_3y >= 30:
                score.valuation_gap_score = 3
                score.valuation_gap_reason = (
                    f"PEG {peg:.2f} < 0.5 at {rev_3y:.0f}% revenue CAGR — "
                    "market is deeply underpricing the growth rate"
                )
                data_flags.append(
                    f"[MULTIBAGGER SIGNAL: PEG {peg:.2f} + Rev CAGR {rev_3y:.0f}% — "
                    "classic pre-discovery setup]"
                )
            elif peg < 1.0 and rev_3y >= 25:
                score.valuation_gap_score = 2
                score.valuation_gap_reason = (
                    f"PEG {peg:.2f} < 1.0 at {rev_3y:.0f}% revenue CAGR — "
                    "reasonable valuation before full market recognition"
                )
            elif peg <= 2.0:
                score.valuation_gap_score = 1
                score.valuation_gap_reason = (
                    f"PEG {peg:.2f} — growth partially priced in; "
                    "still room for re-rating if growth sustains"
                )
            else:
                score.valuation_gap_score = 0
                score.valuation_gap_reason = (
                    f"PEG {peg:.2f} > 2.0 — market has already recognised the growth story; "
                    "multibagger from here requires sustained outperformance of expectations"
                )
        elif peg is None and ev_rev is not None and rev_3y is not None:
            # Pre-profit: use EV/Revenue as a proxy for valuation discipline
            # Cheap EV/Revenue relative to growth rate ≈ low implied P/S
            growth_implied_fair_ps = rev_3y / 10  # rough rule: 10× PE / growth rate
            if ev_rev < growth_implied_fair_ps * 0.5:
                score.valuation_gap_score = 2
                score.valuation_gap_reason = (
                    f"EV/Revenue {ev_rev:.1f}× well below growth-implied fair P/S "
                    f"{growth_implied_fair_ps:.1f}× — pre-profit but cheaply priced"
                )
                data_flags.append("[MULTIBAGGER NOTE: pre-profit company; PEG unavailable; EV/Rev used as proxy]")
            elif ev_rev < growth_implied_fair_ps:
                score.valuation_gap_score = 1
                score.valuation_gap_reason = (
                    f"EV/Revenue {ev_rev:.1f}× below growth-implied fair P/S — moderate gap"
                )
            else:
                score.valuation_gap_score = 0
                score.valuation_gap_reason = (
                    f"EV/Revenue {ev_rev:.1f}× at or above growth-implied fair value"
                )
                data_flags.append("[MULTIBAGGER NOTE: pre-profit; PEG unavailable; EV/Rev used as proxy]")
        else:
            score.valuation_gap_score = 0
            score.valuation_gap_reason = "Valuation gap indeterminate — PEG and EV/Revenue both unavailable"
            data_flags.append("[DATA UNVERIFIED: valuation_gap — PEG and EV/Rev both missing]")

        # ==================================================================
        # Component 2 — Reinvestment Runway (0-2)
        # High ROIIC + low TAM penetration = the business can redeploy profits
        # at high rates for years.  This is the Buffett/Lynch compounder test.
        # ==================================================================
        roiic = gm.roiic_3y
        roiic_proxy = gm.roiic_proxy_cfo_revenue
        tam_pct = gm.tam_penetration_est_pct

        roiic_strong = (roiic is not None and roiic >= 20) or (roiic_proxy is not None and roiic_proxy >= 20)
        roiic_is_proxy = roiic is None and roiic_proxy is not None
        tam_early = tam_pct is not None and tam_pct < 10

        max_reinvestment = 1 if roiic_is_proxy else 2  # proxy method: cap at 1

        if roiic_strong and tam_early:
            score.reinvestment_runway = min(max_reinvestment, 2)
            data_flags.append(
                f"[POSITIVE: reinvestment runway — ROIIC "
                f"{'(proxy) ' if roiic_is_proxy else ''}"
                f"{(roiic or roiic_proxy):.0f}% + TAM penetration {tam_pct:.1f}%]"
            )
        elif roiic_strong or tam_early:
            score.reinvestment_runway = min(max_reinvestment, 1)
            if roiic_is_proxy:
                data_flags.append(
                    "[REINVESTMENT: one condition met; ROIIC from CFO/Revenue proxy — "
                    "verify with actual capex data for higher conviction]"
                )
        else:
            score.reinvestment_runway = 0
            if roiic is None and roiic_proxy is None:
                data_flags.append("[DATA UNVERIFIED: ROIIC — reinvestment runway scored 0]")

        # ==================================================================
        # Component 3 — TAM Runway (0-2, confidence-weighted)
        # Revenue at 20% TAM penetration vs current revenue tells us how many
        # times the business can multiply before hitting structural limits.
        # ==================================================================
        tam_cr = gm.tam_size_cr
        tam_conf = _tam_confidence_weight(gm.tam_source)

        if tam_cr is not None and rev_cr and rev_cr > 0:
            revenue_at_20pct = tam_cr * 0.20
            tam_multiple = revenue_at_20pct / rev_cr

            if tam_multiple >= 10:
                raw_score = 2
                score.compounding_horizon_years = "10-15 years"
            elif tam_multiple >= 5:
                raw_score = 1
                score.compounding_horizon_years = "7-10 years"
            else:
                raw_score = 0
                score.compounding_horizon_years = "3-7 years"

            # Apply confidence weight (industry report = 1.0x, LLM = 0.5x)
            score.tam_runway_score = round(raw_score * tam_conf)
            score.tam_confidence = gm.tam_source or "none"

            if raw_score > 0:
                data_flags.append(
                    f"[TAM RUNWAY: {tam_multiple:.1f}× to 20% penetration "
                    f"(TAM ₹{tam_cr:,.0f} Cr, revenue ₹{rev_cr:,.0f} Cr) "
                    f"[SOURCE: {gm.tam_source or 'unknown'}, confidence {tam_conf:.0%}]]"
                )
                if tam_conf < 0.75:
                    data_flags.append(
                        "[TAM CAUTION: estimate from low-confidence source — "
                        "verify with IBEF/CRISIL report before MULTIBAGGER classification]"
                    )
        else:
            score.tam_runway_score = 0
            score.tam_confidence = "none"
            if score.compounding_horizon_years == "":
                score.compounding_horizon_years = "7-10 years"  # default when unknown
            data_flags.append(
                "[TAM UNKNOWN: tam_runway_score = 0; MULTIBAGGER_CANDIDATE still possible "
                "if other four components are strong — undiscovered TAM is itself a signal]"
            )

        # ==================================================================
        # Component 4 — Promoter Decade Conviction (0-2)
        # Multibagger analysis needs the 5-10Y track record, not just today's holding.
        # Consistent hold + no historical pledge + low dilution = aligned promoter.
        # ==================================================================
        holding_trend = gm.promoter_holding_trend_5y
        current_holding = g.promoter_holding_pct if g else None
        current_pledging = g.promoter_pledging_pct if g else None
        pledging_trend = g.pledging_trend_direction if g else None
        dilution = gm.equity_dilution_3y_pct

        conditions_met = 0
        promoter_concerns = []

        # Condition A: holding trend
        if holding_trend in ("increasing", "stable"):
            conditions_met += 1
        elif holding_trend == "declining":
            promoter_concerns.append(f"promoter holding declining (now {current_holding or 'N/A'}%)")
        else:
            data_flags.append("[DATA UNVERIFIED: promoter_holding_trend_5y — decade track record unavailable]")

        # Condition B: pledging clean
        if current_pledging is not None:
            no_pledge_hist = (
                pledging_trend not in ("increasing",) and (current_pledging < 5)
            )
            if no_pledge_hist:
                conditions_met += 1
            else:
                promoter_concerns.append(f"pledging {current_pledging:.1f}% or increasing trend")
        else:
            data_flags.append("[DATA UNVERIFIED: promoter_pledging — pledge history incomplete]")
            conditions_met += 1  # benefit of doubt — scored in prefetch

        # Condition C: low dilution
        if dilution is not None:
            if dilution < 15:
                conditions_met += 1
            elif dilution < 30:
                promoter_concerns.append(f"moderate equity dilution {dilution:.0f}% over 3Y")
            else:
                promoter_concerns.append(f"significant equity dilution {dilution:.0f}% over 3Y")
        else:
            data_flags.append("[DATA UNVERIFIED: equity_dilution_3y — dilution check incomplete]")
            conditions_met += 1  # benefit of doubt

        # Score from conditions_met (max 3 conditions checked)
        if conditions_met >= 3:
            score.promoter_decade_score = 2
            data_flags.append("[POSITIVE: promoter aligned — holding stable/increasing, no pledge history, low dilution]")
        elif conditions_met == 2:
            score.promoter_decade_score = 1
            if promoter_concerns:
                data_flags.append(f"[PROMOTER CONCERN: {'; '.join(promoter_concerns)}]")
        else:
            score.promoter_decade_score = 0
            data_flags.append(f"[PROMOTER CONCERN (multibagger risk): {'; '.join(promoter_concerns)}]")

        # ==================================================================
        # Component 5 — Earnings Quality (0-1)
        # Real, clean earnings that can compound.  Adjusted metrics, heavy other
        # income, or RPT inflation means the reported number may not compound.
        # ==================================================================
        cfo_np = f.cfo_net_profit_3y_avg if f else None
        other_inc = f.other_income_pct_revenue if f else None
        rpt = state.governance_data.rpt_pct_revenue if state.governance_data else None

        quality_flags = []
        if cfo_np is not None and cfo_np > 80:
            pass  # clean
        elif cfo_np is not None and cfo_np > 0:
            pass  # acceptable
        elif cfo_np is not None and cfo_np <= 0:
            quality_flags.append(f"CFO/NP {cfo_np:.0f}% — cash not backing reported profits")
        else:
            pass  # unknown — neutral

        if other_inc is not None and other_inc > 20:
            quality_flags.append(f"other income {other_inc:.1f}% of revenue — core earnings inflated")

        if rpt is not None and rpt > 10:
            quality_flags.append(f"RPT {rpt:.1f}% of revenue — related-party risk")

        if not quality_flags:
            score.earnings_quality_score = 1
            data_flags.append("[POSITIVE: earnings quality clean — CFO, other income, RPT all within bounds]")
        else:
            score.earnings_quality_score = 0
            data_flags.append(f"[EARNINGS QUALITY CONCERN: {'; '.join(quality_flags)}]")

        # ==================================================================
        # Final composite
        # ==================================================================
        total = (
            score.valuation_gap_score
            + score.reinvestment_runway
            + score.tam_runway_score
            + score.promoter_decade_score
            + score.earnings_quality_score
        )
        score.total_score = total

        if total >= 8:
            score.verdict = "MULTIBAGGER_CANDIDATE"
        elif total >= 6:
            score.verdict = "GROWTH_BUY"
        elif total >= 4:
            score.verdict = "GROWTH_WATCHLIST"
        else:
            score.verdict = "GROWTH_REJECT"

        # Milestones — derived from available data
        milestones = []
        if rev_cr and rev_3y:
            rev_2y = round(rev_cr * ((1 + rev_3y / 100) ** 2))
            milestones.append(f"Revenue ≥ ₹{rev_2y:,.0f} Cr in 2Y (confirms {rev_3y:.0f}% CAGR on track)")
        if gm.gross_margin_pct:
            target_gm = round(gm.gross_margin_pct + 3, 1)
            milestones.append(f"Gross margin > {target_gm}% (unit economics holding at scale)")
        milestones.append("Promoter holding unchanged or increasing (conviction maintained)")
        score.key_milestones = milestones[:3]

        # Narrative
        score.score_narrative = (
            f"Multibagger score {total}/10: "
            f"valuation gap {score.valuation_gap_score}/3, "
            f"reinvestment runway {score.reinvestment_runway}/2, "
            f"TAM runway {score.tam_runway_score}/2, "
            f"promoter conviction {score.promoter_decade_score}/2, "
            f"earnings quality {score.earnings_quality_score}/1. "
            f"{score.valuation_gap_reason}"
        )
        score.data_flags = data_flags

        state.multibagger_score = score

        # Update recommendation type
        state.recommendation_type = score.verdict

        # Set watchlist tier for GROWTH_WATCHLIST
        if score.verdict == "GROWTH_WATCHLIST":
            state.watchlist_tier = WatchlistTier.TIER_2

        for flag in data_flags:
            state.add_flag(flag)

        self.log.info(
            "multibagger_scored",
            ticker=state.ticker,
            total_score=total,
            verdict=score.verdict,
            valuation_gap=score.valuation_gap_score,
            reinvestment_runway=score.reinvestment_runway,
            tam_runway=score.tam_runway_score,
            promoter_decade=score.promoter_decade_score,
            earnings_quality=score.earnings_quality_score,
        )

        return state
