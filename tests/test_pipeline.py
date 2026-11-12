from __future__ import annotations

from pathlib import Path

import pandas as pd

from qrg.calendar.regime import RegimeCalendar
from qrg.calendar.session import SessionCalendar
from qrg.master.security import SecurityMasterImpl
from qrg.pipeline import clean_panel, clean_partitioned
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]


def _panel(days: int = 800) -> pd.DataFrame:
    dates = pd.bdate_range("2023-06-01", periods=days)
    rows = []
    for code in ("600001", "600002"):
        for i, d in enumerate(dates):
            close = 10 + i * 0.01
            rows.append(
                {
                    "trade_date": d,
                    "code": code,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000.0,
                    "amount": 1e6,
                }
            )
    return pd.DataFrame(rows)


def _setup(panel: pd.DataFrame):
    spec = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()
    dates = sorted(pd.to_datetime(panel["trade_date"]).dt.date.unique())
    calendar = SessionCalendar(dates)
    master = SecurityMasterImpl(
        events=[
            {"code": c, "event": "listed", "effective_date": "2010-01-04", "payload": {"name": c}}
            for c in sorted(panel["code"].unique())
        ]
    )
    return spec, calendar, master, RegimeCalendar()


def test_partitioned_flags_match_single_run() -> None:
    panel = _panel()
    spec, calendar, master, regime = _setup(panel)
    single = clean_panel(panel, spec=spec, calendar=calendar, master=master, regime=regime, replay_step=1)
    parts = clean_partitioned(
        panel, spec=spec, calendar=calendar, master=master, regime=regime, replay_step=1
    )
    columns = ["trade_date", "code", "limit_pct", "is_st", "suspended", "limit_up", "tradable"]
    a = single.flags.loc[:, columns].sort_values(["trade_date", "code"]).reset_index(drop=True)
    b = parts.flags.loc[:, columns].sort_values(["trade_date", "code"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    assert parts.rows == len(panel)
