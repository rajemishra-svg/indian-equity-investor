"""Step 1 — Governance & Management Gate (deterministic + Claude for narrative)."""
from __future__ import annotations

import anthropic

from src.agent.steps.base import BaseStep
from src.agent.tools import TOOLS
from src.config import settings
from src.models import AnalysisState, GateResult, GovernanceScore
from src.sector.profiles import get_sector_profile

# Immediate rejection triggers — any single one fails the gate
IMMEDIATE_TRIGGER_CHECKS = [
    (
        "promoter_pledging > 10%",
        lambda g: (
            g.promoter_pledging_pct is not None and g.promoter_pledging_pct > 10.0
        ),
    ),
    (
        "active_sebi_ed_fraud_investigation",
        # Fire ONLY when actual SEBI/ED orders are on record.
        # "sebi_record_clean=False with no orders" means the enrichment model expressed
        # doubt but found nothing specific — treat as DATA UNVERIFIED, not confirmed fraud.
        # Confirmed fraud requires explicit orders (sebi_orders non-empty).
        lambda g: bool(g.sebi_orders),
    ),
    (
        "rpt > 20% of revenue (unexplained)",
        lambda g: g.rpt_pct_revenue is not None and g.rpt_pct_revenue > 20.0,
    ),
    (
        "auditor_resigned_mid_year",
        lambda g: g.auditor_changed_3y and any(
            "resign" in q.lower() for q in g.audit_qualifications
        ),
    ),
    (
        "going_concern_qualification",
        lambda g: any("going concern" in q.lower() for q in g.audit_qualifications),
    ),
]


