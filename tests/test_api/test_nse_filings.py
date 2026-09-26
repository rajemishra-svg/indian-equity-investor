"""Tests for NSE integrated-filing XBRL parsing and NSEFilingsClient (auditor + RPT)."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from src.api.nse_filings import (
    FilingGovernance,
    NSEFilingsClient,
    RPTRow,
    counted_rpt_total,
    group_entities,
    is_counted_rpt,
    normalise_auditor_names,
    parse_integrated_filing,
    revenue_for,
)

NSE = "https://www.nseindia.com"
ARCHIVE = "https://nsearchives.nseindia.com/corporate/xbrl"
LISTING = f"{NSE}/api/integrated-filing-results"

COMPANY = "Acme Industries Limited"


# ---------------------------------------------------------------------------
# Fixture builders — minimal SEBI in-capmkt integrated-filing instances
# ---------------------------------------------------------------------------


def _ctx(cid: str, start: str, end: str, rpt_member: str | None = None) -> str:
    scenario = ""
    if rpt_member:
        scenario = (
            "<xbrli:scenario><xbrldi:typedMember dimension=\"in-capmkt:RelatedPartyTransactionAxis\">"
            f"<in-capmkt:RelatedPartyTransactionDomain>{rpt_member}</in-capmkt:RelatedPartyTransactionDomain>"
            "</xbrldi:typedMember></xbrli:scenario>"
        )
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        f"</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period>{scenario}</xbrli:context>"
    )


def make_filing(
    *,
    quarter_start: str,
    period_end: str,
    ytd_start: str,
    ytd_revenue: float,
    auditors: tuple[str, ...] = ("B S R & Co. LLP",),
    opinion: str | None = None,
    nature: str = "Standalone",
    rpt_rows: list[dict] | None = None,
    rpt_declared: str | None = None,
    equity: float | None = None,
    fy_context_dates: tuple[str, str] | None = None,
) -> str:
    """Build an integrated-filing XBRL document.

    ``rpt_rows`` items: counterparty, relationship, type, amount, [details], [entity].
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt">',
        _ctx("OneD", quarter_start, period_end),
        # Legacy annual results mis-date FourD with the quarter's dates
        _ctx("FourD", *(fy_context_dates or (ytd_start, period_end))),
        _ctx("MainD", ytd_start, period_end),
        f'<xbrli:context id="OneI"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        f"</xbrli:entity><xbrli:period><xbrli:instant>{period_end}</xbrli:instant></xbrli:period>"
        "</xbrli:context>",
    ]
    for i in range(1, len(auditors) + 1):
        parts.append(_ctx(f"D_Auditor{i}", quarter_start, period_end))
    for i, _ in enumerate(rpt_rows or [], 1):
        parts.append(_ctx(f"D_RelatedPartyTransaction{i}", quarter_start, period_end, f"RPT{i}"))
        parts.append(_ctx(f"D_RelatedPartyTransaction_PY{i}", "2020-01-01", "2020-03-31", f"RPT{i}"))

    parts.append(f'<in-capmkt:NameOfTheCompany contextRef="MainD">{COMPANY}</in-capmkt:NameOfTheCompany>')
    parts.append(
        f'<in-capmkt:NatureOfReportStandaloneConsolidated contextRef="MainD">{nature}'
        "</in-capmkt:NatureOfReportStandaloneConsolidated>"
    )
    parts.append(
        '<in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-5">'
        f"{ytd_revenue / 4:.0f}</in-capmkt:RevenueFromOperations>"
    )
    parts.append(
        '<in-capmkt:RevenueFromOperations contextRef="FourD" unitRef="INR" decimals="-5">'
        f"{ytd_revenue:.0f}</in-capmkt:RevenueFromOperations>"
    )
    if equity is not None:
        parts.append(
            '<in-capmkt:EquityAttributableToOwnersOfParent contextRef="OneI" unitRef="INR" '
            f'decimals="-5">{equity:.0f}</in-capmkt:EquityAttributableToOwnersOfParent>'
        )
    for i, a in enumerate(auditors, 1):
        parts.append(
            f'<in-capmkt:AuditorsFirmName contextRef="D_Auditor{i}">{a.replace("&", "&amp;")}'
            "</in-capmkt:AuditorsFirmName>"
        )
    if opinion:
        parts.append(
            '<in-capmkt:DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification '
            f'contextRef="OneD">{opinion}'
            "</in-capmkt:DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification>"
        )
    if rpt_declared:
        parts.append(
            "<in-capmkt:WhetherTheCompanyHasEnteredIntoAnyRelatedPartyTransactionDuringThePeriod "
            f'contextRef="MainD">{rpt_declared}'
            "</in-capmkt:WhetherTheCompanyHasEnteredIntoAnyRelatedPartyTransactionDuringThePeriod>"
        )
    for i, r in enumerate(rpt_rows or [], 1):
        c = f"D_RelatedPartyTransaction{i}"
        fields = {
            "NameOfListedEntityOrSubsidiaryEnteringIntoTheTransaction": r.get("entity", COMPANY),
            "NameOfCounterParty": r["counterparty"],
            "RelationshipOfTheCounterpartyWithTheListedEntityOrItsSubsidiary": r["relationship"],
            "TypeOfRelatedPartyTransaction": r["type"],
            "DetailsOfOtherRelatedPartyTransaction": r.get("details", ""),
            "ValueOfTheRelatedPartyTransactionAsApprovedByTheAuditCommittee": (
                f"{r['approved']:.0f}" if r.get("approved") else ""
            ),
        }
        for k, v in fields.items():
            if v:
                parts.append(f'<in-capmkt:{k} contextRef="{c}">{v.replace("&", "&amp;")}</in-capmkt:{k}>')
        parts.append(
            f'<in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod contextRef="{c}" '
            f'unitRef="INR" decimals="-3">{r["amount"]:.0f}'
            "</in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod>"
        )
        # Prior-year balance context must be ignored
        parts.append(
            f'<in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod '
            f'contextRef="D_RelatedPartyTransaction_PY{i}" unitRef="INR" decimals="-3">999999999999'
            "</in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod>"
        )
    parts.append("</xbrli:xbrl>")
    return "".join(parts)


