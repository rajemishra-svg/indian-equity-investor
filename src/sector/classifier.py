"""Sector classifier — maps company name + moat narrative to a SectorProfile name.

Usage::

    from src.sector.classifier import classify_sector
    sector = classify_sector(company_name="Bharat Electronics Limited", ticker="BEL")
    # → "defence_govt"
"""
from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Keyword sets for each sector (checked against company_name.lower())
# Order matters — more specific checks first.
# ---------------------------------------------------------------------------

_FINANCIAL_KEYWORDS = frozenset({
    "bank", "banking", "finance", "financial", "insurance", "nbfc",
    "finserv", "fincorp", "lending", "credit", "housing finance",
    "microfinance", "micro finance", "bajaj fin", "hdfc", "pnb",
    "canara", "lic", "sbi life", "max life", "kotak", "axis",
    "icici pru", "aditya birla capital", "indiabulls", "muthoot",
    "manappuram", "l&t finance",
    "tata capital",  # NBFC, not industrial Tata group
    "amc", "asset management",  # mutual fund AMCs
})

_DEFENCE_KEYWORDS = frozenset({
    "bharat electronics", "bel ", "garden reach", "grse", "mazagon",
    "cochin shipyard", "goa shipyard", "hindustan aeronautics", "hal ",
    "ordnance", "defence", "aerospace", "bharat dynamics", "bel-",
    "zen technologies", "data patterns", "paras defence", "ideaforge",
    "hbl engineering",  # railway/defence electronics
})

_INFRA_UTILITY_KEYWORDS = frozenset({
    "power", "energy", "transmission", "distribution", "electricity",
    "torrent power", "tata power", "adani green", "adani transmission",
    "jsw energy", "cesc", "nhpc", "ntpc", "powergrid",
    "port", "airport", "adani ports", "gateway distriparks",
    # Gas: use specific distribution/utility keywords to avoid matching oil & gas exploration
    "pipeline", "city gas", "gas distribution", "gas utility", "gas supply",
    "piped gas", "indraprastha gas", "mahanagar gas",
    "utility", "pvnl", "delhi jal", "water treatment",
    "renewable", "solar", "wind energy",
})

_CAPITAL_GOODS_KEYWORDS = frozenset({
    "transformer", "voltamp", "switchgear", "siemens", "abb",
    "bharat forge", "thermax", "cummins", "kirloskar",
    "larsen", "l&t", "kalpataru", "kec international",
    "apar industries", "diamond power", "engineering",
    "boiler", "compressor", "pump", "valve", "bearing",
    "hbl engine",  # industrial / railway
    "triveni", "turbine", "generator",
})

_COMMODITY_KEYWORDS = frozenset({
    "steel", "iron", "metal", "aluminium", "copper", "zinc",
    "tata steel", "jsw steel", "hindalco", "vedanta", "nalco",
    "cement", "aggregate", "lime", "clinker",
    "chemical", "fertiliser", "agrochemical", "pesticide",
    "petrochemical", "crude", "refinery", "oil",
    # Oil & gas exploration/production — explicitly commodity, not infra
    "oil & natural gas", "oil india", "natural gas corporation",
    "coal", "mining", "mineral", "extraction",
    "commodity",
})

# Moat narrative keywords for financial sector (used after Step 2)
_FINANCIAL_MOAT_KEYWORDS = frozenset({
    "bank", "nbfc", "insurance", "lending", "loan", "deposit",
    "credit portfolio", "npa", "net interest margin", "nim",
    "asset under management", "aum", "mutual fund",
})

_DEFENCE_MOAT_KEYWORDS = frozenset({
    "defence", "defense", "military", "ordnance", "navy", "airforce",
    "ministry of defence", "mod ", "drdo", "classified contract",
})


def classify_sector(
    company_name: str,
    ticker: str = "",
    moat_narrative: str = "",
    listing_years: Optional[float] = None,
) -> str:
    """Return the sector name string for the given company.

    Args:
        company_name: Full company name (e.g. "Bharat Electronics Limited").
        ticker: NSE ticker (used as fallback keyword).
        moat_narrative: Step 2 moat narrative (enriches classification post-Step 2).
        listing_years: Years since listing (for recently_listed detection).

    Returns:
        One of: ``"default"``, ``"financial_services"``, ``"defence_govt"``,
        ``"infrastructure_utility"``, ``"capital_goods"``, ``"commodities_cyclical"``,
        ``"recently_listed"``.
    """
    name = (company_name or ticker or "").lower()
    narrative = moat_narrative.lower()

    # 1. Financial services — highest priority (unique accounting treatment)
    if any(kw in name for kw in _FINANCIAL_KEYWORDS):
        return "financial_services"
    if moat_narrative and any(kw in narrative for kw in _FINANCIAL_MOAT_KEYWORDS):
        return "financial_services"

    # 2. Defence / government contractor
    if any(kw in name for kw in _DEFENCE_KEYWORDS):
        return "defence_govt"
    if moat_narrative and any(kw in narrative for kw in _DEFENCE_MOAT_KEYWORDS):
        return "defence_govt"

    # 3. Infrastructure / utility
    if any(kw in name for kw in _INFRA_UTILITY_KEYWORDS):
        return "infrastructure_utility"

    # 4. Capital goods / engineering
    if any(kw in name for kw in _CAPITAL_GOODS_KEYWORDS):
        return "capital_goods"

    # 5. Commodities / cyclicals
    if any(kw in name for kw in _COMMODITY_KEYWORDS):
        return "commodities_cyclical"

    # 6. Recently listed (fallback — no strong sector signal)
    if listing_years is not None and listing_years < 3.0:
        return "recently_listed"

    return "default"
