"""PIT 合并（B4）：财务按公告日、行业按生效日、ST 由主数据给出。

时点规则：
- 财务：NOTICE_DATE 当天不可见（盘后公告），默认顺延 1 个自然日；更保守，不会产生前视。
- 行业：as-of 查询，查不到标 UNKNOWN，禁止用当前分类回填历史。
- 缺失一律保留 NaN，禁止 ffill。
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from ..api import AuditReport, IndustryCalendar, PanelSpec, RuleHit, Severity, SpecError

RULE_INDUSTRY_UNKNOWN = "PIT.industry_unknown"
RULE_FIN_MISSING = "PIT.financial_missing"

FIN_COLUMNS = ("code", "notice_date", "item", "value")
UNKNOWN = "UNKNOWN"


def _prepare_financial(financial: pd.DataFrame, lag_days: int) -> pd.DataFrame:
    missing = [c for c in FIN_COLUMNS if c not in financial.columns]
    if missing:
        raise SpecError(f"financial missing columns: {missing}")
    f = financial.loc[:, list(FIN_COLUMNS)].copy()
    f["code"] = f["code"].astype("string")
    f["notice_date"] = pd.to_datetime(f["notice_date"])
    f["value"] = pd.to_numeric(f["value"], errors="coerce")
    f = f.dropna(subset=["notice_date", "item"])
    # 同日同科目取最后一条（重述按公告顺序后到者）
    f = f.drop_duplicates(["code", "notice_date", "item"], keep="last")
    wide = f.pivot_table(index=["code", "notice_date"], columns="item", values="value", aggfunc="last").reset_index()
    wide.columns = [str(c) for c in wide.columns]
    wide["available_date"] = wide["notice_date"] + pd.Timedelta(days=lag_days)
    return wide.sort_values(["available_date", "code"]).reset_index(drop=True)


def _industry_lookup(industry: IndustryCalendar | None, code: str, day: date) -> tuple[str, str, str]:
    if industry is None:
        return UNKNOWN, "", ""
    node = industry.classify(code, day)
    if node is None:
        return UNKNOWN, "", ""
    return node.code, node.name, node.version


def pit_merge(
    panel: pd.DataFrame,
    financial: pd.DataFrame | None = None,
    industry: IndustryCalendar | None = None,
    spec: PanelSpec | None = None,
    lag_days: int = 1,
) -> tuple[pd.DataFrame, AuditReport]:
    """给面板补上行业 as-of 与财务 PIT 字段。"""
    if panel.empty:
        return panel.copy(), AuditReport(stage="pit", meta={"rows": 0})
    missing = [c for c in ("trade_date", "code") if c not in panel.columns]
    if missing:
        raise SpecError(f"panel missing columns: {missing}")
    if lag_days < 0:
        raise SpecError("lag_days must be >= 0")

    out = panel.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["code"] = out["code"].astype("string")

    codes: list[str] = []
    names: list[str] = []
    versions: list[str] = []
    for ts, code in zip(out["trade_date"], out["code"]):
        c, name, version = _industry_lookup(industry, code, ts.date())
        codes.append(c)
        names.append(name)
        versions.append(version)
    out["industry_code"] = codes
    out["industry_name"] = names
    out["industry_version"] = versions

    report = AuditReport(stage="pit", meta={"rows": int(len(out))})
    unknown_rate = float((out["industry_code"] == UNKNOWN).mean())
    report.meta["industry_unknown_rate"] = unknown_rate
    if unknown_rate > 0:
        report.add(
            RuleHit(
                rule_id=RULE_INDUSTRY_UNKNOWN,
                severity=Severity.WARN,
                message="industry unknown for some rows; left as UNKNOWN",
                scope={"rate": unknown_rate},
                row_count=int((out["industry_code"] == UNKNOWN).sum()),
            )
        )

    if financial is not None and not financial.empty:
        wide = _prepare_financial(financial, lag_days)
        value_cols = [c for c in wide.columns if c not in {"code", "notice_date", "available_date"}]
        out = out.sort_values(["trade_date", "code"])
        merged = pd.merge_asof(
            out,
            wide.rename(columns={"code": "code"}),
            left_on="trade_date",
            right_on="available_date",
            by="code",
            direction="backward",
        )
        merged = merged.drop(columns=["notice_date", "available_date"], errors="ignore")
        coverage = float(merged[value_cols].notna().any(axis=1).mean()) if value_cols else 0.0
        report.meta["financial_coverage"] = coverage
        if coverage < 1.0:
            report.add(
                RuleHit(
                    rule_id=RULE_FIN_MISSING,
                    severity=Severity.INFO,
                    message="financial values missing for some rows; left NaN",
                    scope={"rate": 1.0 - coverage},
                    row_count=int((~merged[value_cols].notna().any(axis=1)).sum()) if value_cols else len(merged),
                )
            )
        out = merged
    else:
        report.meta["financial_coverage"] = 0.0

    return out.reset_index(drop=True), report