CRORE = 1e7


def _row(counterparty, relationship, txn_type, crore, details="", entity=COMPANY,
         approved_crore=None) -> dict:
    return {
        "approved": approved_crore * CRORE if approved_crore else None,
        "counterparty": counterparty,
        "relationship": relationship,
        "type": txn_type,
        "amount": crore * CRORE,
        "details": details,
        "entity": entity,
    }


# H1 (Apr–Sep) disclosure: 100 Cr counted (promoter-entity sales), subsidiary sale excluded
H1_ROWS = [
    _row("Acme Promoter Holdings Pvt Ltd", "Entity under common control", "Sale of goods or services", 100),
    _row("Acme Subsidiary Private Limited", "Subsidiary", "Sale of goods or services", 900),
]
# H2 (Oct–Mar) disclosure: 50 Cr purchases + 50 Cr loan to promoter entity counted;
# interest, KMP pay and a period-end balance excluded
H2_ROWS = [
    _row("Acme Promoter Holdings Pvt Ltd", "Entity under common control", "Purchase of goods or services", 50),
    _row("Acme Promoter Holdings Pvt Ltd", "Entity under common control", "Loan", 50),
    _row("Acme Promoter Holdings Pvt Ltd", "Entity under common control", "Interest received", 5),
    _row("Mr. A. Founder", "Managing Director", "Remuneration", 20),
    _row("Acme Promoter Holdings Pvt Ltd", "Entity under common control", "Any other transaction", 70,
         details="Balance Receivable"),
]

Q2_STANDALONE = make_filing(
    quarter_start="2025-07-01", period_end="2025-09-30", ytd_start="2025-04-01",
    ytd_revenue=900 * CRORE, rpt_rows=H1_ROWS,
)
Q2_CONSOLIDATED = make_filing(
    quarter_start="2025-07-01", period_end="2025-09-30", ytd_start="2025-04-01",
    ytd_revenue=1000 * CRORE, nature="Consolidated",
)
Q4_STANDALONE = make_filing(
    quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
    ytd_revenue=1800 * CRORE, rpt_rows=H2_ROWS, opinion="Declaration of unmodified opinion",
)
Q4_CONSOLIDATED = make_filing(
    quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
    ytd_revenue=2000 * CRORE, nature="Consolidated", opinion="Declaration of unmodified opinion",
    equity=1000 * CRORE,
)
Q1_CONSOLIDATED = make_filing(
    quarter_start="2026-04-01", period_end="2026-06-30", ytd_start="2026-04-01",
    ytd_revenue=550 * CRORE, nature="Consolidated",
    auditors=("S.R. Batliboi & Associates LLP", "Chaturvedi & Shah LLP"),
)


