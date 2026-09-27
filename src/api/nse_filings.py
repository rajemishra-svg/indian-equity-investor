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
* **Counted** — every other row with an outside related party, split in two:

  - *Operating*: sale/purchase of goods, services and fixed assets,
    rent/royalty/brand/consultancy fees. Reported as % of consolidated
    revenue from operations (``rpt_pct_revenue``) — the figure Step 1's
    8/15/20% bands score.
  - *Funding*: loans, inter-corporate deposits, investments, guarantees and
    security given to related parties — the classic siphoning route. Reported
    as % of consolidated net worth (``rpt_funding_pct_networth``), since these
    are balance-sheet flows, not revenue-comparable.

Both are summed over the fiscal year's two half-yearly disclosures (Q2 =
Apr–Sep, Q4 = Oct–Mar). Halves before the integrated format (Sep 2024 and
earlier) come from NSE's standalone Reg 23(9) RPT filings, which use the same
row taxonomy — that is also how the prior-year figure (for the
spike check in Step 1) is built.
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
_BENEFIT_TRUST_NAME = re.compile(
    r"employees?'?\s*(provident|gratuity|superannuation|pension)|provident\s+fund|gratuity\s+(fund|trust)"
    r"|superannuation\s+(fund|trust)",
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
    # Audit-committee approved value for the transaction (None/0 = not stated)
    approved_inr: float | None = None


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
    # Same facts keyed by context id ("OneD" = quarter, "FourD" = year-to-date) —
    # legacy annual-results XBRL mis-dates FourD with the quarter's dates.
    revenue_by_context: dict[str, float] = field(default_factory=dict)
    # Equity attributable to owners (else total equity) keyed by balance-sheet date
    equity_by_instant: dict[date, float] = field(default_factory=dict)
    rpt_rows: list[RPTRow] = field(default_factory=list)


def parse_integrated_filing(xml: bytes | str) -> FilingFacts:
    """Parse an NSE Integrated Filing (Financials) XBRL instance.

    Raises:
        etree.XMLSyntaxError: on malformed XML (caller treats as unavailable).
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = etree.fromstring(xml, parser=_PARSER)

    # context id → (start, end) for duration contexts; → date for instants
    durations: dict[str, tuple[date, date]] = {}
    instants: dict[str, date] = {}
    for ctx in root.iter():
        if not isinstance(ctx.tag, str) or _local(ctx.tag) != "context":
            continue
        start = end = instant = None
        for el in ctx.iter():
            if not isinstance(el.tag, str):
                continue
            name = _local(el.tag)
            if name == "startDate" and el.text:
                start = date.fromisoformat(el.text.strip())
            elif name == "endDate" and el.text:
                end = date.fromisoformat(el.text.strip())
            elif name == "instant" and el.text:
                instant = date.fromisoformat(el.text.strip())
        if start and end:
            durations[ctx.get("id", "")] = (start, end)
        elif instant:
            instants[ctx.get("id", "")] = instant

    facts = FilingFacts()
    rpt: dict[str, dict[str, str]] = {}
    total_equity: dict[date, float] = {}

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
                facts.revenue_by_period.setdefault(durations[ctx], val)
                facts.revenue_by_context[ctx] = val
        elif name == "EquityAttributableToOwnersOfParent" and ctx in instants:
            val = _to_float(text)
            if val is not None:
                facts.equity_by_instant[instants[ctx]] = val
        elif name == "Equity" and ctx in instants:
            val = _to_float(text)
            if val is not None:
                total_equity[instants[ctx]] = val
        elif ctx.startswith("D_RelatedPartyTransaction") and "_PY" not in ctx:
            rpt.setdefault(ctx, {})[name] = text

    for when, val in total_equity.items():
        facts.equity_by_instant.setdefault(when, val)

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
                approved_inr=_to_float(
                    row.get("ValueOfTheRelatedPartyTransactionAsApprovedByTheAuditCommittee")
                ),
            )
        )
    return facts


def is_counted_rpt(row: RPTRow, group_entities: set[str]) -> bool:
    """Whether an RPT row counts toward the RPT-%-of-revenue governance metric."""
    if _is_own_subsidiary(row.relationship or ""):
        return False
    if _GOVERNMENT_RELATED.search(row.relationship or ""):
        return False
    # Benefit trusts are sometimes labelled "Promoter Group" — check the name too.
    if _BENEFIT_PLAN.search(row.relationship or "") or _BENEFIT_TRUST_NAME.search(
        row.counterparty or ""
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


_FUNDING = re.compile(
    r"loan|inter[\s-]*corporate|\bicd\b|deposit|investment|guarantee|security|advance"
    r"|debenture|subscription|share\s+capital|preference\s+share|equity\s+share",
    re.I,
)
_SALE = re.compile(r"^\s*sale\s+of\s+goods", re.I)


def is_funding_rpt(row: RPTRow) -> bool:
    """Loans / ICDs / investments / guarantees / security given — balance-sheet flows."""
    return bool(_FUNDING.search(f"{row.txn_type} {row.details}"))


@dataclass
class RPTBreakdown:
    """Counted RPT for one or more half-yearly disclosures (INR)."""

    operating_inr: float = 0.0
    funding_inr: float = 0.0
    sales_inr: float = 0.0  # subset of operating: sale of goods/services to related parties
    over_approval: int = 0  # counted rows whose amount exceeds the audit-committee approval
    over_approval_excess_inr: float = 0.0  # sum of (amount − approved) on those rows

    def __add__(self, other: RPTBreakdown) -> RPTBreakdown:
        return RPTBreakdown(
            self.operating_inr + other.operating_inr,
            self.funding_inr + other.funding_inr,
            self.sales_inr + other.sales_inr,
            self.over_approval + other.over_approval,
            self.over_approval_excess_inr + other.over_approval_excess_inr,
        )


def rpt_breakdown(facts: FilingFacts) -> RPTBreakdown:
    """Split one disclosure's counted rows into operating vs funding flows."""
    group = group_entities(facts.rpt_rows, facts.company_name)
    out = RPTBreakdown()
    for r in facts.rpt_rows:
        if not is_counted_rpt(r, group):
            continue
        if is_funding_rpt(r):
            out.funding_inr += r.amount_inr
        else:
            out.operating_inr += r.amount_inr
            if _SALE.search(r.txn_type or ""):
                out.sales_inr += r.amount_inr
        if r.approved_inr and r.amount_inr > r.approved_inr * 1.01:
            out.over_approval += 1
            out.over_approval_excess_inr += r.amount_inr - r.approved_inr
    return out


def counted_rpt_total(facts: FilingFacts) -> float:
    """Sum of all counted RPT amounts (operating + funding, INR) for one disclosure."""
    b = rpt_breakdown(facts)
    return b.operating_inr + b.funding_inr


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
    rpt_pct_revenue: float | None = None  # operating RPT, % of revenue
    rpt_fiscal_year: str | None = None  # e.g. "FY2026"
    rpt_funding_pct_networth: float | None = None
    rpt_sales_pct_revenue: float | None = None
    rpt_prior_year_pct_revenue: float | None = None
    rpt_prior_fiscal_year: str | None = None
    rpt_over_approval_count: int | None = None
    rpt_over_approval_pct_revenue: float | None = None  # excess over approvals, % of revenue
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

    async def _annual_revenue(
        self, symbol: str, fy_end: date, consolidated: bool
    ) -> float | None:
        """FY revenue from NSE's pre-integration annual results XBRL."""
        try:
            resp = await self.get(
                "/api/corporates-financial-results",
                params={"index": "equities", "symbol": symbol, "period": "Annual"},
            )
            rows = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            self.log.info("nse_annual_results_listing_failed", symbol=symbol, error=str(exc))
            return None
        wanted = "consolidated" if consolidated else "non-consolidated"
        candidates = []
        for row in rows if isinstance(rows, list) else []:
            try:
                to_date = datetime.strptime(str(row.get("toDate", "")), "%d-%b-%Y").date()
            except ValueError:
                continue
            url = str(row.get("xbrl") or "")
            if to_date != fy_end or not url.startswith("https://nsearchives.nseindia.com/"):
                continue
            nature = str(row.get("consolidated", "")).lower()
            candidates.append((nature == wanted, url))
        # Prefer the matching basis; fall back to the other only if it's all there is.
        for _, url in sorted(candidates, reverse=True):
            facts = await self._fetch_facts(_FilingRef(fy_end, consolidated, "", url))
            if facts is None:
                continue
            revenue = revenue_for(facts, fy_end, 12)
            if revenue is None:
                # Legacy Q4 XBRL: "FourD" holds the full year but carries the
                # quarter's dates; accept it only if it exceeds the quarter figure.
                ytd = facts.revenue_by_context.get("FourD")
                quarter = facts.revenue_by_context.get("OneD")
                if ytd and (quarter is None or ytd > quarter * 1.5):
                    revenue = ytd
            if revenue:
                return revenue
        return None

    async def _legacy_rpt_listing(self, symbol: str) -> dict[date, str]:
        """Half-year end → XBRL URL for pre-integration Reg 23(9) RPT filings."""
        try:
            resp = await self.get(
                "/api/related-party-transactions-master",
                params={"index": "equities", "symbol": symbol},
            )
            rows = resp.json().get("data", [])
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError, AttributeError) as exc:
            self.log.info("nse_legacy_rpt_listing_failed", symbol=symbol, error=str(exc))
            return {}
        out: dict[date, tuple[str, str]] = {}
        for row in rows if isinstance(rows, list) else []:
            url = str(row.get("xbrlLink") or "")
            if not url.startswith("https://nsearchives.nseindia.com/"):
                continue
            try:
                end = datetime.strptime(str(row.get("periodEndDate", "")).title(), "%d-%b-%Y").date()
            except ValueError:
                continue
            submitted = str(row.get("submissionDate", ""))
            if end not in out or submitted > out[end][1]:
                out[end] = (url, submitted)
        return {end: url for end, (url, _) in out.items()}

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
                # The listing is paginated (20 rows by default) — ask for all of it.
                params={"index": "equities", "symbol": symbol, "size": 100},
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

        # Companies with subsidiaries file consolidated results; the RPT numerator is
        # consolidated-basis, so standalone revenue is only a valid denominator for
        # companies that never file consolidated.
        files_consolidated = any(r.consolidated for r in refs)

        async def revenue_facts(period_end: date) -> FilingFacts | None:
            if files_consolidated:
                return await facts_for(find(period_end, True))
            return await facts_for(find(period_end, False))

        result = FilingGovernance()

        # --- Auditor: the most recent filing names the current statutory auditor.
        latest = await facts_for(refs[0])
        if latest and latest.auditors:
            result.auditor_name = normalise_auditor_names(latest.auditors)

        # --- Audit opinion on the latest audited (Q4) results
        q4_ends = sorted(
            {r.period_end for r in refs if (r.period_end.month, r.period_end.day) == (3, 31)},
            reverse=True,
        )
        q2_ends = sorted(
            {r.period_end for r in refs if (r.period_end.month, r.period_end.day) == (9, 30)},
            reverse=True,
        )
        if q4_ends:
            for f in (await facts_for(find(q4_ends[0], True)), await facts_for(find(q4_ends[0], False))):
                if f is None:
                    continue
                if f.modified_opinion:
                    result.modified_opinion = True
                elif f.modified_opinion is False and result.modified_opinion is None:
                    result.modified_opinion = False
                if not result.auditor_name and f.auditors:
                    result.auditor_name = normalise_auditor_names(f.auditors)

        # --- RPT: half-yearly disclosures (Q2 = Apr–Sep, Q4 = Oct–Mar)
        legacy: dict[date, str] | None = None

        async def legacy_rpt_urls() -> dict[date, str]:
            nonlocal legacy
            if legacy is None:
                legacy = await self._legacy_rpt_listing(symbol)
            return legacy

        async def rpt_half(period_end: date) -> tuple[FilingFacts, str] | None:
            """(disclosure carrying RPT rows for the half ending ``period_end``, format)."""
            for consolidated in (False, True):
                f = await facts_for(find(period_end, consolidated))
                if f and (f.rpt_rows or f.rpt_declared is False):
                    return f, "integrated"
            url = (await legacy_rpt_urls()).get(period_end)
            if url:
                f = await facts_for(_FilingRef(period_end, False, "", url))
                if f is not None:
                    return f, "legacy"
            return None

        async def fy_revenue_for(fy_end: date) -> float | None:
            rev_facts = await revenue_facts(fy_end)
            if rev_facts is not None:
                return revenue_for(rev_facts, fy_end, 12)
            # Before the integrated format: NSE's annual results XBRL.
            return await self._annual_revenue(symbol, fy_end, files_consolidated)

        async def fy_figures(
            fy_end: date, *, require_both: bool = False
        ) -> tuple[RPTBreakdown, float, bool] | None:
            """(breakdown, revenue it compares to, estimated?) for one fiscal year.

            A year whose halves come from different formats (the FY2025 switch
            to integrated filing) is rejected with ``require_both``: some filers
            reported the full year in that first integrated Q4 disclosure, so
            adding the legacy H1 would double-count it.
            """
            fy_revenue = await fy_revenue_for(fy_end)
            if not fy_revenue:
                return None
            h2 = await rpt_half(fy_end)
            h1 = await rpt_half(date(fy_end.year - 1, 9, 30))
            if h2 is not None and h1 is not None:
                if h1[1] != h2[1]:
                    return None
                return rpt_breakdown(h1[0]) + rpt_breakdown(h2[0]), fy_revenue, False
            if require_both:
                return None
            if h2 is not None or h1 is not None:
                # One half on file: compare with half the year's revenue.
                return rpt_breakdown((h2 or h1)[0]), fy_revenue / 2, True
            return None

        if q4_ends:
            fy_end = q4_ends[0]
            current = await fy_figures(fy_end)
            if current is not None:
                breakdown, revenue, estimated = current
                label = f"FY{fy_end.year}" + (" (one half)" if estimated else "")
                result.rpt_fiscal_year = label
                result.rpt_pct_revenue = round(breakdown.operating_inr / revenue * 100, 2)
                result.rpt_sales_pct_revenue = round(breakdown.sales_inr / revenue * 100, 2)
                result.rpt_over_approval_count = breakdown.over_approval
                result.rpt_over_approval_pct_revenue = round(
                    breakdown.over_approval_excess_inr / revenue * 100, 3
                )
                if estimated:
                    result.data_flags.append(
                        f"[ESTIMATE: rpt_pct_revenue — only one half-yearly RPT disclosure for "
                        f"FY{fy_end.year} was available; compared against half of FY revenue]"
                    )
                bs = await revenue_facts(fy_end)
                net_worth = bs.equity_by_instant.get(fy_end) if bs else None
                if net_worth and net_worth > 0:
                    funding = breakdown.funding_inr * (2 if estimated else 1)
                    result.rpt_funding_pct_networth = round(funding / net_worth * 100, 2)
            # Comparison year: the most recent earlier FY with both halves in one format.
            for years_back in (1, 2):
                prior_end = date(fy_end.year - years_back, 3, 31)
                prior = await fy_figures(prior_end, require_both=True)
                if prior is not None:
                    p_breakdown, p_revenue, _ = prior
                    result.rpt_prior_year_pct_revenue = round(
                        p_breakdown.operating_inr / p_revenue * 100, 2
                    )
                    result.rpt_prior_fiscal_year = f"FY{prior_end.year}"
                    break
        elif q2_ends:
            h1_end = q2_ends[0]
            h1 = await rpt_half(h1_end)
            rev_facts = await revenue_facts(h1_end)
            h1_revenue = revenue_for(rev_facts, h1_end, 6) if rev_facts else None
            if h1 is not None and h1_revenue:
                b = rpt_breakdown(h1)
                result.rpt_pct_revenue = round(b.operating_inr / h1_revenue * 100, 2)
                result.rpt_sales_pct_revenue = round(b.sales_inr / h1_revenue * 100, 2)
                result.rpt_over_approval_count = b.over_approval
                result.rpt_over_approval_pct_revenue = round(
                    b.over_approval_excess_inr / h1_revenue * 100, 3
                )
                result.rpt_fiscal_year = f"FY{h1_end.year + 1} H1"

        if result.rpt_pct_revenue is not None:
            parts = [f"operating RPT {result.rpt_pct_revenue:.1f}% of revenue ({result.rpt_fiscal_year})"]
            if result.rpt_sales_pct_revenue is not None:
                parts.append(f"sales to related parties {result.rpt_sales_pct_revenue:.1f}% of revenue")
            if result.rpt_funding_pct_networth is not None:
                parts.append(f"funding to related parties {result.rpt_funding_pct_networth:.1f}% of net worth")
            if result.rpt_prior_year_pct_revenue is not None:
                parts.append(
                    f"{result.rpt_prior_fiscal_year} operating RPT {result.rpt_prior_year_pct_revenue:.1f}%"
                )
            result.data_flags.append(
                "[RPT: " + "; ".join(parts) + " — NSE RPT disclosures; excludes intra-group/"
                "interest/dividend/remuneration]"
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
