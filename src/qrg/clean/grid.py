"""网格与可交易标志（B3）。

把“行情行缺失”转成显式的停牌，把规则算成可交易状态：
    trade_date x 在册股票  ->  suspended / limit_up / limit_down / limit_locked / tradable

注意：涨跌停价按交易所规则（四舍五入到分）计算；ST 与新股无限制由制度日历给出。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..api import (
    PANEL_COLUMNS,
    AuditReport,
    CalendarProvider,
    PanelSpec,
    RegimeRules,
    RuleHit,
    SecurityMaster,
    Severity,
    SpecError,
)

RULE_HIGH_SUSPENSION = "GRID.high_suspension_rate"


def _round_to_cent(values: np.ndarray) -> np.ndarray:
    """四舍五入到分（A 股涨跌停价规则；numpy round 是银行家舍入，不能用）。"""
    return np.floor(np.asarray(values, dtype="float64") * 100.0 + 0.5) / 100.0


def build_grid(
    panel: pd.DataFrame,
    calendar: CalendarProvider,
    master: SecurityMaster,
    regime: RegimeRules,
    start: date,
    end: date,
    spec: PanelSpec | None = None,
    suspension_rate_warn: float = 0.5,
) -> tuple[pd.DataFrame, AuditReport]:
    """返回 (flags 面板, 审计报告)，不回写原 panel。"""
    sessions = [s.day for s in calendar.sessions(start, end)]
    report = AuditReport(stage="grid", meta={"sessions": len(sessions)})
    if not sessions:
        return pd.DataFrame(columns=["trade_date", "code"]), report

    frames = []
    for day in sessions:
        codes = sorted(master.listable(day))
        if codes:
            frames.append(pd.DataFrame({"trade_date": pd.Timestamp(day), "code": codes}))
    if not frames:
        return pd.DataFrame(columns=["trade_date", "code"]), report

    grid = pd.concat(frames, ignore_index=True)
    grid["code"] = grid["code"].astype("string")

    raw_cols = [c for c in PANEL_COLUMNS if c in panel.columns]
    if panel is None or panel.empty:
        merged = grid.copy()
        for col in PANEL_COLUMNS:
            if col not in merged.columns:
                merged[col] = np.nan
    else:
        missing = [c for c in PANEL_COLUMNS if c not in panel.columns]
        if missing:
            raise SpecError(f"panel missing columns: {missing}")
        p = panel.loc[:, raw_cols].copy()
        p["trade_date"] = pd.to_datetime(p["trade_date"])
        p["code"] = p["code"].astype("string")
        p = p.drop_duplicates(["trade_date", "code"], keep="last")
        merged = grid.merge(p, on=["trade_date", "code"], how="left")

    merged = merged.sort_values(["code", "trade_date"]).reset_index(drop=True)
    merged["prev_close"] = merged.groupby("code", observed=True)["close"].shift(1)

    pct: list[float] = []
    st_flags: list[bool] = []
    new_flags: list[bool] = []
    for ts, code in zip(merged["trade_date"], merged["code"]):
        day = ts.date()
        state = master.state(code, day)
        if state is None:
            pct.append(float("nan"))
            st_flags.append(False)
            new_flags.append(False)
            continue
        pct.append(float(regime.limit_pct(state, day)))
        st_flags.append(bool(state.is_st))
        new_flags.append(bool(regime.is_no_limit(state, day)))

    merged["limit_pct"] = pct
    merged["is_st"] = st_flags
    merged["is_new"] = new_flags
    merged["suspended"] = merged["close"].isna()
    merged["listed"] = True

    finite = np.isfinite(merged["limit_pct"].to_numpy(dtype="float64")) & merged["prev_close"].notna().to_numpy()
    prev = merged["prev_close"].to_numpy(dtype="float64")
    lpct = merged["limit_pct"].to_numpy(dtype="float64")

    up_price = np.full(len(merged), np.nan)
    down_price = np.full(len(merged), np.nan)
    up_price[finite] = _round_to_cent(prev[finite] * (1.0 + lpct[finite]))
    down_price[finite] = _round_to_cent(prev[finite] * (1.0 - lpct[finite]))

    merged["limit_up_price"] = up_price
    merged["limit_down_price"] = down_price

    close = merged["close"].to_numpy(dtype="float64")
    high = merged["high"].to_numpy(dtype="float64")
    low = merged["low"].to_numpy(dtype="float64")

    limit_up = finite & np.isfinite(close) & (close >= up_price - 1e-9)
    limit_down = finite & np.isfinite(close) & (close <= down_price + 1e-9)
    locked_up = limit_up & (high == low)
    locked_down = limit_down & (high == low)

    merged["limit_up"] = limit_up
    merged["limit_down"] = limit_down
    merged["limit_locked"] = locked_up | locked_down
    merged["tradable"] = (~merged["suspended"]) & (~merged["limit_locked"])

    flags = merged[
        [
            "trade_date",
            "code",
            "prev_close",
            "limit_pct",
            "limit_up_price",
            "limit_down_price",
            "suspended",
            "limit_up",
            "limit_down",
            "limit_locked",
            "is_st",
            "is_new",
            "tradable",
            "listed",
        ]
    ].copy()

    rows = len(flags)
    report.meta.update(
        {
            "rows": rows,
            "suspended": int(flags["suspended"].sum()),
            "limit_locked": int(flags["limit_locked"].sum()),
            "tradable": int(flags["tradable"].sum()),
        }
    )
    if rows and float(flags["suspended"].mean()) > suspension_rate_warn:
        report.add(
            RuleHit(
                rule_id=RULE_HIGH_SUSPENSION,
                severity=Severity.WARN,
                message="suspension rate above warning threshold",
                scope={"rate": float(flags["suspended"].mean())},
                row_count=int(flags["suspended"].sum()),
            )
        )
    return flags, report
