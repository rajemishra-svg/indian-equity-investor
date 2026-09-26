"""NSE "Integrated Filing — Financials" XBRL: statutory auditor + related-party transactions.

Since the quarter ended Dec 2024, SEBI's integrated filing format carries two
governance facts the pipeline previously had no structured source for:

* ``AuditorsFirmName`` — the statutory auditor (one fact per auditor; joint
  audits file several) and ``DeclarationOfUnmodifiedOpinionOrStatement...`` —
  whether the audited annual results carry a modified (qualified) opinion.
* The half-yearly related-party-transaction (RPT) disclosure under LODR
  Reg 23(9) — one row per counterparty/transaction type, filed with the Q2
  and Q4 results (the standalone document for most companies).

The parsing/arithmetic helpers are pure (no network) so they can be tested
against fixture XML; ``NSEFilingsClient.get_filing_governance`` does the fetching.

RPT % of revenue — what is counted
----------------------------------
The disclosure is made on a consolidated basis: rows are entered into by the
listed entity *or its subsidiaries*. Governance rule "RPT > 20% of revenue
(unexplained)" targets value leaking to promoter-group entities, so:

* **Excluded — intra-group**: rows whose counterparty is the listed entity
  itself or one of its subsidiaries (tagged "Subsidiary" on any row). These
  eliminate on consolidation and are not related-party leakage. Joint
  ventures and associates are *not* part of the group — they are
  equity-accounted, so their transactions stay counted.
* **Excluded — government-related only**: CPSE/PSU counterparties related
  purely through common government control (Ind AS 24 para 25) — ordinary
  commercial trade between state-owned companies, not promoter leakage.
* **Excluded — not revenue-comparable**: interest, dividends, KMP/director
  remuneration and sitting fees, employee-benefit-plan contributions (PF /
  gratuity / superannuation trusts), expense reimbursements, period-end
  balances (receivable/payable rows), and money coming back to the group
  (loan repayments, redemptions, guarantee releases).
* **Counted**: every other row with an outside related party — sale/purchase
  of goods, services and fixed assets, rent/royalty/brand/consultancy fees,
  AND loans, inter-corporate deposits, investments and guarantees extended
  to promoter-group entities (a classic siphoning route; over-counting a
  benign loan is preferred to missing a fund diversion).

The numerator is compared with consolidated revenue from operations for the
same fiscal year (Q2 disclosure = Apr–Sep, Q4 disclosure = Oct–Mar).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from lxml import etree

from src.api.base import BaseHTTPClient

# SEBI in-capmkt taxonomy — namespace URI is versioned by date, so match on
# local names only.
_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, huge_tree=False, recover=False
)

# Relationship is free text ("Subsidiary", "Subsidiaries of TCS", "Wholly owned
# step-down subsidiary", ...). A "subsidiar..." label is the listed entity's own
# subsidiary unless it names someone else's group: "Fellow subsidiary", a
# (ultimate) holding company's or a JV's/associate's subsidiary, or "Related
# parties of subsidiaries" — those are outside the listed group and stay counted.
_SUBSIDIARY_WORD = re.compile(r"subsidiar", re.I)
_OTHER_GROUP = re.compile(
    r"fellow|holding|parent|promoter|joint\s*venture|\bjv\b|venturer|associate"
    r"|related\s+part|investing\s+party|common\s+control",
    re.I,
)
_EQUITY_ACCOUNTED = re.compile(r"joint\s*venture|\bjv\b|associate", re.I)
# Entities related only through common government control (CPSEs/PSUs trading
# with each other) — Ind AS 24 para 25 exempts these from full RPT disclosure;
# they are ordinary commercial trade, not promoter-group leakage.
_GOVERNMENT_RELATED = re.compile(
    r"cpse|psu|govt\.?\s*undertaking|government[\s-]*(related|controlled|undertaking|company|entit)"
    r"|central\s+public\s+sector|state[\s-]*owned|public\s+sector\s+(undertaking|enterprise)",
    re.I,
)
# Post-employment benefit plans (PF / gratuity / superannuation trusts) receive
# statutory employee-benefit contributions, not business flows.
_BENEFIT_PLAN = re.compile(
    r"benefit\s+plan|plan\s+of|provident|gratuity|superannuation|pension", re.I
)


def _is_own_subsidiary(relationship: str) -> bool:
    return bool(_SUBSIDIARY_WORD.search(relationship)) and not _OTHER_GROUP.search(relationship)


_EXCLUDED_TYPES = re.compile(
    r"interest|dividend|remuneration|reimburse", re.I
)
# Free-text details on "Any other transaction" rows that are not
# revenue-comparable (same categories as _EXCLUDED_TYPES).
_EXCLUDED_DETAILS = re.compile(
    r"interest|dividend|remuneration|salary|sitting\s+fee|commission\s+to\s+director"
    r"|reimburs|esop|stock\s+option|share[\s-]*based|contribution\s+to\s+(provident|gratuity|superannuation|post)"
    # Period-end balances are positions, not transactions in the period.
    r"|balance|outstanding|receivable|payable"
    # Money coming *back* (repayments, redemptions, guarantee releases) is not
    # value flowing to the related party.
    r"|repaid|repayment|recover|redemption|release|refund",
    re.I,
)

_NAME_NOISE = re.compile(r"[^a-z0-9]+")
_NAME_SUFFIXES = (
    ("private", "pvt"), ("limited", "ltd"), ("company", "co"),
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _norm_name(name: str) -> str:
    """Normalise an entity name so 'Padget Electronics Pvt. Ltd' == '... Private Limited'."""
    n = name.lower()
    for full, short in _NAME_SUFFIXES:
        n = re.sub(rf"\b{full}\b", short, n)
    return _NAME_NOISE.sub("", n)


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


@dataclass
class RPTRow:
    entity: str
    counterparty: str
    relationship: str
    txn_type: str
    details: str
    amount_inr: float


@dataclass
class FilingFacts:
    """Facts extracted from one integrated-filing XBRL document."""

    auditors: list[str] = field(default_factory=list)
    # True = statement on impact of audit qualification filed (modified opinion);
    # False = declaration of unmodified opinion; None = not stated (unaudited quarter).
    modified_opinion: bool | None = None
    company_name: str | None = None
    # "WhetherTheCompanyHasEnteredIntoAnyRelatedPartyTransactionDuringThePeriod":
    # False lets an empty RPT section mean 0% instead of "not disclosed".
    rpt_declared: bool | None = None
    nature: str | None = None  # "Standalone" / "Consolidated"
    # RevenueFromOperations keyed by (period start, period end)
    revenue_by_period: dict[tuple[date, date], float] = field(default_factory=dict)
    rpt_rows: list[RPTRow] = field(default_factory=list)


def parse_integrated_filing(xml: bytes | str) -> FilingFacts:
    """Parse an NSE Integrated Filing (Financials) XBRL instance.

    Raises:
        etree.XMLSyntaxError: on malformed XML (caller treats as unavailable).
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = etree.fromstring(xml, parser=_PARSER)

    # context id → (start, end) for duration contexts
    durations: dict[str, tuple[date, date]] = {}
    for ctx in root.iter():
        if not isinstance(ctx.tag, str) or _local(ctx.tag) != "context":
            continue
        start = end = None
        for el in ctx.iter():
            if not isinstance(el.tag, str):
                continue
            name = _local(el.tag)
            if name == "startDate" and el.text:
                start = date.fromisoformat(el.text.strip())
            elif name == "endDate" and el.text:
                end = date.fromisoformat(el.text.strip())
        if start and end:
            durations[ctx.get("id", "")] = (start, end)

    facts = FilingFacts()
    rpt: dict[str, dict[str, str]] = {}

    for el in root:
        if not isinstance(el.tag, str):
            continue
        name = _local(el.tag)
        text = (el.text or "").strip()
        ctx = el.get("contextRef", "")

        if name == "AuditorsFirmName" and text:
            if text not in facts.auditors:
                facts.auditors.append(text)
        elif name == "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification" and text:
            low = text.lower()
            if "unmodified" in low:
                facts.modified_opinion = False
            elif "impact of audit qualification" in low or "modified" in low:
                facts.modified_opinion = True
        elif name == "WhetherTheCompanyHasEnteredIntoAnyRelatedPartyTransactionDuringThePeriod" and text:
            facts.rpt_declared = text.strip().lower() in ("yes", "true")
        elif name == "NameOfTheCompany" and text and facts.company_name is None:
            facts.company_name = text
        elif name == "NatureOfReportStandaloneConsolidated" and text and facts.nature is None:
            facts.nature = text
        elif name == "RevenueFromOperations" and ctx in durations:
            val = _to_float(text)
            if val is not None:
                facts.revenue_by_period[durations[ctx]] = val
        elif ctx.startswith("D_RelatedPartyTransaction") and "_PY" not in ctx:
            rpt.setdefault(ctx, {})[name] = text

    for row in rpt.values():
        amount = _to_float(row.get("AmountOfRelatedPartyTransactionDuringTheReportingPeriod"))
        if amount is None:
            continue
        facts.rpt_rows.append(
            RPTRow(
                entity=row.get("NameOfListedEntityOrSubsidiaryEnteringIntoTheTransaction", ""),
                counterparty=row.get("NameOfCounterParty", ""),
                relationship=row.get(
                    "RelationshipOfTheCounterpartyWithTheListedEntityOrItsSubsidiary", ""
                ),
                txn_type=row.get("TypeOfRelatedPartyTransaction", ""),
                details=row.get("DetailsOfOtherRelatedPartyTransaction", ""),
                amount_inr=abs(amount),
            )
        )
    return facts


