"""Exit monitoring for long-term holdings — thesis breaks and valuation exits.

`surveillance` watches *analyses*; this watches *holdings*.  Exits are driven
by the business and its valuation, not by price alone:

  • thesis break  — the latest analysis is REJECT / PEER_SWITCH / GROWTH_REJECT
  • valuation     — live CMP reaches the Step 9 exit ladder (DCF × sector multipliers)
  • sharp fall    — CMP drops below the cap-size review level (the Step 9
                    stop-loss multiplier applied to the investor's average cost).
                    This is a prompt to re-analyse the thesis, never a sell signal:
                    for a long-term holder a fall with the thesis intact can be an
                    opportunity.

Deterministic and side-effect free — the CLI supplies holdings, latest
analyses and live prices.

Analyses saved before the exit ladder was persisted still work: targets are
re-derived from the stored ``dcf_intrinsic_weighted`` and ``sector_name``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.config import settings
from src.portfolio.tracker import add_one_year
from src.sector.profiles import get_sector_profile

# Recommendations whose latest analysis says the investment case no longer holds
BROKEN_THESIS = {"REJECT", "PEER_SWITCH", "GROWTH_REJECT"}

LTCG_WAIT_WINDOW_DAYS = 60  # mention LTCG timing when eligibility is this close

_SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "OK": 0}


@dataclass
class HoldingPosition:
    """One ticker's rolled-up position (quantity-weighted cost across lots)."""

    ticker: str
    quantity: int
    avg_cost: float
    first_buy: str | None = None   # oldest lot's ISO date — FIFO sells it first


@dataclass
class ExitLevels:
    review_price: float        # sharp-fall level: avg cost × stop-loss multiplier
    review_multiplier: float
    trim: float | None = None
    reduce: float | None = None
    full: float | None = None
    dcf: float | None = None
    note: str | None = None   # why the ladder is missing, when it is


@dataclass
class HoldingAlert:
    ticker: str
    severity: str                     # HIGH | MEDIUM | LOW | OK
    action: str                       # headline, e.g. "THESIS BROKEN"
    reasons: list[str] = field(default_factory=list)
    live_cmp: float | None = None
    pnl_pct: float | None = None
    levels: ExitLevels | None = None


def exit_levels(position: HoldingPosition, analysis: dict | None) -> ExitLevels:
    """Sharp-fall review level from actual cost; exit ladder from the latest analysis."""
    cap_size = analysis.get("cap_size") if analysis else None
    multiplier = (analysis or {}).get("stop_loss_multiplier") or settings.stop_loss_multiplier(
        cap_size
    )
    levels = ExitLevels(
        review_price=round(position.avg_cost * multiplier, 2),
        review_multiplier=multiplier,
    )
    if not analysis:
        return levels
    if analysis.get("analysis_mode") == "growth":
        # The growth-mode forward-revenue DCF overstates per-share value by an
        # order of magnitude; exits on it would never fire. Stop-loss only.
        levels.note = "Growth-mode DCF unreliable — exit ladder skipped"
        return levels

    levels.dcf = analysis.get("dcf_intrinsic_weighted")
    levels.trim = analysis.get("exit_trim_price")
    levels.reduce = analysis.get("exit_reduce_price")
    levels.full = analysis.get("exit_full_price")
    dcf = levels.dcf
    if dcf and dcf > 0 and not (levels.trim and levels.reduce and levels.full):
        profile = get_sector_profile(analysis.get("sector_name"))
        levels.trim = round(dcf * profile.exit_mult_1x, 2)
        levels.reduce = round(dcf * profile.exit_mult_2x, 2)
        levels.full = round(dcf * profile.exit_mult_3x, 2)
    return levels


def _ltcg_note(first_buy: str | None, today: date) -> str | None:
    """Tax-timing hint when an exit fires shortly before LTCG eligibility."""
    if not first_buy:
        return None
    try:
        eligible = add_one_year(date.fromisoformat(first_buy))
    except ValueError:
        return None
    days_left = (eligible - today).days
    if 0 < days_left <= LTCG_WAIT_WINDOW_DAYS:
        return (
            f"Oldest lot turns LTCG in {days_left}d ({eligible.isoformat()}) — "
            "STCG 20% vs LTCG 12.5%; weigh waiting before selling"
        )
    return None


