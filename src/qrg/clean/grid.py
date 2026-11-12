"""网格与可交易标志（B3）。

把“行情行缺失”转成显式的停牌，把规则算成可交易状态：
    trade_date x 在册股票  ->  suspended / limit_up / limit_down / limit_locked / tradable

注意：涨跌停价按交易所规则（四舍五入到分）计算；ST 与新股无限制由制度日历给出。
"""
from __future__ import annotations

from dataclasses import replace
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
RULE_ST_UNKNOWN = "GRID.st_unknown"
RULE_LIMIT_AMBIGUOUS = "GRID.limit_ambiguous"
RULE_ST_PROXY = "GRID.st_proxy"
RULE_NO_LIMIT = "GRID.no_limit_days"
RULE_SUSPENDED_ANNOUNCED = "GRID.suspension_announced"
RULE_PANEL_CODE_MISSING = "GRID.panel_code_missing"
RULE_COVERAGE_GAP = "GRID.master_coverage_gap"
RULE_LOW_DAILY_COVERAGE = "GRID.low_daily_coverage"


def _round_to_cent(values: np.ndarray) -> np.ndarray:
    """四舍五入到分（A 股涨跌停价规则；numpy round 是银行家舍入，不能用）。"""
    return np.floor(np.asarray(values, dtype="float64") * 100.0 + 0.5) / 100.0


def _interval_mask(
    frame: pd.DataFrame,
    intervals: pd.DataFrame,
    start_col: str,
    end_col: str,
) -> np.ndarray:
    """code × [start, end] 区间命中掩码（闭区间）。"""
    iv = intervals.loc[:, ["code", start_col, end_col]].copy()
    iv["code"] = iv["code"].astype(str).str.zfill(6)
    iv[start_col] = pd.to_datetime(iv[start_col], errors="coerce").astype("datetime64[ns]")
    iv[end_col] = pd.to_datetime(iv[end_col], errors="coerce").astype("datetime64[ns]")
    iv = iv.dropna(subset=[start_col, end_col])
    if iv.empty:
        return np.zeros(len(frame), dtype=bool)
    tmp = frame.loc[:, ["code", "trade_date"]].merge(iv, on="code", how="inner")
    hit = tmp.loc[
        (tmp["trade_date"] >= tmp[start_col]) & (tmp["trade_date"] <= tmp[end_col]),
        ["code", "trade_date"],
    ].drop_duplicates()
    hit["_hit"] = True
    marker = frame.loc[:, ["code", "trade_date"]].merge(hit, on=["code", "trade_date"], how="left")
    return marker["_hit"].fillna(False).to_numpy(dtype=bool)


