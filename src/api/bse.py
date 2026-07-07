"""BSE India API client for shareholding patterns.

BSE retired the old ``fetchCompanyCode`` and ``shareHoldingPattrn`` endpoints —
both now 301/302-redirect to HTML pages, which is why every call used to fail
with "Expecting value: line 1 column 1". The current Angular site uses three
endpoints, none of which require session cookies (browser-like UA + Referer
from ``_default_headers`` is sufficient):

1. ``PeerSmartSearch/w?Type=EQ&text=<ticker>`` — scripcode lookup. Returns a
   JSON-encoded *string* of HTML ``<li>`` items; each item carries
   ``liclick('<scripcode>', '<company>')`` and a
   ``<span>SYMBOL&nbsp;ISIN&nbsp;SCRIPCODE</span>``.
2. ``SHPQNewFormat/w?scripcode=<code>`` — shareholding filing archive, newest
   quarter first, each row carrying a numeric ``qtrid``.
3. ``Corp_shpSec_SHPSUMMARY_ng/w?scripcode=<code>&qtrcode=<qtrid>`` — category
   summary for one quarter; the promoter row carries holding % and pledge %.
"""
from __future__ import annotations

import asyncio
import html
import re

import structlog

from src.api.base import BaseHTTPClient
from src.models import GovernanceData

_log = structlog.get_logger("BSEClient")

# ── Module-level circuit breaker ─────────────────────────────────────────────
# When BSE blocks or breaks, every ticker in a 500-ticker batch scan pays the
# failed round-trips and emits its own warnings. After this many *consecutive*
# infrastructure failures (network errors / non-JSON responses — a clean
# "ticker not listed on BSE" does not count), BSE is skipped for the rest of
# the process and callers fall straight through to the Screener fallback.
CIRCUIT_BREAKER_THRESHOLD = 10

# Quarters of shareholding history fetched per ticker (1 summary call each).
# Two is the minimum for a pledging trend direction; four gives a year of
# trend without hammering BSE during batch scans.
_TREND_QUARTERS = 4

_consecutive_failures = 0
_circuit_open = False


def is_circuit_open() -> bool:
    """True once the breaker has tripped — BSE is skipped for this process."""
    return _circuit_open


def reset_circuit_breaker() -> None:
    """Reset breaker state (used in tests)."""
    global _consecutive_failures, _circuit_open
    _consecutive_failures = 0
    _circuit_open = False


def _record_failure() -> None:
    global _consecutive_failures, _circuit_open
    _consecutive_failures += 1
    if not _circuit_open and _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open = True
        _log.warning(
            "bse_circuit_breaker_open",
            consecutive_failures=_consecutive_failures,
            detail=(
                "BSE shareholding API failing repeatedly — skipping BSE for the "
                "remainder of this process; shareholding falls back to Screener"
            ),
        )


def _record_success() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


