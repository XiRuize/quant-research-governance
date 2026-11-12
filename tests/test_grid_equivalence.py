from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qrg.api import SecurityState
from qrg.calendar.regime import RegimeCalendar
from qrg.calendar.session import SessionCalendar
from qrg.clean.grid import build_grid
from qrg.master.security import SecurityMasterImpl

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
DAYS = [D1, D2, D3]


def _master() -> SecurityMasterImpl:
    events = [
        {"code": "600001", "event": "listed", "effective_date": "2010-01-04", "payload": {"name": "A", "board": "main"}},
        {"code": "600001", "event": "st_on", "effective_date": str(D2), "payload": {"proxy": True}},
        {"code": "600002", "event": "listed", "effective_date": "2024-01-02", "payload": {"name": "B", "board": "star"}},
        {"code": "920001", "event": "listed", "effective_date": "2021-11-15", "payload": {"name": "C"}},
    ]
    return SecurityMasterImpl(events=events)


def test_state_arrays_match_state() -> None:
    m = _master()
    codes = ["600001", "600002", "920001", "999999"]
    days = [D1, D2, D3]
    arrays = m.state_arrays(codes * 3, days * 4)
    for code, day, board, is_st, status in zip(codes * 3, days * 4, arrays["board"], arrays["is_st"], arrays["st_status"]):
        state = m.state(code, day)
        if state is None:
            continue
        assert state.board == board, (code, day)
        assert bool(state.is_st) == bool(is_st), (code, day)
        assert state.st_status == status, (code, day)


class _LoopMaster:
    """只暴露 state/listable，用于强制网格走逐行回退路径。"""

    def __init__(self, inner: SecurityMasterImpl) -> None:
        self._inner = inner

    def state(self, code: str, as_of: date) -> SecurityState | None:
        return self._inner.state(code, as_of)

    def listable(self, as_of: date) -> set[str]:
        return self._inner.listable(as_of)


class _LoopRegime:
    """不提供 limit_frame，强制逐行 limit_pct。"""

    def __init__(self, inner: RegimeCalendar) -> None:
        self._inner = inner

    def limit_pct(self, state: SecurityState, day: date) -> float:
        return self._inner.limit_pct(state, day)

    def fees(self, day: date):
        return self._inner.fees(day)

    def is_no_limit(self, state: SecurityState, day: date) -> bool:
        return self._inner.is_no_limit(state, day)


def _panel() -> pd.DataFrame:
    rows = []
    for code in ("600001", "600002", "920001"):
        for i, day in enumerate(DAYS):
            rows.append(
                {
                    "trade_date": day,
                    "code": code,
                    "open": 10.0 + i,
                    "high": 10.5 + i,
                    "low": 9.5 + i,
                    "close": 10.0 + i,
                    "volume": 1000.0,
                    "amount": 1e6,
                }
            )
    return pd.DataFrame(rows)


def test_grid_vector_and_loop_paths_are_identical() -> None:
    panel = _panel()
    base = _master()
    regime = RegimeCalendar()
    vector, _ = build_grid(panel, SessionCalendar(DAYS), base, regime, D1, D3)
    loop, _ = build_grid(panel, SessionCalendar(DAYS), _LoopMaster(base), _LoopRegime(regime), D1, D3)

    assert len(vector) == len(loop)
    columns = [
        "trade_date",
        "code",
        "limit_pct",
        "is_st",
        "is_st_unknown",
        "is_st_proxy",
        "is_new",
        "suspended",
        "limit_up",
        "limit_down",
        "limit_locked",
        "tradable",
    ]
    pd.testing.assert_frame_equal(vector[columns].reset_index(drop=True), loop[columns].reset_index(drop=True))