def _listing_row(qe: str, consolidated: str, name: str, broadcast: str, kind: str = "Financials") -> dict:
    return {
        "qe_Date": qe,
        "consolidated": consolidated,
        "type": f"Integrated Filing- {kind}",
        "broadcast_Date": broadcast,
        "xbrl": f"{ARCHIVE}/{name}.xml",
    }


FULL_LISTING = {
    "data": [
        _listing_row("30-JUN-2026", "Consolidated", "q1c", "31-Jul-2026 18:12:59"),
        _listing_row("30-JUN-2026", "None", "gov", "29-Jul-2026 09:14:15", kind="Governance"),
        _listing_row("31-MAR-2026", "Consolidated", "q4c", "12-May-2026 11:38:23"),
        _listing_row("31-MAR-2026", "Standalone", "q4s", "12-May-2026 11:38:16"),
        _listing_row("30-SEP-2025", "Consolidated", "q2c", "17-Oct-2025 09:15:26"),
        _listing_row("30-SEP-2025", "Standalone", "q2s", "17-Oct-2025 09:14:35"),
    ]
}


LEGACY_RPT = f"{NSE}/api/related-party-transactions-master"
ANNUAL_RESULTS = f"{NSE}/api/corporates-financial-results"


def _mock_nse(respx_mock, listing=FULL_LISTING, files=None, legacy=None, annual=None):
    respx_mock.get(f"{NSE}/").mock(return_value=httpx.Response(403, text="denied"))
    respx_mock.get(LISTING).mock(return_value=httpx.Response(200, json=listing))
    respx_mock.get(LEGACY_RPT).mock(return_value=httpx.Response(200, json={"data": legacy or []}))
    respx_mock.get(ANNUAL_RESULTS).mock(return_value=httpx.Response(200, json=annual or []))
    files = files if files is not None else {
        "q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": Q4_STANDALONE,
        "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE,
    }
    for name, body in files.items():
        if isinstance(body, int):
            respx_mock.get(f"{ARCHIVE}/{name}.xml").mock(return_value=httpx.Response(body))
        else:
            respx_mock.get(f"{ARCHIVE}/{name}.xml").mock(
                return_value=httpx.Response(200, content=body.encode())
            )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_extracts_auditor_opinion_revenue_and_rpt_rows():
    facts = parse_integrated_filing(Q4_STANDALONE)
    assert facts.auditors == ["B S R & Co. LLP"]
    assert facts.modified_opinion is False
    assert facts.company_name == COMPANY
    assert facts.nature == "Standalone"
    assert revenue_for(facts, date(2026, 3, 31), 12) == 1800 * CRORE
    assert revenue_for(facts, date(2026, 3, 31), 3) == 450 * CRORE
    assert revenue_for(facts, date(2026, 3, 31), 6) is None
    # Prior-year (_PY) contexts are ignored
    assert len(facts.rpt_rows) == len(H2_ROWS)
    assert all(r.amount_inr < 1e11 for r in facts.rpt_rows)


def test_parse_joint_auditors_joined():
    facts = parse_integrated_filing(Q1_CONSOLIDATED)
    assert normalise_auditor_names(facts.auditors) == (
        "S.R. Batliboi & Associates LLP; Chaturvedi & Shah LLP"
    )


def test_parse_modified_opinion_detected():
    xml = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1, opinion="Statement on impact of audit qualification",
    )
    assert parse_integrated_filing(xml).modified_opinion is True


def test_parse_does_not_expand_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    xml = (
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">'
        '<in-capmkt:AuditorsFirmName contextRef="D_Auditor1">&xxe;</in-capmkt:AuditorsFirmName>'
        "</xbrli:xbrl>"
    )
    facts = parse_integrated_filing(xml)
    assert all("TOP-SECRET" not in a for a in facts.auditors)


# ---------------------------------------------------------------------------
# RPT counting rules
# ---------------------------------------------------------------------------