def is_counted_rpt(row: RPTRow, group_entities: set[str]) -> bool:
    """Whether an RPT row counts toward the RPT-%-of-revenue governance metric."""
    if _is_own_subsidiary(row.relationship or ""):
        return False
    if _BENEFIT_PLAN.search(row.relationship or "") or _GOVERNMENT_RELATED.search(
        row.relationship or ""
    ):
        return False
    if _norm_name(row.counterparty) in group_entities:
        return False
    if _EXCLUDED_TYPES.search(row.txn_type or ""):
        return False
    if row.txn_type.strip().lower() == "any other transaction" and _EXCLUDED_DETAILS.search(
        row.details or ""
    ):
        return False
    return True


def group_entities(rows: list[RPTRow], company_name: str | None) -> set[str]:
    """Normalised names of the listed entity and its subsidiaries.

    Two signals: counterparties labelled as the entity's own subsidiary, and the
    entities *entering into* rows — the field is defined as "listed entity or
    its subsidiary", which catches filers that label their own subsidiaries
    oddly (HCL files them as "Subsidiary of ultimate parent entity"). An
    entering entity that is also labelled a JV/associate as a counterparty
    elsewhere in the filing is treated as outside the group.
    """
    outside = {
        _norm_name(r.counterparty) for r in rows if _EQUITY_ACCOUNTED.search(r.relationship or "")
    }
    group = {_norm_name(r.counterparty) for r in rows if _is_own_subsidiary(r.relationship or "")}
    group |= {_norm_name(r.entity) for r in rows} - outside
    if company_name:
        group.add(_norm_name(company_name))
    group.discard("")
    return group