def evaluate_holding(
    position: HoldingPosition,
    analysis: dict | None,
    live_cmp: float | None,
    today: date,
    stale_days: int = 30,
) -> HoldingAlert:
    """Classify one holding. Severity is the worst of every rule that fires."""
    levels = exit_levels(position, analysis)
    alert = HoldingAlert(ticker=position.ticker, severity="OK", action="Hold", levels=levels)
    fired: list[tuple[str, str, str]] = []  # (severity, action, reason)

    if live_cmp is None:
        fired.append(("MEDIUM", "No live price", "Live quote unavailable — check manually"))
    else:
        alert.live_cmp = live_cmp
        if position.avg_cost > 0:
            alert.pnl_pct = round((live_cmp - position.avg_cost) / position.avg_cost * 100, 2)

        if live_cmp <= levels.review_price:
            fell = (
                f"CMP ₹{live_cmp:,.2f} is {(1 - live_cmp / position.avg_cost) * 100:.0f}% "
                f"below avg cost ₹{position.avg_cost:,.2f} (review level ₹{levels.review_price:,.2f})"
            )
            analysed_cmp = (analysis or {}).get("cmp")
            rec = (analysis or {}).get("recommendation") or ""
            if rec in BROKEN_THESIS:
                # The thesis-break rule below already says what to do.
                fired.append(("LOW", "Price fall", fell))
            elif analysed_cmp and analysed_cmp <= levels.review_price:
                # The latest analysis already ran at a post-fall price and the
                # thesis held — nothing new to do.
                fired.append((
                    "LOW", "Fall re-checked",
                    f"{fell}; thesis re-checked at ₹{analysed_cmp:,.2f} on "
                    f"{analysis.get('analysis_date')} → {rec}",
                ))
            else:
                fired.append((
                    "MEDIUM", "SHARP FALL",
                    f"{fell} — re-analyse the thesis before deciding "
                    f"(investor analyze {position.ticker})",
                ))

        dcf_ctx = f" ({live_cmp / levels.dcf:.1f}× DCF ₹{levels.dcf:,.0f})" if levels.dcf else ""
        if levels.full and live_cmp >= levels.full:
            fired.append(("HIGH", "FULL EXIT", f"CMP ≥ full-exit ₹{levels.full:,.2f}{dcf_ctx}"))
        elif levels.reduce and live_cmp >= levels.reduce:
            fired.append(("MEDIUM", "REDUCE", f"CMP ≥ reduce ₹{levels.reduce:,.2f}{dcf_ctx}"))
        elif levels.trim and live_cmp >= levels.trim:
            fired.append(("MEDIUM", "TRIM", f"CMP ≥ trim ₹{levels.trim:,.2f}{dcf_ctx}"))

    if analysis is None:
        fired.append(("LOW", "No analysis", "Never analysed — exit targets unknown"))
    else:
        rec = analysis.get("recommendation") or ""
        if rec in BROKEN_THESIS:
            reason = analysis.get("termination_reason") or "failed a pipeline gate"
            fired.append(("HIGH", "THESIS BROKEN", f"Latest analysis: {rec} — {reason}"))
        try:
            age = (today - date.fromisoformat(analysis["analysis_date"])).days
        except (KeyError, TypeError, ValueError):
            age = None
        if age is None or age > stale_days:
            fired.append((
                "LOW", "Stale analysis",
                f"Last analysed {age}d ago — targets may be outdated" if age is not None
                else "Analysis date unknown",
            ))

    if not fired:
        if levels.note:
            alert.reasons.append(levels.note)
        return alert

    fired.sort(key=lambda f: _SEVERITY_RANK[f[0]], reverse=True)
    alert.severity, alert.action = fired[0][0], fired[0][1]
    alert.reasons = [r for _, _, r in fired]
    if levels.note:
        alert.reasons.append(levels.note)

    is_exit = any(a.startswith(("TRIM", "REDUCE", "FULL", "THESIS")) for _, a, _ in fired)
    if is_exit:
        note = _ltcg_note(position.first_buy, today)
        if note:
            alert.reasons.append(note)
    return alert


def rollup_lots(lots: list[dict]) -> list[HoldingPosition]:
    """Aggregate purchase lots into one position per ticker (W.avg cost)."""
    agg: dict[str, HoldingPosition] = {}
    cost: dict[str, float] = {}
    for lot in lots:
        t = lot["ticker"]
        pos = agg.setdefault(t, HoldingPosition(ticker=t, quantity=0, avg_cost=0.0))
        pos.quantity += lot["quantity"]
        cost[t] = cost.get(t, 0.0) + lot["avg_cost"] * lot["quantity"]
        bought = lot.get("purchase_date") or None
        if bought and (pos.first_buy is None or bought < pos.first_buy):
            pos.first_buy = bought
    for t, pos in agg.items():
        pos.avg_cost = round(cost[t] / pos.quantity, 2) if pos.quantity else 0.0
    return sorted(agg.values(), key=lambda p: p.ticker)


def sort_alerts(alerts: list[HoldingAlert]) -> list[HoldingAlert]:
    """Most urgent first, then ticker."""
    return sorted(alerts, key=lambda a: (-_SEVERITY_RANK[a.severity], a.ticker))
