from __future__ import annotations

import pandas as pd
import pytest

from qrg.master.actions import build_factor


def prices() -> pd.DataFrame:
    days = pd.date_range("2024-01-02", periods=4, freq="D")
    rows = []
    for code, base in (("600001", 10.0), ("600002", 20.0)):
        for i, d in enumerate(days):
            rows.append({"code": code, "trade_date": d, "close": base + i * 0.1})
    return pd.DataFrame(rows)


def test_multi_code_factor_does_not_raise() -> None:
    """回归：多标的、日期交错时 merge_asof 需要全局按 trade_date 排序。"""
    events = [
        {"code": "600001", "ex_date": "2024-01-04", "event": "cash_dividend", "payload": {"dividend": 0.5}},
        {"code": "600002", "ex_date": "2024-01-03", "event": "stock_dividend", "payload": {"shares": 1.0}},
    ]
    frame = build_factor(events, prices=prices())
    assert set(frame["code"]) == {"600001", "600002"}
    rows = frame.loc[frame["code"] == "600002"].sort_values("trade_date")
    assert rows["factor"].iloc[0] == pytest.approx(1.0)
    assert rows["factor"].iloc[-1] == pytest.approx(2.0)


def test_factor_is_monotonic_per_code() -> None:
    events = [{"code": "600001", "ex_date": "2024-01-03", "event": "stock_dividend", "payload": {"shares": 1.0}}]
    frame = build_factor(events, prices=prices())
    for code, group in frame.groupby("code"):
        assert group.sort_values("trade_date")["factor"].is_monotonic_increasing, code
