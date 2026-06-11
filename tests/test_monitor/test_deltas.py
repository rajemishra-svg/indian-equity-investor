"""Tests for fundamental drift detection (src/monitor/deltas.py)."""
from __future__ import annotations

import pytest

from src.db.repository import save_snapshot
from src.monitor.deltas import compute_deltas, scan_fundamental_drift


def _deltas(data_type: str, old: dict, new: dict):
    return compute_deltas("TESTCO", data_type, "2026-03-01", old, "2026-06-01", new)


# ---------------------------------------------------------------------------
# compute_deltas — rule behaviour
# ---------------------------------------------------------------------------


def test_pledging_rise_crossing_hard_trigger_is_high():
    alerts = _deltas(
        "governance",
        {"promoter_pledging_pct": 6.0},
        {"promoter_pledging_pct": 12.5},
    )
    assert len(alerts) == 1
    a = alerts[0]
    assert a.severity == "HIGH"
    assert "hard-trigger" in a.message
    assert a.change == pytest.approx(6.5)


def test_pledging_rise_below_hard_trigger_is_medium():
    alerts = _deltas(
        "governance",
        {"promoter_pledging_pct": 1.0},
        {"promoter_pledging_pct": 4.0},
    )
    assert len(alerts) == 1
    assert alerts[0].severity == "MEDIUM"


def test_small_noise_changes_do_not_alert():
    alerts = _deltas(
        "governance",
        {"promoter_pledging_pct": 2.0, "promoter_holding_pct": 55.0},
        {"promoter_pledging_pct": 2.3, "promoter_holding_pct": 54.5},
    )
    assert alerts == []


def test_improvements_do_not_alert():
    alerts = _deltas(
        "financials",
        {"roce_5y_avg": 12.0, "cfo_net_profit_3y_avg": 55.0, "debt_to_equity": 1.5},
        {"roce_5y_avg": 18.0, "cfo_net_profit_3y_avg": 85.0, "debt_to_equity": 0.8},
    )
    assert alerts == []


def test_cfo_collapse_below_hard_trigger_is_high():
    alerts = _deltas(
        "financials",
        {"cfo_net_profit_3y_avg": 75.0},
        {"cfo_net_profit_3y_avg": 42.0},
    )
    assert len(alerts) == 1
    assert alerts[0].severity == "HIGH"


def test_cfo_drop_staying_healthy_is_medium():
    alerts = _deltas(
        "financials",
        {"cfo_net_profit_3y_avg": 95.0},
        {"cfo_net_profit_3y_avg": 80.0},
    )
    assert len(alerts) == 1
    assert alerts[0].severity == "MEDIUM"


def test_leverage_build_up_alerts():
    alerts = _deltas(
        "financials",
        {"debt_to_equity": 1.0, "interest_coverage": 8.0},
        {"debt_to_equity": 3.4, "interest_coverage": 2.5},
    )
    fields = {a.field: a.severity for a in alerts}
    assert fields == {"debt_to_equity": "HIGH", "interest_coverage": "HIGH"}


def test_missing_fields_are_skipped():
    alerts = _deltas(
        "financials",
        {"roce_5y_avg": None},
        {"roce_5y_avg": 8.0},
    )
    assert alerts == []


# ---------------------------------------------------------------------------
# scan_fundamental_drift — end-to-end against a seeded DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_detects_drift_between_two_snapshots(tmp_path):
    db_path = str(tmp_path / "drift.db")
    await save_snapshot(
        db_path, "DRIFTCO", "2026-03-01", "governance",
        {"promoter_pledging_pct": 2.0, "promoter_holding_pct": 60.0}, "test",
    )
    await save_snapshot(
        db_path, "DRIFTCO", "2026-06-01", "governance",
        {"promoter_pledging_pct": 11.0, "promoter_holding_pct": 55.0}, "test",
    )

    drift = await scan_fundamental_drift(db_path, ["DRIFTCO"])

    assert "DRIFTCO" in drift
    alerts = drift["DRIFTCO"]
    assert {a.field for a in alerts} == {"promoter_pledging_pct", "promoter_holding_pct"}
    # HIGH (pledging crossed 10%) sorts before MEDIUM (holding drop)
    assert alerts[0].severity == "HIGH" and alerts[0].field == "promoter_pledging_pct"


@pytest.mark.asyncio
async def test_scan_skips_tickers_with_single_snapshot(tmp_path):
    db_path = str(tmp_path / "drift.db")
    await save_snapshot(
        db_path, "NEWCO", "2026-06-01", "governance",
        {"promoter_pledging_pct": 15.0}, "test",
    )
    assert await scan_fundamental_drift(db_path, ["NEWCO"]) == {}


@pytest.mark.asyncio
async def test_scan_reports_nothing_when_fundamentals_stable(tmp_path):
    db_path = str(tmp_path / "drift.db")
    payload = {"roce_5y_avg": 20.0, "cfo_net_profit_3y_avg": 85.0, "debt_to_equity": 0.5}
    await save_snapshot(db_path, "STABLECO", "2026-03-01", "financials", payload, "test")
    await save_snapshot(db_path, "STABLECO", "2026-06-01", "financials", payload, "test")

    assert await scan_fundamental_drift(db_path, ["STABLECO"]) == {}
