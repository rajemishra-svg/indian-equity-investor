"""Forensic scorecard — deterministic Tijori-style green/amber/red checklist.

No LLM calls; pure functions over already-fetched ``FinancialMetrics`` /
``GovernanceData`` / ``ValuationData``. Reuses the same thresholds as the
main pipeline's Step 0/1/3 hurdles (see ``src/sector/profiles.py``'s
``default`` profile) so a stock that would fail the real pipeline doesn't
get a clean bill of health here, and vice versa.

Each check returns a single ``ScorecardItem``. ``status`` is "green",
"amber", or "red" — amber always means "not enough data to judge", never
a genuine mixed signal; a real mixed signal is decided one way or the
other with the reasoning stated in ``explanation``.

Mostly sector-unaware by design (see `investor analyze` for the sector-aware
pipeline) — with one deliberate exception: D/E, interest coverage, and ROCE
don't mean the same thing for a bank/NBFC as they do for a manufacturer, so
when ``sector_name == "financial_services"`` those three checks swap to the
P1-1 bank KPIs (CAR, GNPA/NNPA, ROA) already fetched by Screener but
otherwise unused, and "Other Income" — core banking revenue, not a red flag
there — is waived rather than scored against the generic 15% threshold.
"""
from __future__ import annotations

from src.models import FinancialMetrics, GovernanceData, ScorecardItem, ValuationData

# Reuse the default SectorProfile's thresholds — see src/sector/profiles.py.
_MIN_REVENUE_CAGR_5Y = 12.0
_MIN_PAT_CAGR_5Y = 15.0
_MIN_ROE_5Y = 15.0
_MIN_ROCE_5Y = 18.0
_MIN_CFO_NP_PCT = 70.0
_MAX_DE_RATIO = 1.0
_MIN_ICR = 6.0
_PLEDGE_HARD_TRIGGER_PCT = 10.0  # Step 1 immediate-trigger threshold
_OTHER_INCOME_FLAG_PCT = 15.0    # P1-4 threshold
_WORKING_CAPITAL_DETERIORATION_PCT = 30.0  # P1-3 threshold

# ── Bank/NBFC-specific thresholds (financial_services sector only) ─────────
# CAR: RBI's Basel III minimum incl. capital conservation buffer is ~11.5%;
#   well-capitalized private banks typically run 13%+.
# GNPA/NNPA: <3% GNPA and <1% NNPA are the commonly used "healthy asset
#   quality" bar for Indian banks/NBFCs.
# NIM / ROA: 3% NIM and 1% ROA are standard "healthy" watermarks.
_MIN_CAR_PCT = 13.0
_MAX_GNPA_PCT = 3.0
_MAX_NNPA_PCT = 1.0
_MIN_NIM_PCT = 3.0
_MIN_ROA_PCT = 1.0
_FINANCIAL_SERVICES = "financial_services"


def _green(category: str, name: str, explanation: str) -> ScorecardItem:
    return ScorecardItem(category=category, name=name, status="green", explanation=explanation)


def _amber(category: str, name: str, explanation: str) -> ScorecardItem:
    return ScorecardItem(category=category, name=name, status="amber", explanation=explanation)


def _red(category: str, name: str, explanation: str) -> ScorecardItem:
    return ScorecardItem(category=category, name=name, status="red", explanation=explanation)


_ACCOUNTING = "Accounting & Shareholding"
_PRICE = "Price & Performance"


# ---------------------------------------------------------------------------
# Accounting & Shareholding
# ---------------------------------------------------------------------------


def _contingent_liabilities(g: GovernanceData) -> ScorecardItem:
    v = g.contingent_liabilities_pct_networth
    if v is None:
        return _amber(_ACCOUNTING, "Contingent Liabilities", "Not available — verify manually against annual report notes to accounts")
    if v < 10.0:
        return _green(_ACCOUNTING, "Contingent Liabilities", f"{v:.1f}% of net worth — not significant")
    return _red(_ACCOUNTING, "Contingent Liabilities", f"{v:.1f}% of net worth — material exposure, verify the underlying claims")


