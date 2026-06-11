"""Fundamental drift detection between consecutive data snapshots.

`investor surveillance` already catches price drift and staleness; this module
catches what actually breaks a thesis — deteriorating *fundamentals* between
the two most recent stored snapshots of a tracked ticker.  Deterministic, no
LLM calls, no extra HTTP: it reads the snapshots the pipeline already saves.

Only deteriorations alert (improvements are not actionable for a holder).
Severity is HIGH when the new value breaches a pipeline hard-trigger level —
i.e. the stock would now fail a gate it previously passed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.db.repository import get_latest_two_snapshots
from src.logging_config import get_logger

log = get_logger("monitor")


@dataclass(frozen=True)
class _DeltaRule:
    data_type: str                 # 'financials' | 'governance'
    field: str
    label: str
    worsens_when: str              # 'up' | 'down'
    min_change: float              # alert threshold on |new - old|
    hard_breach: Callable[[float], bool] | None = None  # new value breaks a gate
    unit: str = "pp"


# Thresholds chosen to clear normal quarter-to-quarter noise; hard_breach
# levels mirror the Step 1 immediate triggers and Step 3 hard triggers.
_RULES: tuple[_DeltaRule, ...] = (
    _DeltaRule(
        "governance", "promoter_pledging_pct", "Promoter pledging", "up", 0.5,
        hard_breach=lambda v: v > 10.0,  # Step 1 immediate-reject level
    ),
    _DeltaRule(
        "governance", "promoter_holding_pct", "Promoter holding", "down", 1.0,
    ),
    _DeltaRule(
        "financials", "roce_5y_avg", "ROCE 5Y avg", "down", 2.0,
    ),
    _DeltaRule(
        "financials", "roe_5y_avg", "ROE 5Y avg", "down", 2.0,
    ),
    _DeltaRule(
        "financials", "cfo_net_profit_3y_avg", "CFO/NP 3Y", "down", 10.0,
        hard_breach=lambda v: v < 50.0,  # Step 3 hard-trigger level
    ),
    _DeltaRule(
        "financials", "debt_to_equity", "Debt/Equity", "up", 0.3,
        hard_breach=lambda v: v > 3.0,   # Step 3 hard-trigger level
        unit="x",
    ),
    _DeltaRule(
        "financials", "interest_coverage", "Interest coverage", "down", 1.0,
        hard_breach=lambda v: v < 3.0,   # Step 3 hard-trigger level
        unit="x",
    ),
    _DeltaRule(
        "financials", "gnpa_pct", "Gross NPA", "up", 0.5,  # banks/NBFCs
    ),
)


@dataclass
class FundamentalDelta:
    """One deteriorated metric between two snapshots of the same ticker."""

    ticker: str
    field: str
    label: str
    old_value: float
    new_value: float
    change: float
    old_date: str
    new_date: str
    severity: str  # 'HIGH' | 'MEDIUM'
    message: str


def compute_deltas(
    ticker: str,
    data_type: str,
    old_date: str,
    old: dict,
    new_date: str,
    new: dict,
) -> list[FundamentalDelta]:
    """Apply every matching rule to an (old, new) snapshot pair."""
    alerts: list[FundamentalDelta] = []
    for rule in _RULES:
        if rule.data_type != data_type:
            continue
        old_value = old.get(rule.field)
        new_value = new.get(rule.field)
        if not isinstance(old_value, (int, float)) or not isinstance(new_value, (int, float)):
            continue
        change = new_value - old_value
        worsened = (
            change >= rule.min_change
            if rule.worsens_when == "up"
            else change <= -rule.min_change
        )
        if not worsened:
            continue
        severity = (
            "HIGH" if rule.hard_breach is not None and rule.hard_breach(new_value) else "MEDIUM"
        )
        message = (
            f"{rule.label}: {old_value:.1f} → {new_value:.1f} "
            f"({change:+.1f}{rule.unit}, {old_date} → {new_date})"
        )
        if severity == "HIGH":
            message += " — now at pipeline hard-trigger level"
        alerts.append(
            FundamentalDelta(
                ticker=ticker,
                field=rule.field,
                label=rule.label,
                old_value=float(old_value),
                new_value=float(new_value),
                change=round(change, 2),
                old_date=old_date,
                new_date=new_date,
                severity=severity,
                message=message,
            )
        )
    return alerts


async def scan_fundamental_drift(
    db_path: str, tickers: list[str]
) -> dict[str, list[FundamentalDelta]]:
    """Diff the two most recent financials/governance snapshots per ticker.

    Tickers with fewer than two snapshots of a data type are skipped for that
    type (nothing to compare).  Returns only tickers that have alerts.
    """
    drift: dict[str, list[FundamentalDelta]] = {}
    for ticker in tickers:
        alerts: list[FundamentalDelta] = []
        for data_type in ("financials", "governance"):
            snapshots = await get_latest_two_snapshots(db_path, ticker, data_type)
            if len(snapshots) < 2:
                continue
            (new_date, new_data), (old_date, old_data) = snapshots
            if new_date == old_date:
                continue
            alerts.extend(
                compute_deltas(ticker, data_type, old_date, old_data, new_date, new_data)
            )
        if alerts:
            # HIGH first, then by magnitude of change
            alerts.sort(key=lambda a: (a.severity != "HIGH", -abs(a.change)))
            drift[ticker] = alerts
            log.info(
                "fundamental_drift_detected",
                ticker=ticker,
                alerts=len(alerts),
                high=sum(1 for a in alerts if a.severity == "HIGH"),
            )
    return drift
