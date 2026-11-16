"""从日频名称序列派生 ST 事件（proxy）。

口径：
- 仅凭简称判断 ``ST/*ST/SST/S*ST`` 前缀，**不是**交易所的正式特别处理状态；
- 事件日期取简称首次出现当日（盘前可见），因此不会引入前视；
- 首个观测无论是否 ST 都写一条事件，用于确定初始状态；
- 名称缺失/代码缺失的行跳过，不推断。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import pandas as pd

ST_PATTERN = re.compile(r"^\s*(?:\*ST|ST|SST|S\*ST)")


def st_events_from_names(
    frame: pd.DataFrame,
    *,
    code_col: str = "code",
    date_col: str = "trade_date",
    name_col: str = "name",
    pattern: re.Pattern[str] = ST_PATTERN,
    source: str = "short_name",
    vintage_date: str | None = None,
) -> list[dict[str, Any]]:
    """把 (code, date, name) 序列转成 ``st_on`` / ``st_off`` 事件列表。"""
    missing = [c for c in (code_col, date_col, name_col) if c not in frame.columns]
    if missing:
        raise ValueError(f"frame missing columns: {missing}")
    if frame.empty:
        return []

    df = frame.loc[:, [code_col, date_col, name_col]].copy()
    df["code"] = df[code_col].astype("string").str.strip().str.zfill(6)
    df["day"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["code", "day"])
    df["is_st"] = df[name_col].astype("string").str.match(pattern, na=False)
    df = df.sort_values(["code", "day"]).drop_duplicates(["code", "day"], keep="last")

    payload: dict[str, Any] = {"proxy": True, "source": source}
    if vintage_date:
        payload["vintage_date"] = vintage_date

    events: list[dict[str, Any]] = []
    for code, group in df.groupby("code", sort=True):
        prev: bool | None = None
        for day, is_st in zip(group["day"], group["is_st"]):
            current = bool(is_st)
            if prev is None or current != prev:
                events.append(
                    {
                        "code": str(code),
                        "event": "st_on" if current else "st_off",
                        "effective_date": pd.Timestamp(day).strftime("%Y-%m-%d"),
                        "payload": dict(payload),
                    }
                )
                prev = current
    return events


def events_by_code(events: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        out.setdefault(str(event["code"]), []).append(event)
    return out