def counted_rpt_total(facts: FilingFacts) -> float:
    """Sum of counted RPT amounts (INR) for one half-yearly disclosure."""
    group = group_entities(facts.rpt_rows, facts.company_name)
    return sum(r.amount_inr for r in facts.rpt_rows if is_counted_rpt(r, group))


def revenue_for(facts: FilingFacts, period_end: date, months: int) -> float | None:
    """Revenue from operations for the ``months``-long period ending ``period_end``.

    Q4 filings carry the fiscal year (12 months); Q2 filings carry H1 year-to-date (6).
    """
    lo, hi = months * 30 - 10, months * 31 + 2
    for (start, end), val in facts.revenue_by_period.items():
        if end == period_end and lo <= (end - start).days <= hi:
            return val
    return None


def normalise_auditor_names(auditors: list[str]) -> str | None:
    """Join joint auditors into one display string (``_score_audit`` substring-matches it)."""
    cleaned = [re.sub(r"\s+", " ", a).strip() for a in auditors if a and a.strip()]
    return "; ".join(cleaned) if cleaned else None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass
class FilingGovernance:
    """Governance facts sourced from NSE integrated filings, ready to merge."""

    auditor_name: str | None = None
    modified_opinion: bool | None = None
    rpt_pct_revenue: float | None = None
    rpt_fiscal_year: str | None = None  # e.g. "FY2026"
    data_flags: list[str] = field(default_factory=list)