def build_grid(
    panel: pd.DataFrame,
    calendar: CalendarProvider,
    master: SecurityMaster,
    regime: RegimeRules,
    start: date,
    end: date,
    spec: PanelSpec | None = None,
    suspension_rate_warn: float = 0.5,
    no_limit: pd.DataFrame | None = None,
    suspensions: pd.DataFrame | None = None,
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

    if panel is not None and not panel.empty:
        raw_codes = getattr(master, "codes", None)
        codes_in_master = (set(raw_codes() if callable(raw_codes) else raw_codes)) if raw_codes is not None else None
        if codes_in_master is not None:
            unknown_codes = sorted(set(panel["code"].astype(str)) - codes_in_master)
            if unknown_codes:
                report.add(
                    RuleHit(
                        rule_id=RULE_PANEL_CODE_MISSING,
                        severity=Severity.WARN,
                        message="panel codes missing from security master; they will be dropped from the grid",
                        scope={"codes": unknown_codes[:20]},
                        row_count=len(unknown_codes),
                        sample=tuple(unknown_codes[:5]),
                    )
                )

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
    # prev_close 取"上一有效收盘"：停牌日 close 为 NaN，若直接 shift 会让复牌首日无法判涨跌停
    close_ffill = merged.groupby("code", observed=True)["close"].ffill()
    merged["prev_close"] = close_ffill.groupby(merged["code"], observed=True).shift(1)

    # 优先走向量化：state_frame + limit_frame；两者缺一才退回逐行循环
    use_vector = hasattr(master, "state_arrays") and hasattr(regime, "limit_frame")
    if use_vector:
        arrays = master.state_arrays(
            merged["code"].to_numpy(), merged["trade_date"].dt.date.to_numpy()
        )
        merged["board"] = arrays["board"]
        merged["is_st"] = arrays["is_st"]
        merged["st_status"] = arrays["st_status"]
        merged["listed_on"] = pd.to_datetime(arrays["listed_on"])
        merged["limit_pct"] = regime.limit_frame(merged)
        merged["is_st"] = merged["is_st"].fillna(False).astype(bool)
        merged["is_st_unknown"] = merged["st_status"].fillna("unknown").eq("unknown")
        merged["is_st_proxy"] = merged["st_status"].fillna("unknown").eq("proxy")
        merged["is_new"] = np.isinf(merged["limit_pct"].to_numpy(dtype="float64"))

        unknown_mask = merged["is_st_unknown"].to_numpy(dtype=bool)
        st_pct = np.full(len(merged), np.nan)
        if unknown_mask.any():
            probe = merged.loc[unknown_mask, ["board", "trade_date", "listed_on"]].copy()
            probe["is_st"] = True
            st_pct[unknown_mask] = regime.limit_frame(probe)
    else:
        pct: list[float] = []
        st_pct_list: list[float] = []
        st_flags: list[bool] = []
        st_unknown: list[bool] = []
        st_proxy: list[bool] = []
        new_flags: list[bool] = []
        for ts, code in zip(merged["trade_date"], merged["code"]):
            day = ts.date()
            state = master.state(code, day)
            if state is None:
                pct.append(float("nan"))
                st_pct_list.append(float("nan"))
                st_flags.append(False)
                st_unknown.append(True)
                st_proxy.append(False)
                new_flags.append(False)
                continue
            pct.append(float(regime.limit_pct(state, day)))
            st_flags.append(bool(state.is_st))
            status = getattr(state, "st_status", "known")
            unknown = status == "unknown"
            st_proxy.append(status == "proxy")
            st_unknown.append(unknown)
            if unknown:
                st_state = replace(state, is_st=True)
                st_pct_list.append(float(regime.limit_pct(st_state, day)))
            else:
                st_pct_list.append(float("nan"))
            new_flags.append(bool(regime.is_no_limit(state, day)))

        merged["limit_pct"] = pct
        merged["is_st"] = st_flags
        merged["is_st_unknown"] = st_unknown
        merged["is_st_proxy"] = st_proxy
        merged["is_new"] = new_flags
        st_pct = np.asarray(st_pct_list, dtype="float64")

    merged["suspended"] = merged["close"].isna()
    merged["suspension_announced"] = False
    merged["no_limit"] = False
    merged["listed"] = True

    if suspensions is not None and not suspensions.empty:
        merged["suspension_announced"] = _interval_mask(merged, suspensions, "start_date", "end_date")
        merged["suspended"] = merged["suspended"] | merged["suspension_announced"]
    if no_limit is not None and not no_limit.empty:
        nl = no_limit.loc[:, ["code", "trade_date"]].copy()
        nl["code"] = nl["code"].astype(str).str.zfill(6)
        nl["trade_date"] = pd.to_datetime(nl["trade_date"]).astype("datetime64[ns]")
        nl = nl.drop_duplicates().assign(_no_limit=True)
        marker = merged[["code", "trade_date"]].merge(nl, on=["code", "trade_date"], how="left")
        merged["no_limit"] = marker["_no_limit"].fillna(False).to_numpy(dtype=bool)
        # 无涨跌幅限制日：幅度记为 inf，不做锁死判定
        inf_mask = merged["no_limit"].to_numpy(dtype=bool)
        if inf_mask.any():
            merged.loc[inf_mask, "limit_pct"] = np.inf

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

    merged["limit_up"] = limit_up
    merged["limit_down"] = limit_down

    # ST 状态未知时，涨跌停可能落在 5%（ST）与 10%（非 ST）之间：这是"不确定"，必须显式标记
    spct = np.asarray(st_pct, dtype="float64")
    st_unknown_arr = merged["is_st_unknown"].to_numpy(dtype=bool)
    st_mask = np.isfinite(spct) & np.isfinite(prev) & (spct != lpct)
    up_st = np.full(len(merged), np.nan)
    down_st = np.full(len(merged), np.nan)
    up_st[st_mask] = _round_to_cent(prev[st_mask] * (1.0 + spct[st_mask]))
    down_st[st_mask] = _round_to_cent(prev[st_mask] * (1.0 - spct[st_mask]))

    ambiguous_up = st_unknown_arr & st_mask & np.isfinite(close) & (close >= up_st - 1e-9) & (close < up_price - 1e-9)
    ambiguous_down = st_unknown_arr & st_mask & np.isfinite(close) & (close <= down_st + 1e-9) & (close > down_price + 1e-9)
    merged["limit_ambiguous"] = ambiguous_up | ambiguous_down

    locked_up = limit_up & (high == low)
    locked_down = limit_down & (high == low)
    # 单边价 + 触到任一规则限价：无论是否 ST 都不可交易
    locked_up_any = locked_up | (ambiguous_up & (high == low))
    locked_down_any = locked_down | (ambiguous_down & (high == low))

    merged["limit_locked"] = locked_up_any | locked_down_any
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
            "suspension_announced",
            "no_limit",
            "limit_up",
            "limit_down",
            "limit_locked",
            "limit_ambiguous",
            "is_st",
            "is_st_unknown",
            "is_st_proxy",
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
            "limit_ambiguous": int(flags["limit_ambiguous"].sum()),
            "st_unknown": int(flags["is_st_unknown"].sum()),
            "no_limit": int(flags["no_limit"].sum()),
            "suspension_announced": int(flags["suspension_announced"].sum()),
            "st_proxy": int(flags["is_st_proxy"].sum()),
            "tradable": int(flags["tradable"].sum()),
        }
    )
    if rows and int(flags["is_st_unknown"].sum()) > 0:
        report.add(
            RuleHit(
                rule_id=RULE_ST_UNKNOWN,
                severity=Severity.WARN,
                message="ST history unavailable; limit rules may be 5% instead of board pct",
                scope={"rate": float(flags["is_st_unknown"].mean())},
                row_count=int(flags["is_st_unknown"].sum()),
            )
        )
    if int(flags["is_st_proxy"].sum()) > 0:
        report.add(
            RuleHit(
                rule_id=RULE_ST_PROXY,
                severity=Severity.WARN,
                message="ST status inferred from short names (proxy); not authoritative",
                scope={"rate": float(flags["is_st_proxy"].mean())},
                row_count=int(flags["is_st_proxy"].sum()),
            )
        )
    if int(flags["limit_ambiguous"].sum()) > 0:
        report.add(
            RuleHit(
                rule_id=RULE_LIMIT_AMBIGUOUS,
                severity=Severity.L2,
                message="price moves between ST and board limit; cannot classify without ST history",
                row_count=int(flags["limit_ambiguous"].sum()),
                sample=tuple(
                    f"{r.trade_date.date()}:{r.code}" for r in flags.loc[flags["limit_ambiguous"], ["trade_date", "code"]].head(5).itertuples()
                ),
            )
        )
    if int(flags["no_limit"].sum()) > 0:
        report.add(
            RuleHit(
                rule_id=RULE_NO_LIMIT,
                severity=Severity.INFO,
                message="no price-limit days from exchange table",
                row_count=int(flags["no_limit"].sum()),
            )
        )
    if int(flags["suspension_announced"].sum()) > 0:
        report.add(
            RuleHit(
                rule_id=RULE_SUSPENDED_ANNOUNCED,
                severity=Severity.INFO,
                message="suspension from exchange records (rows may still exist)",
                row_count=int(flags["suspension_announced"].sum()),
            )
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
    if panel is not None and not panel.empty:
        panel_codes = set(panel["code"].astype(str))
        grid_codes = set(flags["code"].astype(str))
        gap = sorted(grid_codes - panel_codes)
        if gap:
            report.meta["coverage_gap_codes"] = len(gap)
            report.add(
                RuleHit(
                    rule_id=RULE_COVERAGE_GAP,
                    severity=Severity.WARN,
                    message="grid codes without any panel row in window (coverage gap, not suspension)",
                    scope={"codes": gap[:20]},
                    row_count=len(gap),
                    sample=tuple(gap[:5]),
                )
            )

        # 逐日覆盖率：数据源在某几天缺行，会被误读成"全市场停牌"
        listed_per_day = flags.groupby("trade_date", observed=True).size()
        listed_per_day.index = pd.to_datetime(listed_per_day.index).astype("datetime64[ns]")
        panel_per_day = (
            panel.assign(trade_date=pd.to_datetime(panel["trade_date"]).astype("datetime64[ns]"))
            .groupby("trade_date", observed=True)["code"]
            .nunique()
        )
        panel_per_day.index = pd.to_datetime(panel_per_day.index).astype("datetime64[ns]")
        coverage = (panel_per_day / listed_per_day).reindex(listed_per_day.index).fillna(0.0)
        if not coverage.empty:
            worst_day = coverage.idxmin()
            worst = float(coverage.min())
            report.meta["min_daily_coverage"] = worst
            report.meta["worst_coverage_day"] = str(pd.Timestamp(worst_day).date())
            if worst < 0.5:
                report.add(
                    RuleHit(
                        rule_id=RULE_LOW_DAILY_COVERAGE,
                        severity=Severity.L2,
                        message="panel covers <50% of listed codes on at least one day (source gap, not suspension)",
                        scope={"worst_day": str(pd.Timestamp(worst_day).date()), "rate": worst},
                        row_count=int((coverage < 0.5).sum()),
                    )
                )
    return flags, report