def _other_income(f: FinancialMetrics, sector_name: str) -> ScorecardItem:
    if sector_name == _FINANCIAL_SERVICES:
        return _amber(
            _ACCOUNTING, "Other Income",
            "Waived for financial services — fee income, treasury gains, etc. are core banking revenue, not a red flag the way they are for a non-financial company"
        )
    v = f.other_income_pct_revenue
    if v is None:
        return _amber(_ACCOUNTING, "Other Income", "Not available")
    if v < _OTHER_INCOME_FLAG_PCT:
        return _green(_ACCOUNTING, "Other Income", f"{v:.1f}% of revenue — not distorting core profitability")
    return _red(_ACCOUNTING, "Other Income", f"{v:.1f}% of revenue (≥{_OTHER_INCOME_FLAG_PCT:.0f}%) — core operating profit may be overstated")


def _pledge(g: GovernanceData) -> ScorecardItem:
    v = g.promoter_pledging_pct
    if v is None:
        return _amber(_ACCOUNTING, "Pledge", "Not available")
    if v == 0:
        return _green(_ACCOUNTING, "Pledge", "Promoters have not pledged any holding")
    if v < _PLEDGE_HARD_TRIGGER_PCT:
        return _amber(_ACCOUNTING, "Pledge", f"{v:.1f}% pledged — below the {_PLEDGE_HARD_TRIGGER_PCT:.0f}% red-flag threshold but monitor the trend")
    return _red(_ACCOUNTING, "Pledge", f"{v:.1f}% pledged — exceeds {_PLEDGE_HARD_TRIGGER_PCT:.0f}%, a governance hard-trigger elsewhere in this pipeline")


def _public_holding(g: GovernanceData) -> ScorecardItem:
    """Public shareholder % trend (BSE SHPSUMMARY "Public shareholder" row —
    institutions + non-institutions + retail combined, not retail alone;
    a true individual/retail-only split needs a more detailed BSE/NSE
    shareholding pattern filing this pipeline doesn't fetch yet)."""
    trend = g.public_holding_trend
    if len(trend) < 2:
        return _amber(_ACCOUNTING, "Public Holding", "Insufficient trend history")
    if trend[-1] < trend[0]:
        return _green(_ACCOUNTING, "Public Holding", "Declining — public shareholders reducing stake, often a favourable contrarian entry signal")
    if trend[-1] > trend[0]:
        return _red(_ACCOUNTING, "Public Holding", "Rising — public shareholders accumulating, removing the contrarian tailwind")
    return _amber(_ACCOUNTING, "Public Holding", "Flat over the tracked quarters")


def _promoter_holding(g: GovernanceData) -> ScorecardItem:
    trend = g.promoter_holding_trend
    if len(trend) < 2:
        return _amber(_ACCOUNTING, "Promoter Holding", "Insufficient trend history")
    if trend[-1] < trend[0]:
        drop = trend[0] - trend[-1]
        return _red(_ACCOUNTING, "Promoter Holding", f"Declined {drop:.1f}pp over the tracked quarters — may signal reduced promoter conviction")
    return _green(_ACCOUNTING, "Promoter Holding", "Stable or increasing — promoters have not been selling")


def _depreciation_effect(f: FinancialMetrics) -> ScorecardItem:
    """Annual depreciation charge (≈ YoY change in accumulated depreciation)
    as a % of gross block. A meaningfully declining rate while profit is
    rising can mean profit growth is partly an artefact of slower asset
    write-down rather than genuine operating improvement."""
    gb, ad = f.gross_block_cr_series, f.accumulated_depreciation_cr_series
    if len(gb) < 3 or len(ad) < 3 or len(gb) != len(ad):
        return _amber(_ACCOUNTING, "Depreciation Effect", "Insufficient gross block / accumulated depreciation history")

    charges = [ad[i] - ad[i - 1] for i in range(1, len(ad))]
    rates = [
        charges[i] / gb[i + 1] * 100
        for i in range(len(charges))
        if gb[i + 1] > 0
    ]
    if len(rates) < 2:
        return _amber(_ACCOUNTING, "Depreciation Effect", "Insufficient data to compute a depreciation-rate trend")

    if rates[-1] < rates[0] * 0.8:  # rate fell by more than 20% relative to itself
        return _red(
            _ACCOUNTING, "Depreciation Effect",
            f"Depreciation rate fell from {rates[0]:.1f}% to {rates[-1]:.1f}% of gross block — verify this isn't inflating reported profit"
        )
    return _green(_ACCOUNTING, "Depreciation Effect", f"Depreciation rate stable (~{rates[-1]:.1f}% of gross block)")


