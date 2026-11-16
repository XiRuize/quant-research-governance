"""数据覆盖检查（D）：记录每个输入的覆盖区间，并对「静默变窄」发规则命中。

规则：
- ``COV.factor_start_gap``：复权因子起始晚于面板起始（影响早期复权）
- ``COV.financial_start_gap``：财务 PIT 起始晚于面板起始
- ``COV.market_cap_start_gap``：市值起始晚于面板起始
- ``COV.single_vintage_snapshot``：主数据来自单日快照（幸存者偏差风险）
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from ..api import AuditReport, RuleHit, Severity

RULE_FACTOR_START = "COV.factor_start_gap"
RULE_FIN_START = "COV.financial_start_gap"
RULE_MV_START = "COV.market_cap_start_gap"
RULE_SNAPSHOT = "COV.single_vintage_snapshot"


def _span(frame: pd.DataFrame | None, columns: tuple[str, ...]) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {"rows": 0, "min": None, "max": None}
    for col in columns:
        if col in frame.columns:
            values = pd.to_datetime(frame[col], errors="coerce")
            if values.notna().any():
                return {
                    "rows": int(len(frame)),
                    "min": values.min().strftime("%Y-%m-%d"),
                    "max": values.max().strftime("%Y-%m-%d"),
                }
    return {"rows": int(len(frame)), "min": None, "max": None}


def build_coverage(
    panel: pd.DataFrame,
    *,
    factors: pd.DataFrame | None = None,
    financial: pd.DataFrame | None = None,
    market_cap: pd.DataFrame | None = None,
    no_limit: pd.DataFrame | None = None,
    suspensions: pd.DataFrame | None = None,
    vintages: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], AuditReport]:
    dates = pd.to_datetime(panel["trade_date"], errors="coerce") if "trade_date" in panel.columns else pd.Series(dtype="datetime64[ns]")
    window = {
        "start": dates.min().strftime("%Y-%m-%d") if dates.notna().any() else None,
        "end": dates.max().strftime("%Y-%m-%d") if dates.notna().any() else None,
        "rows": int(len(panel)),
    }
    inputs = {
        "panel": {"rows": int(len(panel)), "min": window["start"], "max": window["end"]},
        "adjust_factor": _span(factors, ("trade_date", "date")),
        "financial": _span(financial, ("notice_date", "NOTICE_DATE")),
        "market_cap": _span(market_cap, ("trade_date", "date")),
        "no_limit": _span(no_limit, ("trade_date", "date")),
        "suspensions": _span(suspensions, ("start_date",)),
    }
    report = AuditReport(stage="coverage", meta={"window": window, "inputs": inputs, "vintages": dict(vintages or {})})

    if vintages:
        for key in ("adjust_factor", "financial", "market_cap"):
            declared = vintages.get(key) or {}
            if declared.get("min") or declared.get("max"):
                inputs[key] = {
                    "rows": declared.get("rows", inputs[key]["rows"]),
                    "min": declared.get("min") or inputs[key]["min"],
                    "max": declared.get("max") or inputs[key]["max"],
                    "source": declared.get("source"),
                }

    if window["start"]:
        for name, rule, severity in (
            ("adjust_factor", RULE_FACTOR_START, Severity.WARN),
            ("financial", RULE_FIN_START, Severity.INFO),
            ("market_cap", RULE_MV_START, Severity.INFO),
        ):
            start = inputs[name]["min"]
            if start is not None and start > window["start"]:
                report.add(
                    RuleHit(
                        rule_id=rule,
                        severity=severity,
                        message=f"{name} starts after panel window",
                        scope={"panel_start": window["start"], "input_start": start},
                    )
                )

    if vintages:
        stock_basic = vintages.get("stock_basic") or {}
        snapshot = stock_basic.get("snapshot_date")
        if snapshot:
            report.add(
                RuleHit(
                    rule_id=RULE_SNAPSHOT,
                    severity=Severity.WARN,
                    message="security master from a single snapshot; historical universe may be biased",
                    scope={"snapshot_date": snapshot},
                )
            )
    return report.meta, report
