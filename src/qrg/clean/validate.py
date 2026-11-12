"""硬校验（B1）：五类结构性错误 + 代码白名单。

只做“结构/逻辑必然错”的检查；统计异常属于 f-anomaly，不要混在这里。
每个规则的违规率决定分级：> max → L2，> fatal → BLOCKED。
"""
from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from ..api import PANEL_COLUMNS, AuditReport, PanelSpec, RuleHit, Severity, SpecError

_CODE_RE = re.compile(r"\d{6}")

RULE_DUP = "V.primary_key_dup"
RULE_HIGH_LOW = "V.high_lt_low"
RULE_NON_POSITIVE = "V.price_le_zero"
RULE_NEG_VOLUME = "V.negative_volume"
RULE_CODE = "V.code_not_whitelisted"


def _severity(rate: float, spec: PanelSpec) -> Severity:
    if rate > spec.violation_rate_fatal:
        return Severity.BLOCKED
    if rate > spec.violation_rate_max:
        return Severity.L2
    if rate > 0:
        return Severity.WARN
    return Severity.INFO


def _hit(rule_id: str, mask: pd.Series, spec: PanelSpec, df: pd.DataFrame, message: str) -> RuleHit | None:
    if not bool(mask.any()):
        return None
    rate = float(mask.mean())
    severity = _severity(rate, spec)
    sample: Iterable = ()
    if {"trade_date", "code"}.issubset(df.columns):
        sample = tuple(
            f"{row.trade_date}:{row.code}" for row in df.loc[mask, ["trade_date", "code"]].head(5).itertuples()
        )
    return RuleHit(
        rule_id=rule_id,
        severity=severity,
        message=f"{message} (rate={rate:.6f})",
        scope={"rate": rate},
        row_count=int(mask.sum()),
        sample=sample,
    )


def validate_panel(df: pd.DataFrame, spec: PanelSpec, stage: str = "validate") -> AuditReport:
    """返回审计报告；不修改输入。"""
    missing = [c for c in PANEL_COLUMNS if c not in df.columns]
    if missing:
        raise SpecError(f"panel missing columns: {missing}")
    if not isinstance(spec, PanelSpec):
        raise SpecError("spec must be PanelSpec")

    report = AuditReport(stage=stage, meta={"rows": int(len(df)), "spec_version": spec.spec_version})
    if df.empty:
        return report

    checks: list[tuple[str, pd.Series, str]] = [
        (
            RULE_DUP,
            df.duplicated(subset=["trade_date", "code"], keep=False),
            "duplicate primary key (trade_date, code)",
        ),
        (RULE_HIGH_LOW, df["high"] < df["low"], "high < low"),
        (
            RULE_NON_POSITIVE,
            (df[["open", "high", "low", "close"]] <= 0).any(axis=1),
            "non-positive price",
        ),
        (RULE_NEG_VOLUME, df["volume"] < 0, "negative volume"),
    ]

    for rule_id, mask, message in checks:
        hit = _hit(rule_id, mask.fillna(False), spec, df, message)
        if hit:
            report.add(hit)

    codes = df["code"].astype("string")
    canonical = codes.str.fullmatch(r"\d{6}").fillna(False)
    prefix2 = codes.str[:2]
    prefix3 = codes.str[:3]
    whitelisted = prefix2.isin(spec.code_whitelist) | prefix3.isin(spec.code_whitelist)
    code_mask = ~(canonical & whitelisted)
    hit = _hit(RULE_CODE, code_mask.fillna(True), spec, df, "code not in whitelist")
    if hit:
        report.add(hit)

    return report


def violation_summary(report: AuditReport) -> dict[str, float]:
    """规则 → 违规率，便于写进 data_quality_report。"""
    return {h.rule_id: float(h.scope.get("rate", 0.0)) for h in report.hits}
