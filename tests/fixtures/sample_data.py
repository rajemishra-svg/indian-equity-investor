"""Realistic sample data for tests — RELIANCE NSE."""
from datetime import UTC, datetime

from src.models import (
    FinancialMetrics,
    GovernanceData,
    StockQuote,
    TechnicalData,
    ValuationData,
)

# ---------------------------------------------------------------------------
# Good fundamentals — passes Step 0 and Step 3
# ---------------------------------------------------------------------------

SAMPLE_QUOTE = StockQuote(
    ticker="RELIANCE",
    company_name="Reliance Industries Limited",
    cmp=2850.50,
    w52_high=3217.90,
    w52_low=2220.75,
    dma_200=2650.00,
    market_cap_cr=1_931_000.0,  # ~₹19.3 lakh crore — large cap
    exchange="NSE",
    data_timestamp=datetime(2026, 5, 15, 9, 30, 0, tzinfo=UTC),
    is_stale=False,
)

SAMPLE_FINANCIALS = FinancialMetrics(
    revenue_cagr_5y=18.5,
    revenue_cagr_3y=22.0,
    pat_cagr_5y=21.3,
    pat_cagr_3y=19.8,
    roe_5y_avg=22.4,
    roce_5y_avg=25.1,
    cfo_net_profit_3y_avg=92.0,
    debt_to_equity=0.38,
    interest_coverage=12.5,
    current_ratio=1.4,
    net_debt_ebitda=1.1,
    ebitda_margin_latest=18.2,
    data_flags=[],
)

SAMPLE_GOVERNANCE = GovernanceData(
    promoter_holding_pct=50.6,
    promoter_pledging_pct=0.0,
    promoter_pledging_trend=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    pledging_trend_direction="stable",
    auditor_name="Price Waterhouse & Co LLP",
    auditor_changed_3y=False,
    audit_qualifications=[],
    rpt_pct_revenue=5.2,
    contingent_liabilities_pct_networth=8.5,
    sebi_orders=[],
    sebi_record_clean=True,
    capital_allocation_description=(
        "Reliance has consistently reinvested in high-ROE opportunities — "
        "Jio (telecom), JioMart (retail), and green energy. "
        "No value-destructive acquisitions. Regular dividend payments."
    ),
    data_flags=[],
)

SAMPLE_VALUATION = ValuationData(
    pe_current=24.5,
    ev_ebitda_current=15.2,
    pbv_current=2.1,
    peg_ratio=1.15,
    fcf_yield_pct=3.8,
    pe_10y_percentile=38.0,
    pe_5y_percentile=42.0,
    pe_10y_low=11.0,
    pe_10y_high=36.0,
    forward_eps_2y=145.0,
    forward_eps_cagr_2y=21.3,
    fcf_latest_cr=45_000.0,
    net_debt_cr=130_000.0,
    shares_outstanding_cr=678.0,
    data_flags=[],
)

SAMPLE_TECHNICAL = TechnicalData(
    cmp=2850.50,
    w52_high=3217.90,
    w52_low=2220.75,
    pct_from_52w_low=28.4,  # not within 15% of 52W low
    dma_200=2650.00,
    rsi_14=52.3,
    volume_trend_down_days="stable",
)

# ---------------------------------------------------------------------------
# Bad governance — fails Step 1 (pledging > 10%)
# ---------------------------------------------------------------------------

BAD_GOVERNANCE = GovernanceData(
    promoter_holding_pct=40.0,
    promoter_pledging_pct=18.5,  # > 10% → immediate trigger
    promoter_pledging_trend=[12.0, 14.0, 15.5, 16.0, 17.0, 17.5, 18.0, 18.5],
    pledging_trend_direction="increasing",
    auditor_name="Unknown & Associates",
    auditor_changed_3y=True,
    audit_qualifications=["Going concern doubt noted in FY24 report"],
    rpt_pct_revenue=25.0,  # > 20% → another trigger
    contingent_liabilities_pct_networth=55.0,
    sebi_orders=["SEBI order 2023 — investigation into related party transactions"],
    sebi_record_clean=False,
    capital_allocation_description=(
        "Company made several value-destructive acquisitions in FY22-23. "
        "Multiple write-offs. Dividends irregular."
    ),
    data_flags=["[DATA UNVERIFIED: audit_quality]"],
)

# ---------------------------------------------------------------------------
# Weak financials — fails Step 3
# ---------------------------------------------------------------------------

WEAK_FINANCIALS = FinancialMetrics(
    revenue_cagr_5y=6.2,   # < 12
    revenue_cagr_3y=4.5,
    pat_cagr_5y=8.0,       # < 15
    pat_cagr_3y=3.5,
    roe_5y_avg=9.5,        # < 15
    roce_5y_avg=10.2,      # < 18
    cfo_net_profit_3y_avg=42.0,  # < 80 AND < 50 → hard trigger
    debt_to_equity=3.5,    # > 3.0 → hard trigger
    interest_coverage=2.1,  # < 3 → hard trigger
    current_ratio=0.8,
    net_debt_ebitda=5.5,
    ebitda_margin_latest=6.5,
    data_flags=["[DATA UNVERIFIED: revenue_cagr_5y]"],
)