def _r(relationship, txn_type="Sale of goods or services", details="", counterparty="X Ltd") -> RPTRow:
    return RPTRow(COMPANY, counterparty, relationship, txn_type, details, 1.0)


@pytest.mark.parametrize(
    "relationship",
    ["Subsidiary", "Subsidiaries of TCS", "Wholly Owned Subsidiary", "Step-down subsidiary",
     "Subsidiary of the Company"],
)
def test_own_subsidiaries_excluded(relationship):
    assert is_counted_rpt(_r(relationship), set()) is False


@pytest.mark.parametrize(
    "relationship",
    ["Fellow Subsidiary", "Subsidiaries of Ultimate Holding Company", "Holding Company",
     "Joint Venture", "Associates of Holding Company and their subsidiaries",
     "Wholly Owned Subsidiary of Lightanium technologies Private Limited (Joint venture Company)",
     "Entity under common control", "Related parties of Subsidiaries of TCS"],
)
def test_outside_group_related_parties_counted(relationship):
    assert is_counted_rpt(_r(relationship), set()) is True


@pytest.mark.parametrize(
    "txn_type,details",
    [("Interest received", ""), ("Dividend paid", ""), ("Remuneration", ""),
     ("Any other transaction", "Balance Payable"), ("Any other transaction", "Loan repaid by X"),
     ("Any other transaction", "Loans and advances recovered"),
     ("Any other transaction", "Reimbursement of expenses"),
     ("Any other transaction", "Director sitting fees")],
)
def test_non_revenue_comparable_rows_excluded(txn_type, details):
    assert is_counted_rpt(_r("Entity under common control", txn_type, details), set()) is False


@pytest.mark.parametrize(
    "txn_type,details",
    [("Loan", ""), ("Inter-corporate deposit", ""), ("Investment", ""),
     ("Any other transaction", "Corporate guarantee given"),
     ("Any other transaction", "Brand equity contribution"),
     ("Purchase of fixed assets", "")],
)
def test_funding_and_asset_flows_to_promoter_entities_counted(txn_type, details):
    assert is_counted_rpt(_r("Entity under common control", txn_type, details), set()) is True


def test_benefit_plan_contributions_excluded():
    assert is_counted_rpt(_r("Post Employment Benefit Plans", "Any other transaction",
                             "Contribution to provident fund"), set()) is False


def test_counterparty_in_group_excluded_even_with_other_label():
    """Subsidiary→listed-entity rows and rows re-labelled on the other side eliminate."""
    rows = [
        RPTRow(COMPANY, "Sub One Pvt. Ltd", "Subsidiary", "Sale of goods or services", "", 10.0),
        RPTRow("Sub One Private Limited", COMPANY, "Holding Company", "Sale of goods or services", "", 20.0),
        RPTRow(COMPANY, "Sub One Private Limited", "Entities over which holding company exercises control",
               "Purchase of goods or services", "", 30.0),
        RPTRow(COMPANY, "Promoter Co Ltd", "Entity under common control", "Sale of goods or services", "", 7.0),
    ]
    assert group_entities(rows, COMPANY) >= {"subonepvtltd", "acmeindustriesltd"}
    facts = parse_integrated_filing(make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01", ytd_revenue=1,
    ))
    facts.rpt_rows = rows
    assert counted_rpt_total(facts) == 7.0


# ---------------------------------------------------------------------------
# NSEFilingsClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_full_year_rpt_and_latest_auditor(respx_mock):
    _mock_nse(respx_mock)
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("acme")

    assert isinstance(result, FilingGovernance)
    # Latest filing (Q1 FY27, joint audit) names the current auditors
    assert result.auditor_name == "S.R. Batliboi & Associates LLP; Chaturvedi & Shah LLP"
    assert result.modified_opinion is False
    # Operating: (H1 100 sales + H2 50 purchases) / FY consolidated revenue 2000 = 7.5%
    assert result.rpt_pct_revenue == 7.5
    assert result.rpt_sales_pct_revenue == 5.0
    # Funding: H2 50 loan / consolidated net worth 1000 = 5%
    assert result.rpt_funding_pct_networth == 5.0
    assert result.rpt_over_approval_count == 0
    assert result.rpt_prior_year_pct_revenue is None  # no earlier disclosures on file
    assert result.rpt_fiscal_year == "FY2026"
    assert any("[RPT: operating RPT 7.5% of revenue (FY2026)" in f for f in result.data_flags)
    # Homepage 403 is tolerated; the listing call carries the symbol
    assert respx_mock.calls  # made requests
    listing_calls = [c for c in respx_mock.calls if c.request.url.path == "/api/integrated-filing-results"]
    assert listing_calls[0].request.url.params["symbol"] == "ACME"
    # The listing is paginated — the client must ask for all rows
    assert int(listing_calls[0].request.url.params["size"]) >= 100