def _revenue_recognition(f: FinancialMetrics) -> ScorecardItem:
    """Receivables outrunning sales + weak cash conversion is the classic
    aggressive-revenue-recognition signature."""
    latest, prior = f.debtor_days_latest, f.debtor_days_3y_ago
    cfo_np = f.cfo_net_profit_3y_avg
    if latest is None or prior is None or cfo_np is None:
        return _amber(_ACCOUNTING, "Revenue Recognition", "Insufficient debtor-days or CFO/NP data")

    receivables_deteriorating = prior > 0 and (latest - prior) / prior * 100 > _WORKING_CAPITAL_DETERIORATION_PCT
    weak_cash_conversion = cfo_np < _MIN_CFO_NP_PCT

    if receivables_deteriorating and weak_cash_conversion:
        return _red(
            _ACCOUNTING, "Revenue Recognition",
            f"Debtor days up from {prior:.0f} to {latest:.0f} while CFO/NP is only {cfo_np:.0f}% — operating profit isn't converting to cash as receivables rise faster than sales"
        )
    return _green(_ACCOUNTING, "Revenue Recognition", "No signs of aggressive revenue recognition")


# ---------------------------------------------------------------------------
# Price & Performance
# ---------------------------------------------------------------------------


def _capex_vs_roce(f: FinancialMetrics) -> ScorecardItem:
    capex = f.capex_cr_3y
    if len(capex) < 2 or f.roce_trend is None:
        return _amber(_PRICE, "Capex vs ROCE", "Insufficient capex history or ROCE trend")

    heavy_capex = capex[-1] > capex[0]
    if heavy_capex and f.roce_trend == "deteriorating":
        return _red(_PRICE, "Capex vs ROCE", "Capex has grown but ROCE is deteriorating — capital being deployed unproductively")
    return _green(_PRICE, "Capex vs ROCE", f"ROCE trend is {f.roce_trend} alongside current capex levels")


def _roe(f: FinancialMetrics) -> ScorecardItem:
    if f.roe_5y_avg is None:
        return _amber(_PRICE, "ROE", "Not available")
    if f.roe_5y_avg >= _MIN_ROE_5Y and f.roe_trend != "deteriorating":
        return _green(_PRICE, "ROE", f"{f.roe_5y_avg:.1f}% 5Y avg (≥{_MIN_ROE_5Y:.0f}%), trend {f.roe_trend or 'unknown'}")
    if f.roe_5y_avg < _MIN_ROE_5Y:
        return _red(_PRICE, "ROE", f"{f.roe_5y_avg:.1f}% 5Y avg — below the {_MIN_ROE_5Y:.0f}% bar")
    return _red(_PRICE, "ROE", f"{f.roe_5y_avg:.1f}% 5Y avg clears the {_MIN_ROE_5Y:.0f}% bar, but the trend is deteriorating")


def _working_capital(f: FinancialMetrics) -> ScorecardItem:
    latest, prior = f.debtor_days_latest, f.debtor_days_3y_ago
    if latest is None or prior is None or prior <= 0:
        return _amber(_PRICE, "Working Capital", "Insufficient debtor-days history")
    change_pct = (latest - prior) / prior * 100
    if change_pct > _WORKING_CAPITAL_DETERIORATION_PCT:
        return _red(_PRICE, "Working Capital", f"Debtor days up {change_pct:.0f}% — working capital cycle deteriorating")
    return _green(_PRICE, "Working Capital", "Working capital cycle appears under control")


def _valuation_vs_history(v: ValuationData | None) -> ScorecardItem:
    if v is None or v.pe_10y_percentile is None:
        return _amber(_PRICE, "Current vs Historic Valuation", "10Y PE percentile not available")
    p = v.pe_10y_percentile
    if p < 50:
        return _green(_PRICE, "Current vs Historic Valuation", f"Trading at the {p:.0f}th percentile of its own 10Y PE range — below its historic median")
    return _red(_PRICE, "Current vs Historic Valuation", f"Trading at the {p:.0f}th percentile of its own 10Y PE range — above its historic median")


def _sales_growth(f: FinancialMetrics) -> ScorecardItem:
    if f.revenue_cagr_5y is None:
        return _amber(_PRICE, "Sales Growth", "Not available")
    if f.revenue_cagr_5y >= _MIN_REVENUE_CAGR_5Y:
        return _green(_PRICE, "Sales Growth", f"{f.revenue_cagr_5y:.1f}% 5Y CAGR")
    return _red(_PRICE, "Sales Growth", f"{f.revenue_cagr_5y:.1f}% 5Y CAGR — below the {_MIN_REVENUE_CAGR_5Y:.0f}% bar")


