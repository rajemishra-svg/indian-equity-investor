"""Tests for sector classifier."""
from __future__ import annotations

import pytest

from src.sector.classifier import classify_sector

# ---------------------------------------------------------------------------
# Financial services detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,ticker,expected", [
    ("Bajaj Finance Limited", "BAJFINANCE", "financial_services"),
    ("ICICI Bank", "ICICIBANK", "financial_services"),
    ("Canara HSBC Life Insurance", "CANHLIFE", "financial_services"),
    ("Canara Robeco AMC", "CRAMC", "financial_services"),       # "canara" alone not in keywords; AMC?
    ("PNB Housing Finance", "PNBHOUSING", "financial_services"),
    ("Tata Capital", "TATACAP", "financial_services"),          # "tata capital" keyword added
    ("Muthoot Finance", "MUTHOOTFIN", "financial_services"),
    ("Manappuram Finance", "MANAPPURAM", "financial_services"),
])
def test_financial_services_classification(name, ticker, expected):
    assert classify_sector(name, ticker) == expected


def test_financial_sector_from_moat_narrative():
    """Moat narrative with NBFC / NPA keywords should map to financial_services."""
    result = classify_sector(
        "XYZ Holdings Ltd", "XYZHOLD",
        moat_narrative="This NBFC has strong credit portfolio with low NPA ratios."
    )
    assert result == "financial_services"


# ---------------------------------------------------------------------------
# Defence / govt contractor detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,ticker,expected", [
    ("Bharat Electronics Limited", "BEL", "defence_govt"),
    ("Garden Reach Shipbuilders & Engineers", "GRSE", "defence_govt"),
    ("Zen Technologies", "ZENTEC", "defence_govt"),
    ("HBL Engineering", "HBLENGINE", "defence_govt"),
    ("Mazagon Dock Shipbuilders", "MAZDOCK", "defence_govt"),
    ("Hindustan Aeronautics Limited", "HAL", "defence_govt"),
    ("Bharat Dynamics Limited", "BDL", "defence_govt"),
    ("Data Patterns (India)", "DATAPATT", "defence_govt"),
])
def test_defence_govt_classification(name, ticker, expected):
    assert classify_sector(name, ticker) == expected


def test_defence_from_moat_narrative():
    """Moat narrative mentioning MoD contracts maps to defence_govt."""
    result = classify_sector(
        "ABC Engineering Ltd", "ABCENG",
        moat_narrative="The company holds classified contracts with the Ministry of Defence."
    )
    assert result == "defence_govt"


# ---------------------------------------------------------------------------
# Infrastructure / utility detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,ticker,expected", [
    ("Tata Power Company", "TATAPOWER", "infrastructure_utility"),
    ("Oil India Limited", "OIL", "commodities_cyclical"),     # OIL is oil/commodity
    ("Adani Ports & SEZ", "ADANIPORTS", "infrastructure_utility"),
    ("UTL Solar", "UTLSOLAR", "infrastructure_utility"),       # "solar" keyword
    ("NTPC Limited", "NTPC", "infrastructure_utility"),
])
def test_infrastructure_utility_classification(name, ticker, expected):
    assert classify_sector(name, ticker) == expected


# ---------------------------------------------------------------------------
# Capital goods detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,ticker,expected", [
    ("Voltamp Transformers", "VOLTAMP", "capital_goods"),
    ("Triveni Turbine", "TRITURBINE", "capital_goods"),
    ("Voltamp Transformers", "VOLTAMP2", "capital_goods"),      # duplicate check fine
    # KEI Industries (wires & cables) — no capital_goods keyword match → default
    ("Thermax Limited", "THERMAX", "capital_goods"),
    ("Siemens India", "SIEMENS", "capital_goods"),
])
def test_capital_goods_classification(name, ticker, expected):
    result = classify_sector(name, ticker)
    assert result == expected


# ---------------------------------------------------------------------------
# Commodities / cyclical detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,ticker,expected", [
    ("Oil & Natural Gas Corporation", "ONGC", "commodities_cyclical"),   # "oil & natural gas" keyword
    ("Tata Steel", "TATASTEEL", "commodities_cyclical"),
    ("Hindalco Industries", "HINDALCO", "commodities_cyclical"),
])
def test_commodities_classification(name, ticker, expected):
    assert classify_sector(name, ticker) == expected


# ---------------------------------------------------------------------------
# Recently listed detection
# ---------------------------------------------------------------------------

def test_recently_listed_under_3_years():
    result = classify_sector("LG Electronics India", "LGEINDIA", listing_years=0.5)
    assert result == "recently_listed"


def test_recently_listed_exactly_3_years():
    """Exactly 3 years → NOT recently_listed (must be < 3.0)."""
    result = classify_sector("Unknown Corp", "UNKNCO", listing_years=3.0)
    assert result == "default"


def test_recently_listed_over_3_years():
    result = classify_sector("Unknown Corp", "UNKNCO", listing_years=5.0)
    assert result == "default"


# ---------------------------------------------------------------------------
# Default / no match
# ---------------------------------------------------------------------------

def test_default_for_unrecognised_company():
    result = classify_sector("Generic Manufacturing Co", "GENMAN")
    assert result == "default"


def test_empty_company_name_uses_ticker():
    result = classify_sector("", "ADANIPORTS")
    assert result == "infrastructure_utility"


def test_moat_narrative_does_not_override_financial_name():
    """If name already classifies as financial, moat narrative should not matter."""
    result = classify_sector(
        "HDFC Bank", "HDFCBANK",
        moat_narrative="defence contracts and military systems"
    )
    assert result == "financial_services"