@pytest.mark.asyncio
async def test_client_h2_only_is_flagged_estimate(respx_mock):
    listing = {"data": [r for r in FULL_LISTING["data"] if "SEP" not in r["qe_Date"]]}
    _mock_nse(respx_mock, listing=listing)
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    # H2 operating 50 Cr vs half of FY revenue (1000 Cr) = 5%; funding annualised 100/1000
    assert result.rpt_pct_revenue == 5.0
    assert result.rpt_funding_pct_networth == 10.0
    assert result.rpt_fiscal_year == "FY2026 (one half)"
    assert any(f.startswith("[ESTIMATE: rpt_pct_revenue") for f in result.data_flags)


@pytest.mark.asyncio
async def test_client_no_rpt_declared_means_zero(respx_mock):
    no_rpt_q4 = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1800 * CRORE, rpt_declared="No",
    )
    no_rpt_q2 = make_filing(
        quarter_start="2025-07-01", period_end="2025-09-30", ytd_start="2025-04-01",
        ytd_revenue=900 * CRORE, rpt_declared="No",
    )
    _mock_nse(respx_mock, files={"q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": no_rpt_q4,
                                 "q2c": Q2_CONSOLIDATED, "q2s": no_rpt_q2})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    assert result.rpt_pct_revenue == 0.0


@pytest.mark.asyncio
async def test_client_missing_rpt_section_leaves_none(respx_mock):
    """No RPT rows and no 'No RPT' declaration → unknown, not 0%."""
    bare_q4 = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1800 * CRORE,
    )
    bare_q2 = make_filing(
        quarter_start="2025-07-01", period_end="2025-09-30", ytd_start="2025-04-01",
        ytd_revenue=900 * CRORE,
    )
    _mock_nse(respx_mock, files={"q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": bare_q4,
                                 "q2c": Q2_CONSOLIDATED, "q2s": bare_q2})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    assert result.rpt_pct_revenue is None
    assert result.auditor_name  # auditor still sourced
    assert not any(f.startswith("[RPT") for f in result.data_flags)