class Step1Governance(BaseStep):
    """Governance & Management Gate — hard rejection criteria."""

    step_number = 1
    step_name = "Governance & Management Gate"

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic, clients: dict) -> None:
        super().__init__(anthropic_client, clients)

    async def run(self, state: AnalysisState) -> AnalysisState:
        """Score governance and determine gate result."""
        self.log.info(
            "step_start",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
        )
        state.current_step = self.step_number

        # Enrich governance data with auditor / RPT / SEBI research before scoring
        await self._enrich_governance_data(state)

        g = state.governance_data

        sub_scores: dict[str, int] = {}
        data_flags: list[str] = []
        concerns: list[str] = []

        # Initialise immediate_triggers early so CL gate can append before the
        # IMMEDIATE_TRIGGER_CHECKS loop runs below.
        immediate_triggers: list[str] = []

        # --- 1. Pledging score (0–3) ---
        pledging_score = self._score_pledging(g, data_flags)
        sub_scores["pledging"] = pledging_score

        # --- 1b. Pledging trend — add concern but don't score separately ---
        if g is not None and g.pledging_trend_direction == "increasing" and (
            g.promoter_pledging_pct is not None and g.promoter_pledging_pct > 0
        ):
            concerns.append(
                f"Pledging trend is INCREASING ({g.promoter_pledging_pct:.1f}%) — watch closely; "
                "rising pledging signals promoter stress."
            )

        # --- 1c. Contingent liabilities — soft warnings + hard gate ---
        #
        # Hard gate (Rule #3): Contingent Liabilities must be < Net Profit.
        #
        # We store CL as % of net worth.  Net Profit / Net Worth = ROE / 100,
        # so the net-worth denominator cancels:
        #   CL > Net Profit  ⟺  cl_pct_networth > roe_5y_avg
        #
        # When ROE data is unavailable, fall back to the 15% minimum hurdle so
        # the gate still fires at the most lenient reasonable threshold.
        if g is not None and g.contingent_liabilities_pct_networth is not None:
            cl = g.contingent_liabilities_pct_networth
            f_now = state.financials
            roe_proxy = (
                f_now.roe_5y_avg
                if f_now and f_now.roe_5y_avg is not None
                else 15.0  # minimum hurdle — conservative fallback
            )
            roe_source = "5Y avg ROE" if (f_now and f_now.roe_5y_avg is not None) else "default 15% hurdle"

            # Hard gate: CL exceeds net profit in absolute terms
            if cl > roe_proxy:
                trigger_msg = (
                    f"contingent_liabilities_exceed_net_profit "
                    f"(CL = {cl:.0f}% of net worth, {roe_source} = {roe_proxy:.1f}% → "
                    f"CL > Net Profit)"
                )
                immediate_triggers.append(trigger_msg)
                data_flags.append(
                    f"[HARD GATE: contingent_liabilities = {cl:.0f}% of net worth "
                    f"exceeds net profit proxy ({roe_source} = {roe_proxy:.1f}%) — "
                    "pipeline terminated; hidden liability could wipe out a year of earnings]"
                )
            else:
                # Soft warnings for elevated-but-passing CL
                if cl > 75:
                    data_flags.append(
                        f"[HIGH RISK: contingent_liabilities = {cl:.0f}% of net worth "
                        f"(below net profit threshold of {roe_proxy:.1f}% but elevated) — "
                        "manual verification required]"
                    )
                    concerns.append(
                        f"Contingent liabilities very elevated: {cl:.0f}% of net worth "
                        f"(net profit proxy: {roe_proxy:.1f}%)"
                    )
                elif cl > 50:
                    concerns.append(
                        f"Contingent liabilities elevated: {cl:.0f}% of net worth "
                        f"(net profit proxy: {roe_proxy:.1f}%)"
                    )
        elif g is not None and g.contingent_liabilities_pct_networth is None:
            data_flags.append(
                "[DATA UNVERIFIED: contingent_liabilities — could not determine from filings; "
                "verify manually before investing]"
            )

        # --- 2. Audit quality score (0–3) ---
        audit_score = self._score_audit(g, data_flags, concerns)
        sub_scores["audit"] = audit_score

        # --- 3. RPT discipline score (0–3) ---
        rpt_score = self._score_rpt(g, data_flags)
        sub_scores["rpt"] = rpt_score

        # --- 4. Capital allocation track record (0–3) — ask Claude ---
        cap_alloc_score = await self._score_capital_allocation(state, data_flags)
        sub_scores["capital_allocation"] = cap_alloc_score

        # --- 4b. P3-2: Insider/promoter activity signal (non-scoring observation) ---
        if g is not None and g.insider_net_buying_3m is not None:
            signal = g.insider_net_buying_3m
            if signal == "NET_BUYING":
                data_flags.append(
                    "[POSITIVE: insider/promoter NET BUYING in last 3 months — "
                    "constructive signal; verify size and context]"
                )
            elif signal == "NET_SELLING":
                concerns.append(
                    "Insider/promoter NET SELLING in last 3 months — watch closely; "
                    "may signal near-term concern or simple profit-booking."
                )

        # --- 5. Regulatory history score (0–3) ---
        reg_score = self._score_regulatory(g, data_flags, concerns)
        sub_scores["regulatory"] = reg_score

        total_score = sum(sub_scores.values())

        # --- Immediate trigger check (extends list started above for CL gate) ---
        if g is not None:
            for trigger_name, checker in IMMEDIATE_TRIGGER_CHECKS:
                try:
                    if checker(g):
                        immediate_triggers.append(trigger_name)
                except Exception:
                    pass

        # --- Gate determination ---
        if immediate_triggers:
            gate = GateResult.FAIL
            for t in immediate_triggers:
                concerns.append(f"IMMEDIATE TRIGGER FIRED: {t}")
        elif total_score >= 12:
            gate = GateResult.PASS_GREEN
        elif total_score >= 9:
            gate = GateResult.PASS_CONDITIONAL
        else:
            gate = GateResult.FAIL

        result = GovernanceScore(
            score=total_score,
            max_score=15,
            gate=gate,
            immediate_triggers=immediate_triggers,
            sub_scores=sub_scores,
            concerns=concerns,
            data_flags=data_flags,
        )
        state.governance = result

        self.log.info(
            "gate_decision",
            step=self.step_number,
            step_name=self.step_name,
            ticker=state.ticker,
            gate=gate.value,
            score=total_score,
            max_score=15,
            immediate_triggers=immediate_triggers,
            sub_scores=sub_scores,
        )

        if gate == GateResult.FAIL:
            state.terminated_at_step = self.step_number
            state.termination_reason = (
                f"Governance FAILED: score {total_score}/15, "
                f"triggers={immediate_triggers}, "
                f"concerns={concerns}"
            )
            state.recommendation_type = "REJECT"
            self.log.info(
                "pipeline_terminated",
                step=self.step_number,
                ticker=state.ticker,
                reason=state.termination_reason,
            )

        for flag in data_flags:
            state.add_flag(flag)

        return state

    # ------------------------------------------------------------------
    # Governance data enrichment (runs before scoring)
    # ------------------------------------------------------------------

    async def _enrich_governance_data(self, state: AnalysisState) -> None:
        """Research missing governance fields via a targeted Claude mini-loop.

        Populates auditor_name, auditor_changed_3y, rpt_pct_revenue,
        contingent_liabilities_pct_networth, and sebi_record_clean by doing
        narrow web searches against NSE/BSE filings and SEBI SCORES.

        Skips enrichment if all key fields are already populated from prefetch.
        Uses Haiku + web_search/web_fetch, ≤ 4 iterations (cheap).
        """
        g = state.governance_data
        if g is None:
            return

        needs_auditor = g.auditor_name is None
        needs_rpt = g.rpt_pct_revenue is None
        # sebi_record_clean now defaults to False — enrichment is needed when
        # sebi_orders is empty AND the flag has not been affirmatively confirmed clean
        # (i.e. it is still at the conservative False default).
        needs_sebi = not g.sebi_orders and not g.sebi_record_clean
        needs_insider = g.insider_net_buying_3m is None  # P3-2: insider activity

        if not (needs_auditor or needs_rpt or needs_sebi or needs_insider):
            self.log.debug("governance_enrichment_skipped_all_fields_present", ticker=state.ticker)
            return

        ticker = state.ticker
        company = state.company_name or ticker
        missing = []
        if needs_auditor:
            missing.append("auditor_name, auditor_changed_3y, audit_qualifications")
        if needs_rpt:
            missing.append("rpt_pct_revenue (related party transactions as % of revenue)")
        if needs_sebi:
            missing.append("sebi_record_clean, sebi_orders")
        if needs_insider:
            missing.append("insider_net_buying_3m (net promoter/insider buying sentiment last 3 months)")

        system = (
            "You are an Indian equity governance researcher. "
            f"Research {company} ({ticker}, NSE-listed) and return a JSON object with these fields:\n"
            "{\n"
            '  "auditor_name": "<current statutory auditor firm name or null>",\n'
            '  "auditor_changed_3y": <true/false — changed auditor in last 3 years>,\n'
            '  "audit_qualifications": ["<any going-concern or revenue-recognition qualifications, or empty list>"],\n'
            '  "rpt_pct_revenue": <related-party transactions as % of revenue from latest annual report, or null>,\n'
            '  "contingent_liabilities_pct_networth": <contingent liabilities as % of net worth from latest balance sheet, or null>,\n'
            '  "sebi_record_clean": <true if no active SEBI/ED fraud investigation, false otherwise>,\n'
            '  "sebi_orders": ["<brief description of any SEBI/ED orders if present, else empty list>"],\n'
            '  "insider_net_buying_3m": null\n'
            "  // ^ Use exactly: \"NET_BUYING\", \"NET_SELLING\", \"NEUTRAL\", or JSON null (no quotes around null)\n"
            "}\n\n"
            "RULES:\n"
            "1. Use web_search and web_fetch to look up: NSE corporate filings, annual report notes, "
            "SEBI SCORES portal, and news.\n"
            "2. For auditor: search '[company] statutory auditor annual report 2024 site:nseindia.com OR site:bseindia.com'.\n"
            "3. For RPT: search '[company] related party transactions annual report 2024'.\n"
            "4. For SEBI: search '[company] SEBI order notice 2023 2024'.\n"
            "5. P3-2 For insider activity: search '[company] promoter buying selling BSE bulk block deal 2024' "
            "and '[ticker] insider trading disclosure bseindia.com'. "
            "NET_BUYING = promoters/insiders net bought shares; NET_SELLING = net sold; "
            "NEUTRAL = no significant activity or offsetting; null = data not found.\n"
            "6. Return ONLY the JSON — no markdown, no explanation.\n"
            "7. Use null for any field you cannot confidently determine. Do NOT guess."
        )
        initial_message = (
            f"Research governance data for {company} ({ticker}).\n"
            f"Missing fields needed: {', '.join(missing)}.\n"
            "Search NSE/BSE filings and SEBI SCORES. Return only the JSON object."
        )

        try:
            response_text = await self._agentic_loop(
                system=system,
                initial_message=initial_message,
                tools=TOOLS,
                model=settings.model_light,
                max_tokens=settings.max_tokens_short,
                max_iterations=4,
            )
            enriched = self._parse_json_response(response_text)
        except Exception as exc:
            self.log.warning(
                "governance_enrichment_failed", ticker=ticker, error=str(exc)
            )
            return

        # Merge enriched fields into existing GovernanceData (only fill missing)
        if needs_auditor:
            if enriched.get("auditor_name"):
                g.auditor_name = str(enriched["auditor_name"])
            if enriched.get("auditor_changed_3y") is not None:
                g.auditor_changed_3y = bool(enriched["auditor_changed_3y"])
            quals = enriched.get("audit_qualifications")
            if isinstance(quals, list) and quals:
                g.audit_qualifications = [str(q) for q in quals]

        if needs_rpt and enriched.get("rpt_pct_revenue") is not None:
            try:
                g.rpt_pct_revenue = float(enriched["rpt_pct_revenue"])
            except (ValueError, TypeError):
                pass

        if enriched.get("contingent_liabilities_pct_networth") is not None:
            try:
                g.contingent_liabilities_pct_networth = float(
                    enriched["contingent_liabilities_pct_networth"]
                )
            except (ValueError, TypeError):
                pass

        if needs_sebi:
            sebi_clean = enriched.get("sebi_record_clean")
            if sebi_clean is not None:
                g.sebi_record_clean = bool(sebi_clean)
            orders = enriched.get("sebi_orders")
            if isinstance(orders, list):
                g.sebi_orders = [str(o) for o in orders]
            # Mark enrichment as having run — the immediate trigger can now fire
            g.sebi_record_checked = True

        # P3-2: Insider/promoter activity signal
        if needs_insider:
            insider_raw = enriched.get("insider_net_buying_3m")
            # Coerce string "null" (common LLM mistake) to Python None
            if isinstance(insider_raw, str) and insider_raw.lower() == "null":
                insider_raw = None
            if isinstance(insider_raw, str):
                # Normalise short-form variants the model sometimes returns
                insider_aliases = {
                    "NET_BUYING": "NET_BUYING", "BUYING": "NET_BUYING",
                    "NET_SELLING": "NET_SELLING", "SELLING": "NET_SELLING",
                    "NEUTRAL": "NEUTRAL", "NONE": "NEUTRAL",
                }
                normalised = insider_aliases.get(insider_raw.upper())
                if normalised:
                    g.insider_net_buying_3m = normalised
            # JSON null or unrecognised → leave as None (not populated)

        self.log.info(
            "governance_enrichment_complete",
            ticker=ticker,
            auditor=g.auditor_name,
            rpt_pct=g.rpt_pct_revenue,
            sebi_clean=g.sebi_record_clean,
            sebi_orders=g.sebi_orders,
            insider_signal=g.insider_net_buying_3m,
        )

    # ------------------------------------------------------------------
    # Sub-scorers
    # ------------------------------------------------------------------

    def _score_pledging(self, g, flags: list) -> int:
        if g is None or g.promoter_pledging_pct is None:
            flags.append("[PLEDGING UNKNOWN — manual verification required]")
            return 2  # API failure ≠ pledging concern; immediate trigger still guards >10%
        pct = g.promoter_pledging_pct
        if pct == 0:
            return 3
        elif pct <= 4:
            return 2
        elif pct <= 10:
            return 1
        else:
            return 0  # immediate trigger will also fire

    def _score_audit(self, g, flags: list, concerns: list) -> int:
        if g is None:
            flags.append("[DATA UNVERIFIED: auditor]")
            return 0

        # Big 4 global + their Indian affiliates + top-tier reputed Indian firms
        reputed_auditors = {
            # Big 4 global & Indian affiliates
            "price waterhouse", "deloitte", "kpmg", "ernst & young", "ey",
            "bsr", "srbc", "s r b c", "s.r.b.c",
            # Grant Thornton India (Walker Chandiok)
            "walker chandiok", "grant thornton",
            # BDO India
            "bdo", "mska",
            # Other well-regarded Indian firms
            "haribhakti", "sharp & tannan", "nanubhai",
            "s.s. kothari", "kothari & co", "chaturvedi", "lodha",
        }
        auditor_name = (g.auditor_name or "").lower()
        score = 0

        if any(b in auditor_name for b in reputed_auditors):
            score += 3
        elif auditor_name:
            score += 1
        else:
            flags.append("[DATA UNVERIFIED: auditor_name — manual verification required]")
            score += 2  # SEBI mandates external audit for all listed cos; unknown ≠ unreputed

        # Penalise audit qualifications
        if g.audit_qualifications:
            concerns.append(f"Audit qualifications: {g.audit_qualifications}")
            score = max(0, score - 1)

        return min(score, 3)

    def _score_rpt(self, g, flags: list) -> int:
        if g is None or g.rpt_pct_revenue is None:
            flags.append("[DATA UNVERIFIED: rpt_pct_revenue]")
            return 1  # conservative default
        pct = g.rpt_pct_revenue
        if pct < 8:
            return 3
        elif pct < 15:
            return 2
        elif pct < 20:
            return 1
        else:
            return 0

    async def _score_capital_allocation(self, state: AnalysisState, flags: list) -> int:
        """Ask Claude to score capital allocation using description or financial proxy."""
        g = state.governance_data
        f = state.financials

        # Build context: prefer explicit description, fall back to financial metrics
        if g is not None and g.capital_allocation_description:
            context = f"Capital allocation description:\n{g.capital_allocation_description}"
        elif f is not None:
            flags.append("[capital_allocation assessed from financial metrics — no explicit description]")
            context = (
                f"Financial proxy (use to infer capital allocation quality):\n"
                f"  ROE 5Y avg: {f.roe_5y_avg}%\n"
                f"  ROCE 5Y avg: {f.roce_5y_avg}%\n"
                f"  Revenue CAGR 5Y: {f.revenue_cagr_5y}%\n"
                f"  PAT CAGR 5Y: {f.pat_cagr_5y}%\n"
                f"  Debt/Equity: {f.debt_to_equity}\n"
                f"  CFO/Net Profit 3Y avg: {f.cfo_net_profit_3y_avg}%"
            )
        else:
            flags.append("[DATA UNVERIFIED: capital_allocation]")
            return 1

        # Use the sector profile to add appropriate context to the prompt
        profile = get_sector_profile(state.sector_name)
        sector_note = f" {profile.capital_allocation_note}" if profile.capital_allocation_note else ""

        system = (
            "You are an expert Indian equity analyst. "
            "Score the capital allocation track record of this NSE-listed company from 0 to 3. "
            "3 = consistently reinvests in high-ROE growth, no value-destructive acquisitions, "
            "regular dividends or buybacks. "
            "2 = generally good but occasional missteps. "
            "1 = mixed record. 0 = poor/value-destructive. "
            "Use your knowledge of the company combined with the provided data."
            f"{sector_note} "
            'Return JSON only: {"score": <int 0-3>, "rationale": "<one sentence>"}'
        )
        message = f"Company: {state.ticker}\n{context}"
        try:
            response = await self._call_claude(
                system=system,
                messages=[{"role": "user", "content": message}],
                model=settings.model_light,
                max_tokens=settings.max_tokens_mini,
            )
            parsed = self._parse_json_response(response)
            return max(0, min(3, int(parsed.get("score", 1))))
        except Exception as exc:
            self.log.warning(
                "capital_allocation_score_failed",
                ticker=state.ticker,
                error=str(exc),
            )
            flags.append("[DATA UNVERIFIED: capital_allocation_score]")
            return 1

    def _score_regulatory(self, g, flags: list, concerns: list) -> int:
        if g is None:
            flags.append("[DATA UNVERIFIED: sebi_record]")
            return 1
        if not g.sebi_record_checked:
            # Enrichment never ran (e.g. all fields were already present from prefetch).
            # Treat as neither clean nor dirty — give benefit of doubt but flag.
            flags.append("[DATA UNVERIFIED: sebi_record not checked in this run — manual verification recommended]")
            return 2
        if g.sebi_record_clean and not g.sebi_orders:
            return 3
        elif g.sebi_orders:
            # Known orders logged — minor record blemish but visible
            concerns.append(f"SEBI orders on record: {g.sebi_orders}")
            return 2
        elif not g.sebi_record_clean:
            # Enrichment ran but expressed doubt without finding specific orders.
            # This is a data quality gap (model uncertainty), not confirmed fraud.
            # Score same as "unchecked" — flag for manual verification but don't penalise.
            flags.append("[DATA UNVERIFIED: sebi_record — enrichment returned not-clean but no orders found; manual verification required]")
            return 2  # treat as unverified, not confirmed dirty
        else:
            return 3  # clean record, no orders