def _roce_strength(f: FinancialMetrics, sector_name: str) -> ScorecardItem:
    if sector_name == _FINANCIAL_SERVICES:
        if f.roa_pct is None:
            return _amber(_PRICE, "ROCE Strength", "Return on Assets (ROCE's bank equivalent) not available")
        if f.roa_pct >= _MIN_ROA_PCT:
            return _green(_PRICE, "ROCE Strength", f"ROA {f.roa_pct:.1f}% (≥{_MIN_ROA_PCT:.0f}%) — healthy asset profitability")
        return _red(_PRICE, "ROCE Strength", f"ROA {f.roa_pct:.1f}% — below the {_MIN_ROA_PCT:.0f}% bar for asset profitability")

    if f.roce_5y_avg is None:
        return _amber(_PRICE, "ROCE Strength", "Not available")
    if f.roce_5y_avg >= _MIN_ROCE_5Y and f.roce_trend != "deteriorating":
        return _green(_PRICE, "ROCE Strength", f"{f.roce_5y_avg:.1f}% 5Y avg (≥{_MIN_ROCE_5Y:.0f}%), trend {f.roce_trend or 'unknown'}")
    if f.roce_5y_avg < _MIN_ROCE_5Y:
        return _red(_PRICE, "ROCE Strength", f"{f.roce_5y_avg:.1f}% 5Y avg — below the {_MIN_ROCE_5Y:.0f}% bar")
    return _red(_PRICE, "ROCE Strength", f"{f.roce_5y_avg:.1f}% 5Y avg clears the {_MIN_ROCE_5Y:.0f}% bar, but the trend is deteriorating")


def _balance_sheet_strength(f: FinancialMetrics, sector_name: str) -> ScorecardItem:
    if sector_name == _FINANCIAL_SERVICES:
        car = f.car_pct
        if car is None:
            return _amber(_PRICE, "Balance Sheet Strength", "Capital Adequacy Ratio (D/E's bank equivalent) not available")
        if car >= _MIN_CAR_PCT:
            return _green(_PRICE, "Balance Sheet Strength", f"CAR {car:.1f}% (≥{_MIN_CAR_PCT:.0f}%) — comfortable capital buffer above regulatory minimums")
        return _red(_PRICE, "Balance Sheet Strength", f"CAR {car:.1f}% — thin capital buffer, below the {_MIN_CAR_PCT:.0f}% watermark")

    de, cr = f.debt_to_equity, f.current_ratio
    if de is None:
        return _amber(_PRICE, "Balance Sheet Strength", "Debt/equity not available")
    if de < _MAX_DE_RATIO and (cr is None or cr >= 1.0):
        return _green(_PRICE, "Balance Sheet Strength", f"D/E {de:.2f}x — solvency and liquidity look sound")
    return _red(_PRICE, "Balance Sheet Strength", f"D/E {de:.2f}x — may face solvency or liquidity strain")


def _debt(f: FinancialMetrics, sector_name: str) -> ScorecardItem:
    if sector_name == _FINANCIAL_SERVICES:
        gnpa, nnpa = f.gnpa_pct, f.nnpa_pct
        if gnpa is None and nnpa is None:
            return _amber(_PRICE, "Debt", "GNPA/NNPA (this check's bank equivalent — asset quality, not leverage) not available")
        gnpa_bad = gnpa is not None and gnpa >= _MAX_GNPA_PCT
        nnpa_bad = nnpa is not None and nnpa >= _MAX_NNPA_PCT
        if gnpa_bad or nnpa_bad:
            parts = []
            if gnpa is not None:
                parts.append(f"GNPA {gnpa:.1f}%")
            if nnpa is not None:
                parts.append(f"NNPA {nnpa:.1f}%")
            return _red(_PRICE, "Debt", f"{', '.join(parts)} — asset quality concern (bank equivalent of high leverage)")
        parts = []
        if gnpa is not None:
            parts.append(f"GNPA {gnpa:.1f}%")
        if nnpa is not None:
            parts.append(f"NNPA {nnpa:.1f}%")
        return _green(_PRICE, "Debt", f"{', '.join(parts)} — asset quality looks healthy")

    icr, nde = f.interest_coverage, f.net_debt_ebitda
    if icr is None:
        return _amber(_PRICE, "Debt", "Interest coverage not available")
    if icr > _MIN_ICR and (nde is None or nde < 3.0):
        return _green(_PRICE, "Debt", f"Interest coverage {icr:.1f}x — comfortable debt servicing")
    return _red(_PRICE, "Debt", f"Interest coverage {icr:.1f}x — may have difficulty servicing debt")