@pytest.mark.asyncio
async def test_client_one_missing_half_estimates_from_the_other(respx_mock):
    bare_q4 = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1800 * CRORE,
    )
    _mock_nse(respx_mock, files={"q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": bare_q4,
                                 "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    # H1 100 Cr vs half of FY revenue (1000 Cr)
    assert result.rpt_pct_revenue == 10.0
    assert any(f.startswith("[ESTIMATE: rpt_pct_revenue") for f in result.data_flags)


@pytest.mark.asyncio
async def test_client_xbrl_download_failure_degrades_to_none_fields(respx_mock):
    _mock_nse(respx_mock, files={"q1c": 404, "q4c": 404, "q4s": 404, "q2c": 404, "q2s": 404})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    assert result.auditor_name is None
    assert result.rpt_pct_revenue is None


@pytest.mark.asyncio
async def test_client_listing_failure_returns_none(respx_mock):
    respx_mock.get(f"{NSE}/").mock(return_value=httpx.Response(200))
    respx_mock.get(LISTING).mock(return_value=httpx.Response(401))
    async with NSEFilingsClient() as client:
        assert await client.get_filing_governance("ACME") is None


@pytest.mark.asyncio
async def test_client_empty_listing_returns_none(respx_mock):
    _mock_nse(respx_mock, listing={"data": []}, files={})
    async with NSEFilingsClient() as client:
        assert await client.get_filing_governance("ACME") is None


@pytest.mark.asyncio
async def test_client_ignores_non_archive_urls(respx_mock):
    """Only nsearchives.nseindia.com XBRL links are fetched."""
    listing = {"data": [{**_listing_row("31-MAR-2026", "Consolidated", "x", "12-May-2026 11:38:23"),
                         "xbrl": "http://169.254.169.254/latest/meta-data"}]}
    _mock_nse(respx_mock, listing=listing, files={})
    async with NSEFilingsClient() as client:
        assert await client.get_filing_governance("ACME") is None


@pytest.mark.asyncio
async def test_client_revised_filing_supersedes_original(respx_mock):
    revised_q4s = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1800 * CRORE,
        rpt_rows=[_row("Acme Promoter Holdings Pvt Ltd", "Entity under common control",
                       "Purchase of goods or services", 300)],
    )
    listing = {"data": FULL_LISTING["data"] + [
        _listing_row("31-MAR-2026", "Standalone", "q4s_rev", "20-May-2026 10:00:00"),
    ]}
    files = {"q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": Q4_STANDALONE,
             "q4s_rev": revised_q4s, "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE}
    _mock_nse(respx_mock, listing=listing, files=files)
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    # (H1 100 + revised H2 300) / 2000 = 20%
    assert result.rpt_pct_revenue == 20.0


def test_entering_entities_are_group_even_when_mislabelled():
    """HCL-style filings label their own subsidiaries 'Subsidiary of ultimate parent entity'."""
    rows = [
        RPTRow(COMPANY, "Acme America Inc.", "Subsidiary of ultimate parent entity",
               "Sale of goods or services", "", 500.0),
        RPTRow("Acme America Inc.", COMPANY, "Parent", "Purchase of goods or services", "", 500.0),
        RPTRow(COMPANY, "Promoter Co Ltd", "Entity under common control",
               "Sale of goods or services", "", 3.0),
    ]
    facts = parse_integrated_filing(make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01", ytd_revenue=1,
    ))
    facts.rpt_rows = rows
    assert counted_rpt_total(facts) == 3.0


def test_entering_joint_venture_stays_outside_group():
    """A JV that files its own rows (Trent/Booker style) is not intra-group."""
    rows = [
        RPTRow(COMPANY, "Booker JV Ltd", "Reporting Entity's Joint ventures",
               "Sale of goods or services", "", 40.0),
        RPTRow("Booker JV Ltd", "Some Vendor Ltd", "Other related party",
               "Purchase of goods or services", "", 2.0),
    ]
    facts = parse_integrated_filing(make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01", ytd_revenue=1,
    ))
    facts.rpt_rows = rows
    assert counted_rpt_total(facts) == 42.0


@pytest.mark.parametrize("relationship", ["CPSE/Govt. Undertaking", "Government related entity",
                                          "State-owned enterprise"])
def test_government_related_psu_trade_excluded(relationship):
    assert is_counted_rpt(_r(relationship), set()) is False


def test_benefit_trust_labelled_promoter_group_excluded():
    row = _r("Promoter Group", "Any other transaction", "Contribution",
             counterparty="Tata Elxsi (India) Ltd. Employees Provident Fund")
    assert is_counted_rpt(row, set()) is False


# ---------------------------------------------------------------------------
# Prior-year comparison and approvals
# ---------------------------------------------------------------------------

def _legacy_rpt(period_start: str, period_end: str, rows: list[dict]) -> str:
    return make_filing(quarter_start=period_start, period_end=period_end,
                       ytd_start=period_start, ytd_revenue=1, rpt_rows=rows)


PROMOTER = "Acme Promoter Holdings Pvt Ltd"
COMMON = "Entity under common control"

