from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qrg.api import SpecError
from qrg.calendar.regime import RegimeCalendar
from qrg.calendar.session import SessionCalendar
from qrg.clean.grid import build_grid
from qrg.master.security import SecurityMasterImpl

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
DAYS = [D1, D2, D3]


def master(events: list[dict] | None = None, codes: tuple[str, ...] = ("600001",)) -> SecurityMasterImpl:
    base = [
        {"code": c, "event": "listed", "effective_date": "2010-01-04", "payload": {"name": c, "board": "main"}}
        for c in codes
    ]
    return SecurityMasterImpl(events=base + (events or []))


def row(day: date, code: str, close: float, high: float | None = None, low: float | None = None) -> dict:
    high = close if high is None else high
    low = close if low is None else low
    return {
        "trade_date": day,
        "code": code,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000.0,
        "amount": 1e6,
    }


def build(panel: pd.DataFrame, m: SecurityMasterImpl | None = None):
    return build_grid(panel, SessionCalendar(DAYS), m or master(), RegimeCalendar(), D1, D3)


def test_prev_close_survives_suspension() -> None:
    """停牌后复牌首日，prev_close 必须取停牌前最后有效收盘。"""
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D3, "600001", 11.0)])
    flags, _ = build(panel)
    d3 = flags[(flags["trade_date"] == pd.Timestamp(D3))].iloc[0]
    assert d3["prev_close"] == pytest.approx(10.0)
    assert bool(d3["limit_up"]) is True


def test_st_unknown_is_flagged_not_silent() -> None:
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 10.7)])
    flags, report = build(panel)
    d2 = flags[(flags["trade_date"] == pd.Timestamp(D2))].iloc[0]
    assert bool(d2["is_st_unknown"]) is True
    assert bool(d2["limit_ambiguous"]) is True
    assert bool(d2["limit_locked"]) is True  # high==low 且触到 ST 限价
    assert bool(d2["tradable"]) is False
    assert "GRID.st_unknown" in {h.rule_id for h in report.hits}
    assert "GRID.limit_ambiguous" in {h.rule_id for h in report.hits}


def test_known_st_disables_ambiguity() -> None:
    st = [{"code": "600001", "event": "st_on", "effective_date": str(D1), "payload": {}}]
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 10.5)])
    flags, report = build(panel, master(st))
    d2 = flags[(flags["trade_date"] == pd.Timestamp(D2))].iloc[0]
    assert bool(d2["is_st_unknown"]) is False
    assert bool(d2["limit_ambiguous"]) is False
    assert "GRID.limit_ambiguous" not in {h.rule_id for h in report.hits}


def test_panel_code_missing_from_master_warns() -> None:
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D1, "600099", 5.0)])
    flags, report = build(panel)
    assert "GRID.panel_code_missing" in {h.rule_id for h in report.hits}
    assert "600099" not in set(flags["code"])


def test_delisted_code_leaves_grid() -> None:
    events = [{"code": "600001", "event": "delisted", "effective_date": str(D2), "payload": {}}]
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 10.0), row(D3, "600001", 10.0)])
    flags, _ = build(panel, master(events))
    dates = set(flags["trade_date"].dt.date)
    assert dates == {D1}


def test_master_coverage_gap_warns() -> None:
    m = master(codes=("600001", "600002"))
    panel = pd.DataFrame([row(D1, "600001", 10.0)])
    flags, report = build(panel, m)
    assert "GRID.master_coverage_gap" in {h.rule_id for h in report.hits}
    assert report.meta["coverage_gap_codes"] == 1


def test_low_daily_coverage_is_flagged() -> None:
    panel = pd.DataFrame([row(D1, "600001", 10.0)])  # D2/D3 整日缺口
    flags, report = build(panel)
    assert "GRID.low_daily_coverage" in {h.rule_id for h in report.hits}
    assert report.meta["min_daily_coverage"] == 0.0


def test_st_proxy_is_flagged_and_used_for_limit() -> None:
    proxy = [{"code": "600001", "event": "st_on", "effective_date": str(D1), "payload": {"proxy": True}}]
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 10.4)])
    flags, report = build(panel, master(proxy))
    d2 = flags[(flags["trade_date"] == pd.Timestamp(D2))].iloc[0]
    assert bool(d2["is_st_proxy"]) is True
    assert bool(d2["is_st"]) is True
    assert d2["limit_pct"] == pytest.approx(0.05)
    assert bool(d2["limit_ambiguous"]) is False
    assert "GRID.st_proxy" in {h.rule_id for h in report.hits}


def test_no_limit_day_is_marked() -> None:
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 11.0)])
    nl = pd.DataFrame([{"code": "600001", "trade_date": D2}])
    flags, report = build_grid(panel, SessionCalendar(DAYS), master(), RegimeCalendar(), D1, D3, no_limit=nl)
    d2 = flags[(flags["trade_date"] == pd.Timestamp(D2))].iloc[0]
    assert bool(d2["no_limit"]) is True
    assert d2["limit_pct"] == float("inf")
    assert bool(d2["limit_up"]) is False
    assert "GRID.no_limit_days" in {h.rule_id for h in report.hits}


def test_announced_suspension_overrides_missing_rows() -> None:
    panel = pd.DataFrame([row(D1, "600001", 10.0), row(D2, "600001", 10.0)])
    susp = pd.DataFrame([{"code": "600001", "start_date": D2, "end_date": D2}])
    flags, report = build_grid(panel, SessionCalendar(DAYS), master(), RegimeCalendar(), D1, D3, suspensions=susp)
    d2 = flags[(flags["trade_date"] == pd.Timestamp(D2))].iloc[0]
    assert bool(d2["suspended"]) is True
    assert bool(d2["suspension_announced"]) is True
    assert bool(d2["tradable"]) is False
    assert "GRID.suspension_announced" in {h.rule_id for h in report.hits}
