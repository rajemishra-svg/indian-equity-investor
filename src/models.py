"""Pydantic models for the Indian equity investor agent."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MarketMode(str, Enum):
    NORMAL = "normal"
    CORRECTION = "correction"
    MAXIMUM_OPPORTUNITY = "maximum_opportunity"


class GateResult(str, Enum):
    PASS_GREEN = "pass_green"
    PASS_CONDITIONAL = "pass_conditional"
    FAIL = "fail"
    NOT_RUN = "not_run"


class ConvictionLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WatchlistTier(int, Enum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


class MoatType(str, Enum):
    BRAND = "brand"
    NETWORK_EFFECT = "network_effect"
    COST_LEADERSHIP = "cost_leadership"
    SWITCHING_COSTS = "switching_costs"
    REGULATORY = "regulatory"
    SCALE = "scale"
    IP_PATENTS = "ip_patents"
    NONE = "none"


class TailwindType(str, Enum):
    STRUCTURAL = "structural"
    POLICY_DRIVEN = "policy_driven"
    CYCLICAL = "cyclical"


class CyclePosition(str, Enum):
    EARLY = "early"
    MID = "mid"
    LATE = "late"


# ---------------------------------------------------------------------------
# Raw data models
# ---------------------------------------------------------------------------


class StockQuote(BaseModel):
    ticker: str
    company_name: str
    cmp: float
    w52_high: float
    w52_low: float
    dma_200: float | None = None
    market_cap_cr: float
    exchange: str = "NSE"
    data_timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_stale: bool = False
    # P2-5 / P3-4: liquidity + technical signals
    avg_daily_value_cr: float | None = None   # 3-month avg daily traded value (₹ Cr)
    volume_trend_down_days: str | None = None  # "declining" | "stable" | "increasing"


class FinancialMetrics(BaseModel):
    market_cap_cr: float | None = None  # from Screener top-ratios (fallback when NSE is blocked)
    revenue_cagr_5y: float | None = None
    revenue_cagr_3y: float | None = None
    pat_cagr_5y: float | None = None
    pat_cagr_3y: float | None = None
    roe_5y_avg: float | None = None
    roce_5y_avg: float | None = None
    cfo_net_profit_3y_avg: float | None = None
    debt_to_equity: float | None = None
    interest_coverage: float | None = None
    current_ratio: float | None = None
    net_debt_ebitda: float | None = None
    ebitda_margin_latest: float | None = None

    # ── P1-3: Working capital metrics (computed from balance sheet + revenue) ──
    # Debtor Days = Trade Receivables / Annual Revenue × 365
    debtor_days_latest: float | None = None   # most recent year
    debtor_days_3y_ago: float | None = None   # 3 years prior — for trend comparison
    # Inventory Days = Inventory / Annual Revenue × 365 (None for service companies)
    inventory_days_latest: float | None = None

    # ── P1-4: Earnings quality ──────────────────────────────────────────────
    # Other income as % of revenue — high % signals weak core-business earnings
    other_income_pct_revenue: float | None = None

    # ── P1-1: Sector-specific KPIs — financial services ────────────────────
    # Populated from Screener ratios section; None for non-banking companies.
    gnpa_pct: float | None = None    # Gross NPA % — banks/NBFCs
    nnpa_pct: float | None = None    # Net NPA %
    nim_pct: float | None = None     # Net Interest Margin %
    roa_pct: float | None = None     # Return on Assets %
    car_pct: float | None = None     # Capital Adequacy Ratio %

    # ── P2-3: Trend direction signals ──────────────────────────────────────
    # Trajectory matters as much as the absolute level.
    # Values: "improving" | "stable" | "deteriorating" | None
    roce_trend: str | None = None
    roe_trend: str | None = None
    ebitda_margin_trend: str | None = None
    # ── P2-4: EC-02 cyclical normalization ────────────────────────────────
    ebitda_margin_5y_avg: float | None = None  # 5Y OPM avg; used in DCF for cyclicals

    # ── Revenue (absolute) — needed for P/S ratio on pre-profit companies ──
    trailing_revenue_cr: float | None = None   # latest annual revenue in ₹ Crore

    data_flags: list[str] = Field(default_factory=list)


class GovernanceData(BaseModel):
    promoter_holding_pct: float | None = None
    promoter_pledging_pct: float | None = None
    promoter_pledging_trend: list[float] = Field(default_factory=list)  # last 8 quarters
    pledging_trend_direction: str | None = None  # "increasing", "decreasing", "stable"
    auditor_name: str | None = None
    auditor_changed_3y: bool = False
    audit_qualifications: list[str] = Field(default_factory=list)
    rpt_pct_revenue: float | None = None
    contingent_liabilities_pct_networth: float | None = None
    sebi_orders: list[str] = Field(default_factory=list)
    # Default False — assume dirty until enrichment confirms clean (Bug 1.5 fix).
    # A company whose SEBI data was never fetched must NOT get credit for a clean record.
    sebi_record_clean: bool = False
    # sebi_record_checked tracks whether enrichment actually ran and queried SEBI.
    # The immediate trigger fires only when checked=True AND clean=False — this prevents
    # a network/API error during enrichment from causing a spurious REJECT.
    sebi_record_checked: bool = False
    capital_allocation_description: str | None = None
    # EC-06: set True for MNC subsidiaries and professionally-managed companies
    # where promoter holding is naturally low (foreign parent holds via FPI/FDI routes
    # or there is no controlling promoter family).  When True, the promoter_holding >= 40%
    # gate in Step 0 is waived.  Populated by BSE shareholding parser or governance
    # enrichment; defaults to False (conservative).
    is_mnc: bool = False
    # P3-2: Insider / promoter activity (last 3 months from BSE bulk/block deals)
    # "buying" = net promoter/insider purchases, "selling" = net sales, "neutral" = mixed
    insider_net_buying_3m: str | None = None
    data_flags: list[str] = Field(default_factory=list)


class ValuationData(BaseModel):
    pe_current: float | None = None
    ev_ebitda_current: float | None = None
    pbv_current: float | None = None
    peg_ratio: float | None = None
    fcf_yield_pct: float | None = None
    pe_10y_percentile: float | None = None
    pe_5y_percentile: float | None = None
    pe_10y_low: float | None = None
    pe_10y_high: float | None = None
    forward_eps_2y: float | None = None
    forward_eps_cagr_2y: float | None = None
    fcf_latest_cr: float | None = None
    net_debt_cr: float | None = None
    shares_outstanding_cr: float | None = None
    data_flags: list[str] = Field(default_factory=list)


class TechnicalData(BaseModel):
    cmp: float
    w52_high: float
    w52_low: float
    pct_from_52w_low: float
    dma_200: float | None = None
    rsi_14: float | None = None
    volume_trend_down_days: str | None = None  # "declining", "stable", "increasing"


# ---------------------------------------------------------------------------
# Step result models
# ---------------------------------------------------------------------------


class PreScreenResult(BaseModel):
    score: int
    max_score: int = 9
    gate: GateResult
    metric_scores: dict[str, bool] = Field(default_factory=dict)
    failed_metrics: list[str] = Field(default_factory=list)
    conditional_exceptions: list[str] = Field(default_factory=list)
    data_flags: list[str] = Field(default_factory=list)


class GovernanceScore(BaseModel):
    score: int
    max_score: int = 15
    gate: GateResult
    immediate_triggers: list[str] = Field(default_factory=list)
    sub_scores: dict[str, int] = Field(default_factory=dict)
    concerns: list[str] = Field(default_factory=list)
    data_flags: list[str] = Field(default_factory=list)


class MoatAssessment(BaseModel):
    moat_type: MoatType
    moat_durability: str
    market_position: str
    market_share_trend: str
    tam_multiple: float | None = None
    working_capital_flag: str
    moat_narrative: str
    # Compressed 1-sentence summary for use in downstream steps (reduces token cost).
    # Auto-generated from moat_narrative if not explicitly set.
    moat_narrative_short: str | None = None
    # P3-1: Management quality signals from concall research
    management_guidance_reliability: str | None = None  # "High" | "Medium" | "Low" | None
    concall_quality_note: str | None = None             # 1-sentence note or None
    data_flags: list[str] = Field(default_factory=list)


class FinancialGateResult(BaseModel):
    score: int  # out of 7
    gate: GateResult
    hard_triggers_fired: list[str] = Field(default_factory=list)
    hurdles_met: dict[str, bool] = Field(default_factory=dict)
    sector_overrides: list[str] = Field(default_factory=list)
    data_flags: list[str] = Field(default_factory=list)


class TailwindAssessment(BaseModel):
    sector: str
    tailwind_type: TailwindType
    cycle_position: CyclePosition
    growth_runway_years: str
    headwind_flags: list[str] = Field(default_factory=list)
    tailwind_narrative: str
    data_flags: list[str] = Field(default_factory=list)


class ValuationResult(BaseModel):
    gate: GateResult
    dcf_intrinsic_base: float | None = None
    dcf_intrinsic_bull: float | None = None
    dcf_intrinsic_bear: float | None = None
    dcf_intrinsic_weighted: float | None = None
    margin_of_safety_pct: float | None = None
    required_mos_pct: float = 35.0
    mos_met: bool = False
    methods_in_buy_zone: int = 0
    max_methods: int = 5  # total valuation methods evaluated (PE percentile, PEG, DCF, FCF yield, EV/EBITDA)
    pe_percentile_verdict: str | None = None  # EXCELLENT/FAIR/EXPENSIVE/AVOID
    peg_verdict: str | None = None
    fcf_yield_verdict: str | None = None
    ev_ebitda_verdict: str | None = None
    data_flags: list[str] = Field(default_factory=list)


class TechnicalSignal(BaseModel):
    signals_met: int
    signal_details: dict[str, bool] = Field(default_factory=dict)
    entry_guidance: str  # GREEN/AMBER/RED
    tranche_1_price: float | None = None
    tranche_2_price: float | None = None
    tranche_3_price: float | None = None
    data_flags: list[str] = Field(default_factory=list)


class PeerData(BaseModel):
    ticker: str
    name: str
    revenue_cagr_5y: float | None = None
    pat_cagr_5y: float | None = None
    ebitda_margin: float | None = None
    roe_5y_avg: float | None = None
    roce_5y_avg: float | None = None
    debt_to_equity: float | None = None
    forward_pe: float | None = None
    ev_ebitda_forward: float | None = None
    promoter_holding: float | None = None
    pledging_pct: float | None = None


class PeerComparisonResult(BaseModel):
    gate: GateResult
    target_quality_rank: int | None = None
    target_valuation_rank: int | None = None
    peer_count: int = 0
    peers: list[PeerData] = Field(default_factory=list)
    dominant_peer: str | None = None
    data_flags: list[str] = Field(default_factory=list)


class PremortRisk(BaseModel):
    primary_risk: str
    secondary_risk: str
    tertiary_risk: str
    risk_type: str  # CYCLICAL_MANAGEABLE or STRUCTURAL_UNHEDGEABLE
    proceed: bool
    data_flags: list[str] = Field(default_factory=list)


class TrancheEntry(BaseModel):
    tranche: int
    pct_allocation: int
    price: float
    condition: str


class ExitStrategy(BaseModel):
    fundamental_trigger: str
    valuation_exit_price: float | None = None
    stop_loss_price: float | None = None
    ltcg_eligible_after: str | None = None  # date string


# ---------------------------------------------------------------------------
# Main analysis state
# ---------------------------------------------------------------------------


class AnalysisState(BaseModel):
    ticker: str
    company_name: str = ""
    mode: MarketMode = MarketMode.NORMAL
    nifty_level: float | None = None
    nifty_52w_high: float | None = None
    nifty_decline_pct: float | None = None
    sector_name: str | None = None
    # P3-3: Conglomerate detection — ITC, L&T-type multi-business companies where
    # standard DCF undervalues the sum of parts.  Detected by sector classifier.
    is_conglomerate: bool = False

    # Raw fetched data
    quote: StockQuote | None = None
    financials: FinancialMetrics | None = None
    governance_data: GovernanceData | None = None
    valuation_data: ValuationData | None = None
    technical_data: TechnicalData | None = None

    # Step results
    pre_screen: PreScreenResult | None = None
    governance: GovernanceScore | None = None
    moat: MoatAssessment | None = None
    financial_gate: FinancialGateResult | None = None
    tailwind: TailwindAssessment | None = None
    valuation: ValuationResult | None = None
    technical: TechnicalSignal | None = None
    peer_comparison: PeerComparisonResult | None = None
    premortem: PremortRisk | None = None

    # Pipeline status
    current_step: int = 0
    terminated_at_step: int | None = None
    termination_reason: str | None = None
    all_data_flags: list[str] = Field(default_factory=list)
    error_tags: list[str] = Field(default_factory=list)

    # Final recommendation
    recommendation_type: str | None = None  # BUY/WATCHLIST/REJECT/PEER_SWITCH
    watchlist_tier: WatchlistTier | None = None
    conviction: ConvictionLevel | None = None
    suggested_allocation_pct: float | None = None
    investment_thesis: str | None = None
    tranches: list[TrancheEntry] = Field(default_factory=list)
    exit_strategy: ExitStrategy | None = None
    formatted_output: str | None = None

    def add_flag(self, flag: str) -> None:
        if flag not in self.all_data_flags:
            self.all_data_flags.append(flag)

    def add_error(self, error_tag: str) -> None:
        if error_tag not in self.error_tags:
            self.error_tags.append(error_tag)

    @property
    def is_terminated(self) -> bool:
        return self.terminated_at_step is not None

    @property
    def cap_size(self) -> str:
        """Return cap-size bucket.  Returns 'mid_cap' when quote is unavailable so
        WACC/MoS defaults are conservative rather than silently wrong.  Callers that
        need to distinguish genuine unknowns should check ``state.quote is None``."""
        if self.quote:
            mc = self.quote.market_cap_cr
            if mc >= 20000:
                return "large_cap"
            elif mc >= 5000:
                return "mid_cap"
            return "small_cap"
        # Quote unavailable — default to mid_cap (uses higher WACC/MoS than large_cap).
        # Pipeline adds an explicit flag so the analyst is aware (see 8.3 fix in pipeline.py).
        return "mid_cap"

    @property
    def required_mos_pct(self) -> float:
        base = {"large_cap": 25.0, "mid_cap": 35.0, "small_cap": 45.0}.get(self.cap_size, 35.0)
        # Deeper corrections warrant a more generous MoS reduction — more stocks are forced sellers
        if self.mode == MarketMode.MAXIMUM_OPPORTUNITY:
            base -= 10.0  # Nifty >15% below peak — deploy aggressively
        elif self.mode == MarketMode.CORRECTION:
            base -= 5.0   # Nifty 8–15% below peak — modest concession
        return base