@dataclass
class _FilingRef:
    period_end: date
    consolidated: bool
    broadcast: str
    xbrl_url: str


def _parse_listing(payload: object) -> list[_FilingRef]:
    """Pick "Integrated Filing- Financials" rows out of the NSE listing JSON."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    refs: dict[tuple[date, bool], _FilingRef] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "financials" not in str(row.get("type", "")).lower():
            continue
        url = row.get("xbrl")
        if not url or not str(url).startswith("https://nsearchives.nseindia.com/"):
            continue
        try:
            period_end = datetime.strptime(str(row.get("qe_Date", "")).title(), "%d-%b-%Y").date()
        except ValueError:
            continue
        consolidated = str(row.get("consolidated", "")).lower() == "consolidated"
        ref = _FilingRef(period_end, consolidated, str(row.get("broadcast_Date", "")), str(url))
        key = (period_end, consolidated)
        # A revised filing supersedes the original for the same quarter.
        if key not in refs or _broadcast_ts(ref) > _broadcast_ts(refs[key]):
            refs[key] = ref
    return sorted(refs.values(), key=lambda r: r.period_end, reverse=True)


def _broadcast_ts(ref: _FilingRef) -> datetime:
    try:
        return datetime.strptime(ref.broadcast, "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return datetime.min


class NSEFilingsClient(BaseHTTPClient):
    """Fetches NSE integrated-filing XBRL for auditor and RPT data.

    Kept separate from ``NSEClient`` because it needs different transport
    settings: NSE serves brotli bodies (undecodable without the optional
    ``brotli`` package, so only gzip/deflate are advertised), and the
    homepage cookie visit is best-effort — it frequently 403s while the
    filings API itself still answers.
    """

    def __init__(self) -> None:
        super().__init__(base_url="https://www.nseindia.com")
        self._session_attempted = False

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers.update(
            {
                "Referer": "https://www.nseindia.com/companies-listing/corporate-integrated-filing",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        return headers

    async def _warm_session(self) -> None:
        if self._session_attempted:
            return
        self._session_attempted = True
        try:
            await self.get("/")
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            self.log.debug("nse_filings_session_warmup_failed", error=str(exc))

    async def _fetch_facts(self, ref: _FilingRef) -> FilingFacts | None:
        try:
            resp = await self.get(ref.xbrl_url)
            return parse_integrated_filing(resp.content)
        except (httpx.HTTPStatusError, httpx.RequestError, etree.XMLSyntaxError, ValueError) as exc:
            self.log.warning("nse_filing_xbrl_failed", url=ref.xbrl_url, error=str(exc))
            return None

    async def get_filing_governance(self, symbol: str) -> FilingGovernance | None:
        """Auditor, audit-opinion type and RPT % of revenue from NSE integrated filings.

        Returns None when the listing itself is unavailable; individual fields
        stay None (with a flag) when the filings don't support them.
        """
        symbol = symbol.upper().strip()
        await self._warm_session()
        try:
            resp = await self.get(
                "/api/integrated-filing-results",
                params={"index": "equities", "symbol": symbol},
            )
            refs = _parse_listing(resp.json())
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.warning("nse_filings_listing_failed", symbol=symbol, error=str(exc))
            return None
        if not refs:
            self.log.info("nse_filings_none", symbol=symbol)
            return None

        cache: dict[str, FilingFacts | None] = {}

        async def facts_for(ref: _FilingRef | None) -> FilingFacts | None:
            if ref is None:
                return None
            if ref.xbrl_url not in cache:
                cache[ref.xbrl_url] = await self._fetch_facts(ref)
            return cache[ref.xbrl_url]

        def find(period_end: date, consolidated: bool) -> _FilingRef | None:
            return next(
                (r for r in refs if r.period_end == period_end and r.consolidated == consolidated),
                None,
            )

        result = FilingGovernance()

        # --- Auditor: the most recent filing names the current statutory auditor.
        latest = await facts_for(refs[0])
        if latest and latest.auditors:
            result.auditor_name = normalise_auditor_names(latest.auditors)

        # --- RPT: half-yearly disclosures ride on the Q2 (Sep) and Q4 (Mar) filings.
        q4_ends = sorted({r.period_end for r in refs if (r.period_end.month, r.period_end.day) == (3, 31)}, reverse=True)
        q2_ends = sorted({r.period_end for r in refs if (r.period_end.month, r.period_end.day) == (9, 30)}, reverse=True)

        async def rpt_half(period_end: date) -> tuple[FilingFacts | None, FilingFacts | None]:
            """(facts carrying the RPT section, consolidated facts for revenue)."""
            standalone = await facts_for(find(period_end, False))
            consolidated = await facts_for(find(period_end, True))
            for f in (standalone, consolidated):
                if f and (f.rpt_rows or f.rpt_declared is False):
                    return f, consolidated or standalone
            return None, consolidated or standalone

        if q4_ends:
            fy_end = q4_ends[0]
            h2_rpt, fy_rev_facts = await rpt_half(fy_end)
            if fy_rev_facts and fy_rev_facts.modified_opinion is not None:
                result.modified_opinion = fy_rev_facts.modified_opinion
            standalone_q4 = await facts_for(find(fy_end, False))  # cached by rpt_half
            if standalone_q4 and standalone_q4.modified_opinion:
                result.modified_opinion = True
            if not result.auditor_name:
                for f in (fy_rev_facts, standalone_q4):
                    if f and f.auditors:
                        result.auditor_name = normalise_auditor_names(f.auditors)
                        break

            fy_revenue = revenue_for(fy_rev_facts, fy_end, 12) if fy_rev_facts else None
            if h2_rpt is not None and fy_revenue:
                h1_end = date(fy_end.year - 1, 9, 30)
                h1_rpt = (await rpt_half(h1_end))[0] if h1_end in q2_ends else None
                label = f"FY{fy_end.year}"
                if h1_rpt is not None:
                    numerator = counted_rpt_total(h1_rpt) + counted_rpt_total(h2_rpt)
                    result.rpt_pct_revenue = round(numerator / fy_revenue * 100, 2)
                else:
                    # Only the Oct–Mar disclosure is on file: compare with half the year.
                    numerator = counted_rpt_total(h2_rpt)
                    result.rpt_pct_revenue = round(numerator / (fy_revenue / 2) * 100, 2)
                    label += " H2"
                    result.data_flags.append(
                        f"[ESTIMATE: rpt_pct_revenue — only the {label} RPT disclosure was "
                        "available; compared against half of fiscal-year revenue]"
                    )
                result.rpt_fiscal_year = label
        elif q2_ends:
            h1_end = q2_ends[0]
            h1_rpt, rev_facts = await rpt_half(h1_end)
            h1_revenue = revenue_for(rev_facts, h1_end, 6) if rev_facts else None
            if h1_rpt is not None and h1_revenue:
                result.rpt_pct_revenue = round(counted_rpt_total(h1_rpt) / h1_revenue * 100, 2)
                result.rpt_fiscal_year = f"FY{h1_end.year + 1} H1"

        if result.rpt_pct_revenue is not None:
            result.data_flags.append(
                f"[RPT {result.rpt_pct_revenue:.1f}% of revenue ({result.rpt_fiscal_year}) — "
                "NSE integrated-filing RPT disclosure; counts non-group related-party "
                "trade, asset and funding flows, excludes intra-group/interest/dividend/"
                "remuneration]"
            )

        self.log.info(
            "nse_filing_governance_parsed",
            symbol=symbol,
            auditor=result.auditor_name,
            modified_opinion=result.modified_opinion,
            rpt_pct=result.rpt_pct_revenue,
            rpt_period=result.rpt_fiscal_year,
            filings_fetched=len(cache),
        )
        return result
