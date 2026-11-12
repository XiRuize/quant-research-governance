"""清洗管线编排：单次运行与按年分区运行。

分区目的不是更快，而是**控内存**：全市场十年约 1000 万行，单进程峰值会超过 4GB；
按自然年切分后每年约 100 万行，峰值可压到 1GB 量级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from .api import AuditReport, PanelSpec, SpecError


@dataclass
class CleanResult:
    panel: pd.DataFrame
    flags: pd.DataFrame
    reports: list[AuditReport]
    steps: list[str]
    rows: int = 0
    flag_paths: list[str] = field(default_factory=list)


def _ret(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values(["code", "trade_date"]).copy()
    out["ret"] = out.groupby("code", observed=True)["close"].pct_change()
    return out


def clean_panel(
    panel: pd.DataFrame,
    *,
    spec: PanelSpec,
    calendar: Any,
    master: Any,
    regime: Any,
    industry: Any = None,
    financial: pd.DataFrame | None = None,
    factors: pd.DataFrame | None = None,
    no_limit: pd.DataFrame | None = None,
    suspensions: pd.DataFrame | None = None,
    replay_step: int = 0,
    replay_budget: int = 60,
) -> CleanResult:
    """跑完整清洗链；返回面板、flags、报告与步骤。"""
    from .clean.adjust import adjust_panel
    from .clean.grid import build_grid
    from .clean.lookahead import replay_check
    from .clean.pit import pit_merge
    from .clean.validate import validate_panel
    from .quality.normalize import normalize_panel

    reports: list[AuditReport] = []
    steps: list[str] = []

    panel, r = normalize_panel(panel)
    reports.append(r)
    steps.extend(["normalize", "validate"])

    reports.append(validate_panel(panel, spec))

    if factors is not None and not factors.empty:
        panel, r = adjust_panel(panel, factors, spec)
        reports.append(r)
        steps.append("adjust")

    start = pd.to_datetime(panel["trade_date"]).min().date()
    end = pd.to_datetime(panel["trade_date"]).max().date()
    flags, r = build_grid(
        panel,
        calendar,
        master,
        regime,
        start,
        end,
        spec,
        no_limit=no_limit,
        suspensions=suspensions,
    )
    reports.append(r)
    steps.append("grid")

    panel, r = pit_merge(panel, financial, industry, spec)
    reports.append(r)
    steps.append("pit")

    if {"close", "trade_date", "code"}.issubset(panel.columns):
        if replay_step == 1:
            reports.append(replay_check(panel, _ret, feature_cols=["ret"], max_dates=None))
        elif replay_step and replay_step > 1:
            days = sorted(pd.to_datetime(panel["trade_date"]).dt.date.unique())
            reports.append(
                replay_check(panel, _ret, dates=days[::replay_step], feature_cols=["ret"], max_dates=None)
            )
        else:
            reports.append(replay_check(panel, _ret, feature_cols=["ret"], max_dates=replay_budget))
        steps.append("lookahead")

    return CleanResult(panel=panel, flags=flags, reports=reports, steps=steps, rows=int(len(panel)))


def clean_partitioned(
    panel: pd.DataFrame,
    *,
    spec: PanelSpec,
    calendar: Any,
    master: Any,
    regime: Any,
    industry: Any = None,
    financial: pd.DataFrame | None = None,
    factors: pd.DataFrame | None = None,
    no_limit: pd.DataFrame | None = None,
    suspensions: pd.DataFrame | None = None,
    replay_step: int = 0,
    replay_budget: int = 60,
    lookback_days: int = 5,
    flags_out_dir: str | Path | None = None,
) -> CleanResult:
    """按自然年分区跑；每块带 lookback 行以保留 prev_close 跨年连续性。"""
    if panel.empty:
        return CleanResult(panel=panel, flags=pd.DataFrame(), reports=[], steps=[])

    data = panel.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    data = data.sort_values(["code", "trade_date"]).reset_index(drop=True)
    data["_year"] = data["trade_date"].dt.year

    flags_parts: list[pd.DataFrame] = []
    reports: list[AuditReport] = []
    steps: list[str] = []
    total_rows = 0
    flag_paths: list[str] = []
    first_year = True
    years = sorted(data["_year"].unique())

    for year in years:
        chunk = data.loc[data["_year"] == year].drop(columns=["_year"])
        # 取该年第一天之前最近 lookback_days 个交易日的数据作为上下文
        if not first_year:
            before = data.loc[data["_year"] < year].drop(columns=["_year"])
            ctx_days = sorted(before["trade_date"].unique())[-lookback_days:]
            context = before.loc[before["trade_date"].isin(ctx_days)]
            work = pd.concat([context, chunk], ignore_index=True)
        else:
            work = chunk

        result = clean_panel(
            work,
            spec=spec,
            calendar=calendar,
            master=master,
            regime=regime,
            industry=industry,
            financial=financial,
            factors=factors,
            no_limit=no_limit,
            suspensions=suspensions,
            replay_step=replay_step,
            replay_budget=replay_budget,
        )
        year_start = chunk["trade_date"].min()
        part = result.flags.loc[result.flags["trade_date"] >= year_start].reset_index(drop=True)
        if flags_out_dir is not None:
            target_dir = Path(flags_out_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"flags-{year}.parquet"
            part.to_parquet(path, index=False)
            flag_paths.append(str(path))
        else:
            flags_parts.append(part)
        total_rows += int(len(chunk))
        for rep in result.reports:
            reports.append(rep)
        if first_year:
            steps = list(result.steps)
        first_year = False

    # 分区模式不回传整块面板：十年 × 全市场拼回来会再吃一份内存
    return CleanResult(
        panel=pd.DataFrame(),
        flags=pd.concat(flags_parts, ignore_index=True) if flags_parts else pd.DataFrame(),
        reports=reports,
        steps=steps,
        rows=total_rows,
        flag_paths=flag_paths,
    )
