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


def master(st_events: list[dict] | None = None, codes: tuple[str, ...] = ("600001", "600002")) -> SecurityMasterImpl:
    events = [
        {
            "code": code,
            "event": "listed",
            "effective_date": "2010-01-04",
            "payload": {"name": code, "board": "main"},
        }
        for code in codes
    ]
    events += st_events or []
    return SecurityMasterImpl(events=events)


def panel() -> pd.DataFrame:
    rows = [
        (D1, "600001", 10.0, 10.0, 10.0, 10.0),
        (D2, "600001", 11.0, 11.0, 11.0, 11.0),
        (D3, "600001", 10.0, 10.5, 9.5, 10.0),
        (D1, "600002", 20.0, 20.2, 19.8, 20.0),
        (D3, "600002", 20.0, 20.2, 19.8, 20.0),
    ]
    return pd.DataFrame(
        [
            {
                "trade_date": d,
                "code": c,
                "open": o,
                "high": h,
                "low": low_,
                "close": cl,
                "volume": 1000.0,
                "amount": 1e6,
            }
            for d, c, o, h, low_, cl in rows
        ]
    )


def build(**kwargs):
    return build_grid(
        kwargs.get("panel", panel()),
        SessionCalendar(DAYS),
        kwargs.get("master", master()),
        RegimeCalendar(),
        D1,
        D3,
    )


def test_grid_covers_sessions_and_codes() -> None:
    flags, report = build()
    assert len(flags) == 6
    assert report.meta["rows"] == 6


def test_missing_row_becomes_explicit_suspension() -> None:
    flags, _ = build()
    row = flags[(flags["trade_date"] == pd.Timestamp(D2)) & (flags["code"] == "600002")].iloc[0]
    assert bool(row["suspended"]) is True
    assert bool(row["tradable"]) is False


def test_limit_up_locked_is_not_tradable() -> None:
    flags, _ = build()
    row = flags[(flags["trade_date"] == pd.Timestamp(D2)) & (flags["code"] == "600001")].iloc[0]
    assert bool(row["limit_up"]) is True
    assert bool(row["limit_locked"]) is True
    assert bool(row["tradable"]) is False


def test_limit_price_rounds_to_cent() -> None:
    flags, _ = build()
    row = flags[(flags["trade_date"] == pd.Timestamp(D2)) & (flags["code"] == "600001")].iloc[0]
    assert row["limit_up_price"] == pytest.approx(11.0)
    assert row["limit_down_price"] == pytest.approx(9.0)


def test_st_reduces_limit_to_5pct() -> None:
    st = master([{"code": "600001", "event": "st_on", "effective_date": str(D1), "payload": {}}])
    flags, _ = build(master=st)
    row = flags[(flags["trade_date"] == pd.Timestamp(D1)) & (flags["code"] == "600001")].iloc[0]
    assert bool(row["is_st"]) is True
    assert row["limit_pct"] == pytest.approx(0.05)


def test_new_listing_flag_and_no_limit() -> None:
    star = SecurityMasterImpl(
        events=[
            {
                "code": "688001",
                "event": "listed",
                "effective_date": str(D1),
                "payload": {"name": "new", "board": "star"},
            }
        ]
    )
    df = panel().iloc[0:0]
    flags, _ = build_grid(df, SessionCalendar(DAYS), star, RegimeCalendar(), D1, D3)
    assert bool(flags["is_new"].all()) is True
    assert flags["limit_pct"].isna().all() or (flags["limit_pct"] == float("inf")).all()


def test_rejects_broken_panel() -> None:
    with pytest.raises(SpecError):
        build_grid(panel().drop(columns=["volume"]), SessionCalendar(DAYS), master(), RegimeCalendar(), D1, D3)