# FY2024: both halves in the legacy format → valid comparison year (60 Cr / 1500 Cr = 4%)
LEGACY_FILES = {
    "rpt_h1fy24": _legacy_rpt("2023-04-01", "2023-09-30", [_row(PROMOTER, COMMON, "Sale of goods or services", 30)]),
    "rpt_h2fy24": _legacy_rpt("2023-10-01", "2024-03-31", [_row(PROMOTER, COMMON, "Sale of goods or services", 30)]),
    # FY2025 H1 legacy — pairs with the integrated Mar-2025 Q4 (mixed formats → skipped)
    "rpt_h1fy25": _legacy_rpt("2024-04-01", "2024-09-30", [_row(PROMOTER, COMMON, "Sale of goods or services", 500)]),
}
LEGACY_LISTING = [
    {"periodEndDate": "30-SEP-2023", "submissionDate": "01-NOV-2023 10:00:00",
     "xbrlLink": f"{ARCHIVE}/rpt_h1fy24.xml"},
    {"periodEndDate": "31-MAR-2024", "submissionDate": "01-MAY-2024 10:00:00",
     "xbrlLink": f"{ARCHIVE}/rpt_h2fy24.xml"},
    {"periodEndDate": "30-SEP-2024", "submissionDate": "01-NOV-2024 10:00:00",
     "xbrlLink": f"{ARCHIVE}/rpt_h1fy25.xml"},
]
# Legacy annual results: FourD holds the FY figure but carries the quarter's dates
FY24_RESULTS = make_filing(
    quarter_start="2024-01-01", period_end="2024-03-31", ytd_start="2023-04-01",
    ytd_revenue=1500 * CRORE, nature="Consolidated",
    fy_context_dates=("2024-01-01", "2024-03-31"),
)
ANNUAL_LISTING = [
    {"toDate": "31-Mar-2024", "consolidated": "Consolidated", "xbrl": f"{ARCHIVE}/fy24c.xml"},
    {"toDate": "31-Mar-2024", "consolidated": "Non-Consolidated", "xbrl": f"{ARCHIVE}/fy24s.xml"},
]
Q4FY25_STANDALONE = make_filing(
    quarter_start="2025-01-01", period_end="2025-03-31", ytd_start="2024-04-01",
    ytd_revenue=1600 * CRORE, rpt_rows=[_row(PROMOTER, COMMON, "Sale of goods or services", 900)],
)
Q4FY25_CONSOLIDATED = make_filing(
    quarter_start="2025-01-01", period_end="2025-03-31", ytd_start="2024-04-01",
    ytd_revenue=1800 * CRORE, nature="Consolidated",
)


@pytest.mark.asyncio
async def test_prior_year_uses_single_format_year_and_skips_transition(respx_mock):
    listing = {"data": FULL_LISTING["data"] + [
        _listing_row("31-MAR-2025", "Consolidated", "q4fy25c", "12-May-2025 10:00:00"),
        _listing_row("31-MAR-2025", "Standalone", "q4fy25s", "12-May-2025 09:00:00"),
    ]}
    files = {
        "q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": Q4_STANDALONE,
        "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE,
        "q4fy25c": Q4FY25_CONSOLIDATED, "q4fy25s": Q4FY25_STANDALONE,
        "fy24c": FY24_RESULTS, "fy24s": 404, **LEGACY_FILES,
    }
    _mock_nse(respx_mock, listing=listing, files=files, legacy=LEGACY_LISTING, annual=ANNUAL_LISTING)
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    # FY2025 mixes legacy H1 with the integrated Q4 (which some filers reported
    # full-year) → skipped; FY2024 = (30 + 30) / 1500 = 4%
    assert result.rpt_prior_year_pct_revenue == 4.0
    assert result.rpt_prior_fiscal_year == "FY2024"
    assert any("FY2024 operating RPT 4.0%" in f for f in result.data_flags)


@pytest.mark.asyncio
async def test_approval_breach_counted_with_materiality(respx_mock):
    q4s = make_filing(
        quarter_start="2026-01-01", period_end="2026-03-31", ytd_start="2025-04-01",
        ytd_revenue=1800 * CRORE,
        rpt_rows=[_row(PROMOTER, COMMON, "Purchase of goods or services", 50, approved_crore=30)],
    )
    _mock_nse(respx_mock, files={"q1c": Q1_CONSOLIDATED, "q4c": Q4_CONSOLIDATED, "q4s": q4s,
                                 "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    assert result.rpt_over_approval_count == 1
    # 20 Cr excess over approval / 2000 Cr revenue = 1%
    assert result.rpt_over_approval_pct_revenue == 1.0


@pytest.mark.asyncio
async def test_standalone_revenue_never_used_when_company_files_consolidated(respx_mock):
    """Consolidated-basis RPT over standalone revenue would inflate the ratio."""
    _mock_nse(respx_mock, files={"q1c": Q1_CONSOLIDATED, "q4c": 404, "q4s": Q4_STANDALONE,
                                 "q2c": Q2_CONSOLIDATED, "q2s": Q2_STANDALONE})
    async with NSEFilingsClient() as client:
        result = await client.get_filing_governance("ACME")
    assert result.rpt_pct_revenue is None
