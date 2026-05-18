"""Sector-specific threshold profiles for the investment pipeline.

Each SectorProfile encodes the threshold overrides for a particular market
sector.  A ``None`` value means "waive this check entirely" — the evaluator
always returns True for that metric.

Usage::

    from src.sector.profiles import get_sector_profile
    profile = get_sector_profile(state.sector_name)
    wacc += profile.wacc_adjustment
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SectorProfile:
    """Threshold overrides for a market sector.

    None means 'waive this check entirely'.
    """

    name: str
    display_name: str
    sector_override_note: str = ""

    # ── Step 0 / Step 3 hurdle overrides ──────────────────────────────────
    # None = waive the check (evaluator always returns True for this metric)
    min_revenue_cagr_5y: Optional[float] = 12.0
    min_pat_cagr_5y: Optional[float] = 15.0
    min_roe_5y_avg: Optional[float] = 15.0
    min_roce_5y_avg: Optional[float] = 18.0
    min_cfo_np_pct: Optional[float] = 70.0     # Step 0 threshold
    min_cfo_np_pct_s3: Optional[float] = 80.0  # Step 3 hurdle (slightly higher)
    max_de_ratio: Optional[float] = 1.0
    min_icr: Optional[float] = 6.0
    min_promoter_holding: Optional[float] = 40.0

    # ── Step 3 hard trigger overrides ─────────────────────────────────────
    # None = waive this hard trigger entirely
    hard_trigger_cfo_np_min: Optional[float] = 50.0
    hard_trigger_de_max: Optional[float] = 3.0
    hard_trigger_icr_min: Optional[float] = 3.0

    # ── Step 5 valuation adjustments ──────────────────────────────────────
    wacc_adjustment: float = 0.0            # added to base WACC (e.g. +1.0 for higher risk)
    ev_ebitda_applicable: bool = True        # False for financial services

    # Extra PE band for financial sector (price-to-book more relevant)
    pb_band_excellent: Optional[float] = None   # e.g. 1.5 for banks

    # ── Capital allocation prompt context ─────────────────────────────────
    capital_allocation_note: str = ""


SECTOR_PROFILES: dict[str, SectorProfile] = {
    "default": SectorProfile(
        name="default",
        display_name="General Industrial / Manufacturing",
    ),

    "financial_services": SectorProfile(
        name="financial_services",
        display_name="Banking, NBFC, Insurance & Financial Services",
        sector_override_note=(
            "Financial sector: D/E, ICR, CFO/NP thresholds waived — leverage is the business model"
        ),
        # Lower growth hurdles for mature banks/NBFCs
        min_revenue_cagr_5y=10.0,
        min_pat_cagr_5y=12.0,
        min_roe_5y_avg=12.0,       # PSU banks have lower ROE
        min_roce_5y_avg=None,       # not meaningful for banks (use ROE instead)
        min_cfo_np_pct=None,        # NBFCs disburse loans = cash outflow, CFO/NP meaningless
        min_cfo_np_pct_s3=None,
        max_de_ratio=None,          # structural leverage
        min_icr=None,               # waived
        # Hard triggers waived
        hard_trigger_cfo_np_min=None,
        hard_trigger_de_max=None,
        hard_trigger_icr_min=None,
        # Valuation
        ev_ebitda_applicable=False,  # EV/EBITDA not meaningful for banks
        capital_allocation_note=(
            "NOTE: This is a bank/NBFC/insurance company. High Debt/Equity is structurally "
            "normal and NOT a negative signal. Evaluate capital allocation on ROE trends, "
            "NIM, asset quality (NPA %), dividend history, and book value growth."
        ),
    ),

    "defence_govt": SectorProfile(
        name="defence_govt",
        display_name="Defence PSU / Government Contractor",
        sector_override_note=(
            "Defence/Govt sector: CFO/NP trigger relaxed — milestone billing causes lumpy cash flows"
        ),
        min_revenue_cagr_5y=8.0,    # Order book-driven, lumpy revenue recognition
        min_pat_cagr_5y=10.0,
        min_roe_5y_avg=12.0,        # PSU drag on ROE acceptable
        min_roce_5y_avg=14.0,
        min_cfo_np_pct=40.0,        # lower threshold; milestone billing compresses ratio
        min_cfo_np_pct_s3=40.0,
        # Hard trigger relaxed but not waived
        hard_trigger_cfo_np_min=25.0,  # very low bar — defence projects have years of delay
        hard_trigger_de_max=3.0,
        hard_trigger_icr_min=3.0,
        capital_allocation_note=(
            "NOTE: This is a defence/government contractor. Revenue is milestone-based with "
            "long project cycles — low CFO/NP ratio is structural, not a quality concern. "
            "Evaluate capital allocation on order book execution, ROCE on capital employed, "
            "and government relationship quality."
        ),
    ),

    "infrastructure_utility": SectorProfile(
        name="infrastructure_utility",
        display_name="Infrastructure / Utility / Power",
        sector_override_note=(
            "Infrastructure sector: D/E and ICR thresholds relaxed — project financing is normal"
        ),
        min_revenue_cagr_5y=8.0,    # regulated/annuity revenue; lower growth acceptable
        min_pat_cagr_5y=10.0,
        min_roe_5y_avg=10.0,        # capital-heavy; lower ROE acceptable
        min_roce_5y_avg=12.0,
        min_cfo_np_pct=60.0,        # slightly lower; depreciation-heavy assets
        min_cfo_np_pct_s3=65.0,
        max_de_ratio=3.0,           # project debt is normal
        min_icr=3.0,                # lower coverage acceptable for regulated utilities
        # Hard triggers relaxed
        hard_trigger_cfo_np_min=35.0,
        hard_trigger_de_max=5.0,    # infrastructure project debt
        hard_trigger_icr_min=2.0,
        wacc_adjustment=0.5,        # slightly higher risk premium
        capital_allocation_note=(
            "NOTE: This is an infrastructure/utility company. Project debt financing is normal "
            "and D/E up to 3–4x is acceptable. Evaluate capital allocation on ROCE vs. cost "
            "of capital, tariff revision history, and project execution track record."
        ),
    ),

    "capital_goods": SectorProfile(
        name="capital_goods",
        display_name="Capital Goods / Industrial Engineering",
        sector_override_note=(
            "Capital goods sector: CFO/NP threshold adjusted — working capital cycles are long"
        ),
        min_revenue_cagr_5y=10.0,
        min_pat_cagr_5y=12.0,
        min_roe_5y_avg=14.0,
        min_roce_5y_avg=16.0,
        min_cfo_np_pct=55.0,        # L/T order books → high WC; lower CFO/NP acceptable
        min_cfo_np_pct_s3=60.0,
        hard_trigger_cfo_np_min=35.0,  # relaxed trigger
        capital_allocation_note=(
            "NOTE: This is a capital goods / engineering company. Long working capital cycles "
            "and order-book-driven revenue cause CFO/NP to appear low even for quality companies. "
            "Evaluate capital allocation on ROCE, order book quality, and reinvestment rate."
        ),
    ),

    "commodities_cyclical": SectorProfile(
        name="commodities_cyclical",
        display_name="Commodities / Cyclicals",
        sector_override_note=(
            "Commodity/cyclical sector: CAGR thresholds adjusted for cycle effects"
        ),
        min_revenue_cagr_5y=8.0,    # revenue is commodity-price driven
        min_pat_cagr_5y=10.0,       # PAT volatile; through-cycle view needed
        min_roe_5y_avg=12.0,        # cycle lows drag avg
        min_roce_5y_avg=15.0,
        min_cfo_np_pct=60.0,
        hard_trigger_cfo_np_min=40.0,
        wacc_adjustment=1.0,         # higher risk premium for commodity exposure
        capital_allocation_note=(
            "NOTE: This is a commodity/cyclical company. Revenue and PAT CAGRs are distorted "
            "by commodity price cycles. Evaluate on through-cycle average ROCE, balance sheet "
            "strength at trough, and management capital discipline (avoiding peak capex)."
        ),
    ),

    "recently_listed": SectorProfile(
        name="recently_listed",
        display_name="Recently Listed (< 3 years)",
        sector_override_note=(
            "Recently listed: 5Y metrics waived — insufficient listed history"
        ),
        # Waive all 5Y metrics — no history
        min_revenue_cagr_5y=None,
        min_pat_cagr_5y=None,
        min_roe_5y_avg=None,
        min_roce_5y_avg=None,
        # Still require quality on available data
        min_cfo_np_pct=60.0,
        min_cfo_np_pct_s3=70.0,
        max_de_ratio=1.5,           # slightly more flexible for growth stage
        capital_allocation_note=(
            "NOTE: This company has < 3 years of public listing history. 5Y growth metrics "
            "are not available. Evaluate capital allocation based on available track record, "
            "promoter quality, and stated use of IPO proceeds."
        ),
    ),
}

DEFAULT_PROFILE = SECTOR_PROFILES["default"]


def get_sector_profile(sector_name: Optional[str]) -> SectorProfile:
    """Return the sector profile for the given name, defaulting to the standard profile."""
    if sector_name is None:
        return DEFAULT_PROFILE
    return SECTOR_PROFILES.get(sector_name, DEFAULT_PROFILE)