def _share_price_rerating(f: FinancialMetrics, v: ValuationData | None) -> ScorecardItem:
    """Proxy for 'has the price run up on fundamentals or on a rising
    multiple': high current PE-percentile + weak earnings growth means the
    market has re-rated the stock rather than earnings driving the price.
    This is a coarser check than the pipeline's Reverse DCF
    (implied vs delivered growth) used in `investor analyze`; use that for
    a rigorous answer, this is a quick screen."""
    if v is None or v.pe_10y_percentile is None or f.pat_cagr_5y is None:
        return _amber(_PRICE, "Share Price", "Insufficient PE-percentile or PAT-growth data")
    if v.pe_10y_percentile > 70 and f.pat_cagr_5y < _MIN_PAT_CAGR_5Y:
        return _red(
            _PRICE, "Share Price",
            f"PE near 10Y highs ({v.pe_10y_percentile:.0f}th percentile) despite {f.pat_cagr_5y:.1f}% PAT growth — price gains look rerating-driven, not earnings-driven"
        )
    return _green(_PRICE, "Share Price", "No sign the price has run ahead of earnings growth")


def _margin_stability(f: FinancialMetrics, sector_name: str) -> ScorecardItem:
    if sector_name == _FINANCIAL_SERVICES:
        # EBITDA isn't a standard bank metric; Net Interest Margin is the
        # equivalent profitability-per-unit-of-assets signal. No trend series
        # is fetched for NIM (single latest value only), so this is a level
        # check, not a trend check like the non-financial version.
        if f.nim_pct is None:
            return _amber(_PRICE, "Margin Stability", "Net Interest Margin (EBITDA margin's bank equivalent) not available")
        if f.nim_pct >= _MIN_NIM_PCT:
            return _green(_PRICE, "Margin Stability", f"NIM {f.nim_pct:.1f}% (≥{_MIN_NIM_PCT:.0f}%) — healthy interest margin")
        return _red(_PRICE, "Margin Stability", f"NIM {f.nim_pct:.1f}% — below the {_MIN_NIM_PCT:.0f}% watermark")

    if f.ebitda_margin_trend is None:
        return _amber(_PRICE, "Margin Stability", "Not available")
    if f.ebitda_margin_trend == "deteriorating":
        return _red(_PRICE, "Margin Stability", "EBITDA margin trend is deteriorating")
    return _green(_PRICE, "Margin Stability", f"EBITDA margin trend is {f.ebitda_margin_trend}")


def build_scorecard(
    financials: FinancialMetrics,
    governance: GovernanceData,
    valuation: ValuationData | None,
    sector_name: str = "default",
) -> list[ScorecardItem]:
    """Assemble the full forensic scorecard from already-fetched data.

    Deterministic, no network calls, no LLM — callers are responsible for
    fetching fresh ``financials``/``governance``/``valuation`` first.

    ``sector_name`` only affects four checks (Other Income, ROCE Strength,
    Balance Sheet Strength, Debt, Margin Stability) — see module docstring.
    Pass the same sector classification the main pipeline would use
    (``src.sector.classifier.classify_sector``) for consistency; the default
    "default" runs every check with its non-financial-services logic.
    """
    return [
        _contingent_liabilities(governance),
        _other_income(financials, sector_name),
        _pledge(governance),
        _public_holding(governance),
        _promoter_holding(governance),
        _depreciation_effect(financials),
        _revenue_recognition(financials),
        _capex_vs_roce(financials),
        _roe(financials),
        _working_capital(financials),
        _valuation_vs_history(valuation),
        _sales_growth(financials),
        _roce_strength(financials, sector_name),
        _balance_sheet_strength(financials, sector_name),
        _debt(financials, sector_name),
        _share_price_rerating(financials, valuation),
        _margin_stability(financials, sector_name),
    ]