class BSEClient(BaseHTTPClient):
    """Client for BSE India shareholding pattern API."""

    def __init__(self) -> None:
        super().__init__(base_url="https://api.bseindia.com")

    def _default_headers(self) -> dict:
        headers = super()._default_headers()
        headers.update(
            {
                "Origin": "https://www.bseindia.com",
                "Referer": "https://www.bseindia.com/",
            }
        )
        return headers

    async def _get_scripcode(self, ticker: str) -> str | None:
        """Look up BSE scripcode for an NSE ticker symbol.

        Returns None when the search succeeds but no exact symbol match exists
        (ticker not listed on BSE). Infrastructure failures (network errors,
        non-JSON payloads) propagate so the caller can count them toward the
        circuit breaker.
        """
        resp = await self.get(
            "/BseIndiaAPI/api/PeerSmartSearch/w",
            params={"Type": "EQ", "text": ticker},
        )
        payload = resp.json()
        if not isinstance(payload, str):
            raise ValueError(
                f"unexpected PeerSmartSearch payload type: {type(payload).__name__}"
            )
        return self._parse_scripcode_search(payload, ticker)

    @staticmethod
    def _parse_scripcode_search(li_html: str, ticker: str) -> str | None:
        """Extract the scripcode whose symbol exactly matches the ticker.

        Each result item looks like:
        ``<li ... onclick="liclick('500325','Reliance Industries Ltd')">
        <a><strong>RELIANCE</strong> INDUSTRIES LTD<br />
        <span><strong>RELIANCE</strong>&nbsp;&nbsp;&nbsp;INE002A01018&nbsp;&nbsp;&nbsp;500325</span></a></li>``
        """
        for item in re.finditer(
            r"liclick\('(\d+)'.*?<span>(.*?)</span>", li_html, re.DOTALL
        ):
            scripcode, span = item.groups()
            # Strip tags (the <strong> highlight may split the symbol), then
            # decode entities so &nbsp; separators become splittable whitespace.
            text = html.unescape(re.sub(r"<[^>]+>", "", span))
            tokens = text.replace("\xa0", " ").split()
            if tokens and tokens[0].upper() == ticker:
                return scripcode
        return None

    async def _get_quarter_ids(self, scripcode: str) -> list[int]:
        """Return shareholding filing quarter IDs, newest first."""
        resp = await self.get(
            "/BseIndiaAPI/api/SHPQNewFormat/w", params={"scripcode": scripcode}
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(
                f"unexpected SHPQNewFormat payload type: {type(data).__name__}"
            )
        quarters: list[int] = []
        for row in data.get("Table", []) or []:
            try:
                quarters.append(int(float(row["qtrid"])))
            except (KeyError, TypeError, ValueError):
                continue
        return quarters

    async def _get_quarter_summary(
        self, scripcode: str, qtrid: int
    ) -> tuple[float | None, float | None]:
        """Return (promoter_holding_pct, promoter_pledging_pct) for one quarter."""
        resp = await self.get(
            "/BseIndiaAPI/api/Corp_shpSec_SHPSUMMARY_ng/w",
            params={"scripcode": scripcode, "qtrcode": qtrid},
        )
        data = resp.json()
        holding: float | None = None
        pledging: float | None = None
        rows = data.get("Table1", []) or [] if isinstance(data, dict) else []
        for row in rows:
            category = str(row.get("Fld_ShortCatg", "")).lower()
            if "promoter" in category and "non promoter" not in category:
                try:
                    holding = float(row["Fld_TotalPercentageOf_A_B_C2"])
                except (KeyError, TypeError, ValueError):
                    pass
                try:
                    pledging = float(row["Fld_PledgeEncumberedPercentage"])
                except (KeyError, TypeError, ValueError):
                    pass
                break
        return holding, pledging

    async def get_shareholding(self, ticker: str) -> GovernanceData | None:
        """Fetch promoter holding and pledging data from BSE.

        Fetches the latest quarters (newest first) to establish the pledging
        trend. Returns None on any failure so callers fall back to Screener.
        """
        if _circuit_open:
            self.log.debug("bse_skipped_circuit_open", ticker=ticker)
            return None

        ticker = ticker.upper().strip()
        try:
            scripcode = await self._get_scripcode(ticker)
            if not scripcode:
                _record_success()  # API answered — ticker just isn't listed on BSE
                self.log.info("bse_scripcode_not_found", ticker=ticker)
                return None

            quarter_ids = await self._get_quarter_ids(scripcode)
            if not quarter_ids:
                _record_success()
                self.log.info(
                    "bse_no_shareholding_filings", ticker=ticker, scripcode=scripcode
                )
                return None

            summaries = await asyncio.gather(
                *(
                    self._get_quarter_summary(scripcode, qtrid)
                    for qtrid in quarter_ids[:_TREND_QUARTERS]
                ),
                return_exceptions=True,
            )
            if isinstance(summaries[0], BaseException):
                # The latest quarter is the headline number — without it the
                # result is useless; treat as a full BSE failure.
                raise summaries[0]
        except Exception as exc:
            _record_failure()
            self.log.warning("bse_shareholding_failed", ticker=ticker, error=str(exc))
            return None

        _record_success()
        return self._build_governance(ticker, scripcode, summaries)

    def _build_governance(
        self,
        ticker: str,
        scripcode: str,
        summaries: list[tuple[float | None, float | None] | BaseException],
    ) -> GovernanceData | None:
        """Assemble GovernanceData from per-quarter (holding, pledging) tuples.

        ``summaries`` is newest-first; older quarters that errored are dropped.
        """
        per_quarter = [s for s in summaries if not isinstance(s, BaseException)]
        promoter_holding, promoter_pledging = per_quarter[0]

        if promoter_holding is None and promoter_pledging is None:
            # Filing exists but no promoter row was parseable — return None so
            # the Screener fallback gets a chance at real numbers.
            self.log.info(
                "bse_promoter_row_missing", ticker=ticker, scripcode=scripcode
            )
            return None

        # Trend lists are chronological: oldest first, latest last.
        pledging_trend = [
            pledge for _holding, pledge in reversed(per_quarter) if pledge is not None
        ]
        trend_direction: str | None = None
        if len(pledging_trend) >= 2:
            if pledging_trend[-1] > pledging_trend[0]:
                trend_direction = "increasing"
            elif pledging_trend[-1] < pledging_trend[0]:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"

        flags: list[str] = []
        if promoter_holding is None:
            flags.append("[DATA UNVERIFIED: promoter_holding]")
        if promoter_pledging is None:
            flags.append("[PLEDGING UNKNOWN]")
            promoter_pledging = 0.0

        self.log.info(
            "bse_shareholding_parsed",
            ticker=ticker,
            scripcode=scripcode,
            promoter_holding=promoter_holding,
            promoter_pledging=promoter_pledging,
            trend_quarters=len(pledging_trend),
        )

        return GovernanceData(
            promoter_holding_pct=promoter_holding,
            promoter_pledging_pct=promoter_pledging,
            promoter_pledging_trend=pledging_trend,
            pledging_trend_direction=trend_direction,
            data_flags=flags,
        )
