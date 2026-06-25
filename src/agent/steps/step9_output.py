"""Step 9 — Final Output Generation (format report from templates)."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

import anthropic

from src.agent.steps.base import BaseStep
from src.config import settings
from src.models import (
    AnalysisState,
    ConvictionLevel,
    ExitStrategy,
    TrancheEntry,
    WatchlistTier,
)
from src.sector.profiles import get_sector_profile


class Step9Output(BaseStep):
    """Format the final structured investment report."""

    step_number = 9
    step_name = "Final Output Generation"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Generate the formatted output and set recommendation fields."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        # Determine recommendation type if not already set
        if not state.recommendation_type:
            state.recommendation_type = (
                "GROWTH_BUY" if state.analysis_mode == "growth" else "BUY"
            )

        # ER-05: Auto-downgrade BUY → WATCHLIST when ≥5 data-error tags have
        # accumulated across the pipeline.  A BUY decision backed by this many
        # unverified data points cannot be trusted; human verification is
        # required before committing capital.
        if state.recommendation_type == "BUY" and len(state.error_tags) >= 5:
            state.recommendation_type = "WATCHLIST"
            # Passed all gates — data quality is the only blocker; use Tier 1
            state.watchlist_tier = WatchlistTier.TIER_1
            flag = (
                f"[ER-05: AUTO-DOWNGRADE — {len(state.error_tags)} data errors accumulated "
                f"({', '.join(state.error_tags)}); manual data verification required before BUY]"
            )
            state.add_flag(flag)
            self.log.warning(
                "er05_auto_downgrade",
                ticker=state.ticker,
                error_count=len(state.error_tags),
                error_tags=state.error_tags,
            )

        # Set conviction and allocation
        self._set_conviction(state)

        # Risk-adjust the allocation by realized volatility (BUY only)
        await self._apply_volatility_sizing(state)

        # Build tranches
        self._build_tranches(state)

        # Build exit strategy (ask Claude)
        await self._build_exit_strategy(state)

        # Build investment thesis only for actionable outcomes.
        _thesis_types = ("BUY", "WATCHLIST", "MULTIBAGGER_CANDIDATE", "GROWTH_BUY", "GROWTH_WATCHLIST")
        if not state.investment_thesis and state.recommendation_type in _thesis_types:
            state.investment_thesis = await self._build_thesis(state)

        # Format the final report
        formatted = self._format_report(state)
        state.formatted_output = formatted

        self.log.info(
            "output_generated",
            ticker=state.ticker,
            recommendation=state.recommendation_type,
            conviction=state.conviction.value if state.conviction else None,
            allocation_pct=state.suggested_allocation_pct,
        )
        return state

    # ------------------------------------------------------------------

    def _set_conviction(self, state: AnalysisState) -> None:
        """Determine conviction level from gate results."""
        # Growth types use multibagger score for conviction; value types use gate averages.
        if state.recommendation_type in ("MULTIBAGGER_CANDIDATE", "GROWTH_BUY", "GROWTH_WATCHLIST"):
            self._set_growth_conviction(state)
            return
        if state.recommendation_type not in ("BUY",):
            return

        scores = []
        if state.pre_screen:
            scores.append(state.pre_screen.score / state.pre_screen.max_score)
        if state.governance:
            scores.append(state.governance.score / state.governance.max_score)
        if state.financial_gate:
            scores.append(state.financial_gate.score / 7)
        if state.valuation:
            max_m = state.valuation.max_methods or 5
            scores.append(state.valuation.methods_in_buy_zone / max_m)

        # Moat quality contribution: durable moat = meaningful long-term edge.
        # "Unknown" durability means research was incomplete (e.g. ER-06) —
        # treated same as "Low" (0.35), not the previous 0.50 which was too generous.
        if state.moat:
            moat_score = {"High": 1.0, "Medium": 0.70, "Low": 0.35, "Unknown": 0.35}.get(
                state.moat.moat_durability, 0.35
            )
            # No-moat companies face structural disadvantage — cap conviction
            if state.moat.moat_type.value == "none":
                moat_score = min(moat_score, 0.40)
            scores.append(moat_score)

        avg = sum(scores) / len(scores) if scores else 0.5

        # Technical signal bonus
        tech_bonus = 0.0
        if state.technical and state.technical.entry_guidance == "GREEN":
            tech_bonus = 0.05
        elif state.technical and state.technical.entry_guidance == "AMBER":
            tech_bonus = 0.02

        # Late-cycle sector penalty — macro timing risk even if stock is cheap
        if state.tailwind and state.tailwind.cycle_position.value == "late":
            tech_bonus -= 0.03

        combined = avg + tech_bonus

        if combined >= 0.80:
            state.conviction = ConvictionLevel.HIGH
            state.suggested_allocation_pct = 5.0
        elif combined >= 0.65:
            state.conviction = ConvictionLevel.MEDIUM
            state.suggested_allocation_pct = 3.0
        else:
            state.conviction = ConvictionLevel.LOW
            state.suggested_allocation_pct = 2.0

        # EC-01: Pre-profit companies carry structurally higher risk.
        # Cap suggested allocation at 4 % regardless of conviction score to
        # prevent oversized positions in companies without proven earnings.
        is_pre_profit = any("EC-01" in flag for flag in state.all_data_flags)
        if is_pre_profit and state.suggested_allocation_pct and state.suggested_allocation_pct > 4.0:
            state.suggested_allocation_pct = 4.0

    def _set_growth_conviction(self, state: AnalysisState) -> None:
        """Set conviction and allocation for growth recommendations.

        Growth positions start smaller (1-2%) and add on milestone confirmation,
        reflecting the longer time horizon and higher volatility of growth stocks.
        Multibagger candidates get slightly higher initial allocation than growth buys.
        """
        ms = state.multibagger_score
        total = ms.total_score if ms else 0

        if state.recommendation_type == "MULTIBAGGER_CANDIDATE":
            if total >= 9:
                state.conviction = ConvictionLevel.HIGH
                state.suggested_allocation_pct = 2.0  # start small, add on milestones
            elif total >= 8:
                state.conviction = ConvictionLevel.MEDIUM
                state.suggested_allocation_pct = 1.5
            else:
                state.conviction = ConvictionLevel.LOW
                state.suggested_allocation_pct = 1.0
        elif state.recommendation_type == "GROWTH_BUY":
            if total >= 7:
                state.conviction = ConvictionLevel.MEDIUM
                state.suggested_allocation_pct = 2.0
            else:
                state.conviction = ConvictionLevel.LOW
                state.suggested_allocation_pct = 1.5
        else:  # GROWTH_WATCHLIST
            state.conviction = ConvictionLevel.LOW
            state.suggested_allocation_pct = 0.0  # monitor only

    async def _apply_volatility_sizing(self, state: AnalysisState) -> None:
        """Scale the suggested allocation by realized volatility (vol targeting).

        allocation × clamp(target_vol / realized_vol, min_factor, 1.0) — a
        stock at/below target volatility keeps its full conviction-based
        allocation; riskier names are cut proportionally so a 50%-vol small
        cap at HIGH conviction no longer gets the same weight as a 20%-vol
        large cap.  Rounded to the nearest 0.5%, floored at 1%.

        When volatility cannot be computed the allocation is left unchanged
        and flagged — never silently risk-adjust on bad data.
        """
        _actionable = {"BUY", "MULTIBAGGER_CANDIDATE", "GROWTH_BUY"}
        if state.recommendation_type not in _actionable or not state.suggested_allocation_pct:
            return
        yf_client = self.clients.get("yfinance")
        if yf_client is None:
            return

        try:
            vol = await yf_client.get_annualized_volatility(state.ticker)
        except Exception as exc:
            self.log.warning("volatility_fetch_failed", ticker=state.ticker, error=str(exc))
            vol = None
        if not isinstance(vol, (int, float)) or vol <= 0:
            state.add_flag(
                "[DATA UNVERIFIED: realized volatility — allocation not risk-adjusted]"
            )
            return

        target = settings.sizing_target_vol_pct
        factor = max(settings.sizing_min_factor, min(1.0, target / vol))
        if factor >= 0.999:
            state.add_flag(
                f"[VOLATILITY SIZING: {vol:.0f}% annualized vol ≤ {target:.0f}% target — "
                "allocation unchanged]"
            )
            return

        base_alloc = state.suggested_allocation_pct
        scaled = max(1.0, round(base_alloc * factor * 2) / 2)  # nearest 0.5%, floor 1%
        state.suggested_allocation_pct = scaled
        state.add_flag(
            f"[VOLATILITY SIZING: {vol:.0f}% annualized vol vs {target:.0f}% target → "
            f"allocation {base_alloc:.1f}% → {scaled:.1f}%]"
        )
        self.log.info(
            "volatility_sizing_applied",
            ticker=state.ticker,
            annualized_vol_pct=vol,
            factor=round(factor, 3),
            base_allocation_pct=base_alloc,
            scaled_allocation_pct=scaled,
        )

    def _build_tranches(self, state: AnalysisState) -> None:
        """Build tranche plan from technical signals using sector-aware discounts."""
        if state.technical is None:
            return

        t = state.technical
        cmp = t.tranche_1_price or (state.quote.cmp if state.quote else 0)
        base_alloc = int(state.suggested_allocation_pct or 3)

        profile = get_sector_profile(state.sector_name)
        t2_disc = profile.tranche_t2_discount if profile.tranche_t2_discount is not None else settings.tranche_t2_discount
        t3_disc = profile.tranche_t3_discount if profile.tranche_t3_discount is not None else settings.tranche_t3_discount

        # Allocate: 40% T1, 35% T2, 25% T3.
        # Use ceil/floor so T1+T2+T3 always sums exactly to base_alloc.
        t1_alloc = math.ceil(base_alloc * 0.40)
        t3_alloc = math.floor(base_alloc * 0.25)
        t2_alloc = base_alloc - t1_alloc - t3_alloc  # remainder — never under/over allocates

        state.tranches = [
            TrancheEntry(
                tranche=1,
                pct_allocation=t1_alloc,
                price=t.tranche_1_price or cmp,
                condition="Enter now at CMP",
            ),
            TrancheEntry(
                tranche=2,
                pct_allocation=t2_alloc,
                price=t.tranche_2_price or round(cmp * (1 - t2_disc), 2),
                condition=f"If price falls ~{int(t2_disc * 100)}% from CMP",
            ),
            TrancheEntry(
                tranche=3,
                pct_allocation=t3_alloc,
                price=t.tranche_3_price or round(cmp * (1 - t3_disc), 2),
                condition=f"If price falls ~{int(t3_disc * 100)}% from CMP",
            ),
        ]

    async def _build_exit_strategy(self, state: AnalysisState) -> None:
        """Build staggered exit strategy using sector-aware multipliers.

        Three valuation exit targets correspond to SectorProfile.exit_mult_1x/2x/3x:
          • exit_mult_1x: conservative — trim when price reaches fair value (~DCF intrinsic)
          • exit_mult_2x: mid — reduce further when price is well above intrinsic
          • exit_mult_3x: long-hold full exit at peak premium to intrinsic

        Cyclical sectors have tighter multipliers (1.10/1.30/1.70) vs. quality compounders
        (default 1.15/1.50/2.00) because cyclicals mean-revert faster.
        """
        cmp = state.quote.cmp if state.quote else 0.0
        dcf = state.valuation.dcf_intrinsic_weighted if state.valuation else None

        profile = get_sector_profile(state.sector_name)

        # Staggered exits: T1 trim, T2 reduce, T3 full exit
        exit_t1 = round(dcf * profile.exit_mult_1x, 2) if dcf else None
        exit_t2 = round(dcf * profile.exit_mult_2x, 2) if dcf else None
        exit_t3 = round(dcf * profile.exit_mult_3x, 2) if dcf else None

        # Use exit_t2 as the primary "valuation_exit_price" in the report summary;
        # T1 and T3 are stored as data flags so they appear in the detailed output.
        if exit_t1 and exit_t2 and exit_t3:
            state.add_flag(
                f"[EXIT TARGETS: T1-trim ₹{exit_t1} ({profile.exit_mult_1x:.2f}× DCF) | "
                f"T2-reduce ₹{exit_t2} ({profile.exit_mult_2x:.2f}× DCF) | "
                f"T3-full ₹{exit_t3} ({profile.exit_mult_3x:.2f}× DCF)]"
            )

        # Stop-loss: cap-size adjusted from config — large caps mean-revert faster
        sl_multiplier = {
            "large_cap": settings.stop_loss_large_cap,
            "mid_cap": settings.stop_loss_mid_cap,
            "small_cap": settings.stop_loss_small_cap,
        }.get(state.cap_size, settings.stop_loss_mid_cap)
        stop_loss = round(cmp * sl_multiplier, 2) if cmp else None

        # LTCG eligibility = 1 year from today (leap-safe: Feb 29 → Feb 28)
        from src.portfolio.tracker import add_one_year
        ltcg_date = add_one_year(date.today())

        # Fundamental trigger from premortem
        fundamental_trigger = (
            state.premortem.primary_risk
            if state.premortem
            else "Material deterioration in ROE/ROCE below 12%"
        )

        state.exit_strategy = ExitStrategy(
            fundamental_trigger=fundamental_trigger,
            valuation_exit_price=exit_t2,   # mid-target as primary summary price
            stop_loss_price=stop_loss,
            ltcg_eligible_after=ltcg_date.isoformat(),
        )

    async def _build_thesis(self, state: AnalysisState) -> str:
        """Ask Claude to write a 3–4 sentence investment thesis."""
        is_growth = state.analysis_mode == "growth"
        if is_growth:
            system = (
                "You are a senior growth equity analyst specialising in Indian compounders. "
                "Write a concise 3–4 sentence growth investment thesis. "
                "Focus on: (1) the TAM and how early the company is in its journey, "
                "(2) why the moat deepens at scale (or stays intact), "
                "(3) the ROIIC quality — can profits be redeployed at high rates for years, "
                "(4) why the valuation still leaves room for a multibagger return. "
                "Be specific — cite revenue CAGR, TAM penetration, and P/S or PEG numbers. "
                "NEVER fabricate data."
            )
        else:
            system = (
                "You are a senior equity analyst. "
                "Write a concise 3–4 sentence investment thesis for a long-term position. "
                "Focus on: (1) the business compounding advantage, "
                "(2) the structural tailwind, "
                "(3) why now is a reasonable entry. "
                "Be specific — no generic statements. NEVER fabricate data."
            )

        context_parts = [f"Ticker: {state.ticker}"]
        if state.moat:
            moat_ctx = state.moat.moat_narrative_short or state.moat.moat_narrative
            context_parts.append(f"Moat: {moat_ctx}")
        if state.tailwind:
            context_parts.append(f"Tailwind: {state.tailwind.tailwind_narrative}")
        if is_growth and state.growth_metrics:
            gm = state.growth_metrics
            context_parts.append(
                f"Revenue CAGR 3Y: {state.financials.revenue_cagr_3y if state.financials else 'N/A'}%"
            )
            if gm.tam_size_cr and gm.tam_penetration_est_pct:
                context_parts.append(
                    f"TAM: ₹{gm.tam_size_cr:,.0f} Cr, penetration ~{gm.tam_penetration_est_pct:.1f}%"
                )
            if gm.roiic_3y:
                context_parts.append(f"ROIIC: {gm.roiic_3y:.0f}%")
        if state.multibagger_score:
            ms = state.multibagger_score
            context_parts.append(
                f"Multibagger score: {ms.total_score}/10 — {ms.valuation_gap_reason}"
            )
        if state.valuation:
            v = state.valuation
            context_parts.append(
                f"Valuation: {v.methods_in_buy_zone}/{v.max_methods} methods in buy zone"
                + (f", MoS {v.margin_of_safety_pct:.1f}%" if v.margin_of_safety_pct else "")
            )

        message = "\n".join(context_parts) + "\n\nWrite the investment thesis."
        try:
            thesis = await self._call_claude(
                system=system,
                messages=[{"role": "user", "content": message}],
                model=settings.model_light,
                max_tokens=settings.max_tokens_thesis,
            )
            return thesis.strip()
        except Exception as exc:
            self.log.warning("thesis_build_failed", ticker=state.ticker, error=str(exc))
            return "[NOT AVAILABLE — thesis generation failed]"

    def _format_report(self, state: AnalysisState) -> str:  # noqa: C901
        """Format the full report using the BUY_RECOMMENDATION or appropriate template."""
        rtype = state.recommendation_type or "REJECT"
        now = datetime.now(UTC)
        date_str = now.strftime("%Y-%m-%d %H:%M IST")

        q = state.quote
        f = state.financials
        v = state.valuation
        t = state.technical
        g = state.governance
        pre = state.pre_screen
        fin = state.financial_gate

        mode_label = {
            "normal": "Normal",
            "correction": f"Correction — Nifty {state.nifty_decline_pct:.1f}% below peak"
            if state.nifty_decline_pct
            else "Correction",
            "maximum_opportunity": f"Maximum Opportunity — Nifty {state.nifty_decline_pct:.1f}% below peak"
            if state.nifty_decline_pct
            else "Maximum Opportunity",
        }.get(state.mode.value, "Normal")

        cap_label = {
            "large_cap": "Large Cap",
            "mid_cap": "Mid Cap",
            "small_cap": "Small Cap",
        }.get(state.cap_size, "Unknown")

        mc_str = f"₹{q.market_cap_cr:,.0f} Cr" if q else "[NOT AVAILABLE]"
        cmp_str = f"₹{q.cmp:.2f}" if q else "[NOT AVAILABLE]"
        w52h_str = f"₹{q.w52_high:.2f}" if q else "[NOT AVAILABLE]"
        w52l_str = f"₹{q.w52_low:.2f}" if q else "[NOT AVAILABLE]"

        if rtype == "BUY":
            lines = [
                "══════════════════════════════════════════════════════════════════════════",
                "                        STOCK ANALYSIS REPORT",
                "══════════════════════════════════════════════════════════════════════════",
                f"STOCK           : {state.company_name or state.ticker} | {state.ticker} | {state.tailwind.sector if state.tailwind else '[NOT AVAILABLE]'}",
                f"MARKET CAP      : {mc_str} | {cap_label}",
                f"REPORT DATE     : {date_str}",
                f"ANALYST MODE    : {mode_label}",
                f"DATA FRESHNESS  : Price as of {now.strftime('%Y-%m-%d')} | Financials as of latest quarter",
                "──────────────────────────────────────────────────────────────────────────",
                "PRICE DATA",
                f"  CMP           : {cmp_str}",
                f"  52W High      : {w52h_str} | 52W Low: {w52l_str}",
                "──────────────────────────────────────────────────────────────────────────",
                "SCREENING SUMMARY",
                f"  Pre-Screen Score  : {pre.score if pre else 'N/A'}/9  [{pre.gate.value.upper() if pre else 'N/A'}]",
                f"  Governance Score  : {g.score if g else 'N/A'}/15 [{g.gate.value.upper() if g else 'N/A'}]",
                f"  Step 3 Gate       : [{fin.gate.value.upper() if fin else 'N/A'}]",
                f"  Valuation Gate    : [{v.gate.value.upper() if v else 'N/A'}]",
                f"  Technical Signal  : {t.signals_met if t else 'N/A'}/5 signals [{t.entry_guidance if t else 'N/A'}]",
                "──────────────────────────────────────────────────────────────────────────",
                "MOAT ASSESSMENT",
            ]
            if state.moat:
                lines += [
                    f"  Type              : {state.moat.moat_type.value.replace('_', ' ').title()}",
                    f"  Durability        : {state.moat.moat_durability}",
                    f"  Moat Narrative    : {state.moat.moat_narrative}",
                ]
            else:
                lines.append("  [NOT AVAILABLE]")

            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "INVESTMENT THESIS",
                f"  {state.investment_thesis or '[NOT AVAILABLE]'}",
                "──────────────────────────────────────────────────────────────────────────",
                "FINANCIAL PERFORMANCE",
            ]

            if f:
                lines += [
                    f"  Revenue CAGR 5Y   : {f.revenue_cagr_5y}%  |  3Y: {f.revenue_cagr_3y}%",
                    f"  PAT CAGR 5Y       : {f.pat_cagr_5y}%  |  3Y: {f.pat_cagr_3y}%",
                    f"  ROE 5Y Avg        : {f.roe_5y_avg}%  |  ROCE 5Y Avg: {f.roce_5y_avg}%",
                    f"  CFO/Net Profit 3Y : {f.cfo_net_profit_3y_avg}%",
                    f"  Debt/Equity       : {f.debt_to_equity}x  |  Interest Coverage: {f.interest_coverage}x",
                    f"  EBITDA Margin     : {f.ebitda_margin_latest}%",
                ]
            else:
                lines.append("  [NOT AVAILABLE]")

            lines.append("──────────────────────────────────────────────────────────────────────────")
            lines.append("VALUATION")

            if v and state.valuation_data:
                vd = state.valuation_data
                lines += [
                    f"  Current P/E       : {vd.pe_current}x  |  10Y Percentile: {v.pe_percentile_verdict}",
                    f"  PEG Ratio         : {vd.peg_ratio}  |  Verdict: {v.peg_verdict}",
                    f"  EV/EBITDA         : {vd.ev_ebitda_current}x  |  Verdict: {v.ev_ebitda_verdict}",
                    f"  FCF Yield         : {vd.fcf_yield_pct}%  |  Verdict: {v.fcf_yield_verdict}",
                    f"  DCF Intrinsic     : ₹{v.dcf_intrinsic_bear} – ₹{v.dcf_intrinsic_bull} [ESTIMATE]",
                    f"  DCF Weighted      : ₹{v.dcf_intrinsic_weighted} [ESTIMATE]",
                    f"  Margin of Safety  : {v.margin_of_safety_pct:.1f}% (Required: {v.required_mos_pct:.1f}%)"
                    if v.margin_of_safety_pct is not None else
                    f"  Margin of Safety  : [NOT AVAILABLE] (Required: {v.required_mos_pct:.1f}%)",
                    f"  Methods in BZ     : {v.methods_in_buy_zone}/{v.max_methods}",
                ]
                if v.implied_growth_pct is not None and v.growth_anchor_pct is not None:
                    lines.append(
                        f"  Reverse DCF       : market implies {v.implied_growth_pct:.1f}% FCF growth "
                        f"vs {v.growth_anchor_pct:.1f}% delivered [ESTIMATE]"
                    )

            lines.append("──────────────────────────────────────────────────────────────────────────")
            lines.append("ENTRY PLAN")

            for tr in state.tranches:
                lines.append(
                    f"  Tranche {tr.tranche}: {tr.pct_allocation}% of portfolio @ ₹{tr.price:.2f}  |  {tr.condition}"
                )

            if state.exit_strategy:
                ex = state.exit_strategy
                lines += [
                    "──────────────────────────────────────────────────────────────────────────",
                    "EXIT STRATEGY",
                    f"  Fundamental Trigger : {ex.fundamental_trigger}",
                    f"  Valuation Exit      : ₹{ex.valuation_exit_price} [ESTIMATE]"
                    if ex.valuation_exit_price else "  Valuation Exit      : [NOT AVAILABLE]",
                    f"  Stop-Loss           : ₹{ex.stop_loss_price}"
                    if ex.stop_loss_price else "  Stop-Loss           : [NOT AVAILABLE]",
                    f"  LTCG Eligible After : {ex.ltcg_eligible_after}",
                ]

            if state.peer_comparison and state.peer_comparison.peers:
                pc = state.peer_comparison
                lines += [
                    "──────────────────────────────────────────────────────────────────────────",
                    "PEER BENCHMARKING",
                    f"  Quality Rank  : {pc.target_quality_rank}/{pc.peer_count + 1} (1=best)",
                    f"  Valuation Rank: {pc.target_valuation_rank}/{pc.peer_count + 1} (1=cheapest)",
                    f"  Peer Gate     : [{pc.gate.value.upper()}]",
                ]
                for p in pc.peers[:3]:  # show top 3 peers
                    lines.append(
                        f"  {p.ticker:12s}: ROE {p.roe_5y_avg or '[N/A]'}% | "
                        f"ROCE {p.roce_5y_avg or '[N/A]'}% | "
                        f"P/E {p.pe_current or p.forward_pe or '[N/A]'}x"
                    )

            if state.premortem:
                pm = state.premortem
                lines += [
                    "──────────────────────────────────────────────────────────────────────────",
                    "RISK ASSESSMENT (PREMORTEM)",
                    f"  Risk Type  : {pm.risk_type}",
                    f"  Primary    : {pm.primary_risk}",
                    f"  Secondary  : {pm.secondary_risk}",
                    f"  Tertiary   : {pm.tertiary_risk}",
                ]

            if state.all_data_flags:
                lines += [
                    "──────────────────────────────────────────────────────────────────────────",
                    "DATA FLAGS",
                ]
                for flag in state.all_data_flags:
                    lines.append(f"  {flag}")

            lines += [
                "══════════════════════════════════════════════════════════════════════════",
                f"RECOMMENDATION : {state.recommendation_type}  |  Conviction: {state.conviction.value.upper() if state.conviction else 'N/A'}",
                "══════════════════════════════════════════════════════════════════════════",
            ]

        elif rtype == "WATCHLIST":
            # Compute target buy price if DCF is available
            target_buy_price = None
            if state.valuation and state.valuation.dcf_intrinsic_weighted and state.valuation.required_mos_pct:
                dcf_w = state.valuation.dcf_intrinsic_weighted
                mos_req = state.valuation.required_mos_pct / 100.0
                # CMP needs to fall to: intrinsic * (1 - MoS%)
                target_buy_price = round(dcf_w * (1 - mos_req), 2)

            lines = [
                "══════════════════════════════════════════════════════════════════════════",
                f"                    WATCHLIST ADDITION — TIER {state.watchlist_tier or 2}",
                "══════════════════════════════════════════════════════════════════════════",
                f"TICKER          : {state.company_name or state.ticker} | {state.ticker}",
                f"REPORT DATE     : {date_str}",
                f"REASON          : {state.termination_reason or 'Valuation not in buy zone'}",
            ]
            if state.valuation:
                v = state.valuation
                lines += [
                    f"VALUATION SNAP  : {v.methods_in_buy_zone}/{v.max_methods} methods in buy zone | "
                    f"MoS {v.margin_of_safety_pct:.1f}% vs required {v.required_mos_pct:.1f}%"
                    if v.margin_of_safety_pct is not None else
                    f"VALUATION SNAP  : {v.methods_in_buy_zone}/{v.max_methods} methods in buy zone",
                ]
            if q:
                lines.append(f"CURRENT PRICE   : {cmp_str} | 52W Low: {w52l_str} | 52W High: {w52h_str}")
            if target_buy_price:
                lines.append(f"TARGET BUY PRICE: ₹{target_buy_price:.2f} [ESTIMATE — triggers required MoS of {state.valuation.required_mos_pct:.0f}%]")
            if state.moat:
                lines.append(f"MOAT            : {state.moat.moat_type.value.replace('_', ' ').title()} | Durability: {state.moat.moat_durability}")
            lines += [
                f"REVIEW TRIGGER  : Alert when CMP ≤ ₹{target_buy_price:.2f}" if target_buy_price else
                "REVIEW TRIGGER  : Re-evaluate when CMP drops to required MoS level",
                "══════════════════════════════════════════════════════════════════════════",
            ]

        elif rtype == "PEER_SWITCH":
            dominant = state.peer_comparison.dominant_peer if state.peer_comparison else "N/A"
            lines = [
                "══════════════════════════════════════════════════════════════════════════",
                "                         PEER SWITCH NOTICE",
                "══════════════════════════════════════════════════════════════════════════",
                f"TICKER ANALYSED  : {state.ticker}",
                f"DOMINANT PEER    : {dominant}",
                f"REPORT DATE      : {date_str}",
                f"REASON           : {state.termination_reason or 'Peer dominance detected'}",
                f"ACTION           : Run full analysis on {dominant} instead.",
                "══════════════════════════════════════════════════════════════════════════",
            ]

        elif rtype in ("MULTIBAGGER_CANDIDATE", "GROWTH_BUY", "GROWTH_WATCHLIST", "GROWTH_REJECT"):
            lines = self._format_growth_report(state, rtype, date_str, cmp_str, w52h_str, w52l_str, mc_str, cap_label)

        else:  # REJECT
            step_name = f"Step {state.terminated_at_step}" if state.terminated_at_step is not None else "Unknown"
            lines = [
                "══════════════════════════════════════════════════════════════════════════",
                "                           REJECTION LOG",
                "══════════════════════════════════════════════════════════════════════════",
                f"TICKER          : {state.ticker}",
                f"REPORT DATE     : {date_str}",
                f"TERMINATED AT   : {step_name}",
                f"REASON          : {state.termination_reason or 'Gate failure'}",
            ]
            if state.governance and state.governance.immediate_triggers:
                lines.append(f"TRIGGERS        : {', '.join(state.governance.immediate_triggers)}")
            if state.all_data_flags:
                lines.append(f"DATA FLAGS      : {', '.join(state.all_data_flags)}")
            re_eval = self._re_eval_condition(state)
            lines += [
                f"RE-EVAL WHEN    : {re_eval}",
                "══════════════════════════════════════════════════════════════════════════",
            ]

        return "\n".join(lines)

    def _format_growth_report(  # noqa: C901
        self,
        state: AnalysisState,
        rtype: str,
        date_str: str,
        cmp_str: str,
        w52h_str: str,
        w52l_str: str,
        mc_str: str,
        cap_label: str,
    ) -> list[str]:
        """Format MULTIBAGGER_CANDIDATE / GROWTH_BUY / GROWTH_WATCHLIST / GROWTH_REJECT."""
        ms = state.multibagger_score
        gm = state.growth_metrics
        f = state.financials
        v = state.valuation

        header_map = {
            "MULTIBAGGER_CANDIDATE": "MULTIBAGGER CANDIDATE",
            "GROWTH_BUY":           "GROWTH BUY",
            "GROWTH_WATCHLIST":     "GROWTH WATCHLIST",
            "GROWTH_REJECT":        "GROWTH REJECT",
        }

        lines = [
            "══════════════════════════════════════════════════════════════════════════",
            f"                     {header_map.get(rtype, rtype)}",
            "══════════════════════════════════════════════════════════════════════════",
            f"STOCK           : {state.company_name or state.ticker} | {state.ticker}",
            f"MARKET CAP      : {mc_str} | {cap_label}",
            f"REPORT DATE     : {date_str}",
            "──────────────────────────────────────────────────────────────────────────",
            "PRICE DATA",
            f"  CMP           : {cmp_str}",
            f"  52W High      : {w52h_str} | 52W Low: {w52l_str}",
        ]

        # Multibagger score breakdown
        if ms:
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                f"MULTIBAGGER POTENTIAL SCORE : {ms.total_score}/10",
                f"  Valuation Gap       : {ms.valuation_gap_score}/3  — {ms.valuation_gap_reason}",
                f"  Reinvestment Runway : {ms.reinvestment_runway}/2",
                f"  TAM Runway          : {ms.tam_runway_score}/2  [{ms.tam_confidence} confidence]",
                f"  Promoter Conviction : {ms.promoter_decade_score}/2  (5Y track record)",
                f"  Earnings Quality    : {ms.earnings_quality_score}/1",
            ]
            if ms.compounding_horizon_years:
                lines.append(f"  Compounding Horizon : {ms.compounding_horizon_years}")

        # Growth metrics
        lines.append("──────────────────────────────────────────────────────────────────────────")
        lines.append("GROWTH METRICS")
        if f:
            lines.append(f"  Revenue CAGR 3Y   : {f.revenue_cagr_3y}%  |  5Y: {f.revenue_cagr_5y}%")
            lines.append(f"  EBITDA Margin     : {f.ebitda_margin_latest}%")
        if gm:
            if gm.gross_margin_pct is not None:
                lines.append(f"  Gross Margin      : {gm.gross_margin_pct:.1f}% ({gm.gross_margin_trend})")
            if gm.rule_of_40_score is not None:
                lines.append(f"  Rule of 40        : {gm.rule_of_40_score:.0f}")
            if gm.roiic_3y is not None:
                lines.append(f"  ROIIC 3Y          : {gm.roiic_3y:.1f}%")
            elif gm.roiic_proxy_cfo_revenue is not None:
                lines.append(f"  ROIIC (proxy)     : {gm.roiic_proxy_cfo_revenue:.1f}% [CFO/Rev efficiency]")
            if gm.cash_runway_months is not None and gm.burn_rate_cr_month:
                lines.append(f"  Cash Runway       : {gm.cash_runway_months:.0f} months")
            if gm.tam_size_cr:
                pct_str = f" (~{gm.tam_penetration_est_pct:.1f}% penetrated)" if gm.tam_penetration_est_pct else ""
                lines.append(f"  TAM               : ₹{gm.tam_size_cr:,.0f} Cr{pct_str} [{gm.tam_source or 'ESTIMATE'}]")

        # Investment thesis
        lines += [
            "──────────────────────────────────────────────────────────────────────────",
            "GROWTH THESIS",
            f"  {state.investment_thesis or '[NOT AVAILABLE]'}",
        ]

        # Valuation snapshot
        lines.append("──────────────────────────────────────────────────────────────────────────")
        lines.append("VALUATION")
        if state.valuation_data:
            vd = state.valuation_data
            lines.append(f"  P/E               : {vd.pe_current}x  |  PEG: {vd.peg_ratio}")
            lines.append(f"  P/S               : {gm.ps_ratio if gm else '[N/A]'}x  |  EV/Revenue: {gm.ev_revenue_ratio if gm else '[N/A]'}x")
        if v:
            lines.append(f"  Methods in BZ     : {v.methods_in_buy_zone}/{v.max_methods}")
            if v.dcf_intrinsic_weighted:
                lines.append(f"  Forward Rev DCF   : ₹{v.dcf_intrinsic_weighted:.0f} [ESTIMATE]")

        # Entry plan (only for actionable recommendations)
        if rtype in ("MULTIBAGGER_CANDIDATE", "GROWTH_BUY") and state.tranches:
            lines.append("──────────────────────────────────────────────────────────────────────────")
            lines.append("ENTRY PLAN  (start small — add on milestone confirmation)")
            for tr in state.tranches:
                lines.append(
                    f"  Tranche {tr.tranche}: {tr.pct_allocation}% of portfolio @ ₹{tr.price:.2f}  |  {tr.condition}"
                )
            lines.append(
                f"  Total starter : {state.suggested_allocation_pct:.1f}%  |  "
                f"Conviction: {state.conviction.value.upper() if state.conviction else 'N/A'}"
            )

        # Milestones
        if ms and ms.key_milestones:
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "MILESTONES TO MONITOR",
            ]
            for i, m in enumerate(ms.key_milestones, 1):
                lines.append(f"  {i}. {m}")

        # Exit
        if state.exit_strategy and rtype in ("MULTIBAGGER_CANDIDATE", "GROWTH_BUY"):
            ex = state.exit_strategy
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "EXIT STRATEGY",
                f"  Fundamental Trigger : {ex.fundamental_trigger}",
                "  Valuation Exit      : Growth plateau (rev CAGR < 15% for 2 years) OR re-rating complete",
                f"  Stop-Loss           : ₹{ex.stop_loss_price}" if ex.stop_loss_price else "  Stop-Loss           : [NOT AVAILABLE]",
                f"  LTCG Eligible After : {ex.ltcg_eligible_after}",
            ]

        # Premortem
        if state.premortem:
            pm = state.premortem
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "RISK ASSESSMENT (GROWTH PREMORTEM)",
                f"  Primary   : {pm.primary_risk}",
                f"  Secondary : {pm.secondary_risk}",
                f"  Tertiary  : {pm.tertiary_risk}",
            ]

        # Peer benchmarking
        if state.peer_comparison and state.peer_comparison.peers:
            pc = state.peer_comparison
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "PEER BENCHMARKING",
                f"  Revenue Growth Rank : {pc.target_quality_rank}/{pc.peer_count + 1} (1=fastest)",
            ]

        # Data flags
        if state.all_data_flags:
            lines += [
                "──────────────────────────────────────────────────────────────────────────",
                "DATA FLAGS",
            ]
            for flag in state.all_data_flags:
                lines.append(f"  {flag}")

        lines += [
            "══════════════════════════════════════════════════════════════════════════",
            f"RECOMMENDATION : {rtype}  |  Score: {ms.total_score if ms else 'N/A'}/10",
            "══════════════════════════════════════════════════════════════════════════",
        ]
        return lines

    def _re_eval_condition(self, state: AnalysisState) -> str:
        """Suggest re-evaluation condition based on rejection reason."""
        if state.terminated_at_step == 0:
            return (
                "When revenue CAGR ≥12% AND ROE ≥15% hold for 2 consecutive annual reports"
            )
        elif state.terminated_at_step == 1:
            triggers = state.governance.immediate_triggers if state.governance else []
            if "promoter_pledging > 10%" in triggers:
                return "When promoter pledging falls below 5% and maintains that level for 2 quarters"
            if "active_sebi_ed_fraud_investigation" in triggers:
                return "When SEBI/ED investigation is closed with no adverse order"
            if "going_concern_qualification" in triggers:
                return "When auditor removes going concern qualification for 2 consecutive years"
            return "When all governance red flags are fully resolved and verified"
        elif state.terminated_at_step == 3:
            hard_triggers = state.financial_gate.hard_triggers_fired if state.financial_gate else []
            if hard_triggers:
                return (
                    f"Hard trigger(s) resolved: {'; '.join(hard_triggers[:2])} — "
                    "re-evaluate after 2 clean annual reports"
                )
            return "When all 7 financial hurdles are met for 2 consecutive annual reports"
        # Note: Step 8 (Premortem) has no hard gate — terminated_at_step == 8 never fires.
        return "When underlying business fundamentals improve materially for 2+ quarters"
