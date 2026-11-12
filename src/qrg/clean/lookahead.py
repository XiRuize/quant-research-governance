"""零前视审计（B8）。

两种检查互补：
1. replay_check：把数据截断到 t 重算，t 日特征必须与全量运行一致（容差 0）。
   这能抓住“用了未来数据”的实现，而不依赖人读代码。
2. scan_source：静态扫描 shift(-n) / center=True / 未来窗口等典型未来函数写法。

违反前视一律 BLOCKED，不做“警告后继续”。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pandas as pd

from ..api import AuditReport, RuleHit, Severity, SpecError

RULE_REPLAY = "LA.replay_diff"
RULE_STATIC = "LA.future_function"

_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"shift\(\s*-\s*\d+", "negative shift"),
    (r"center\s*=\s*True", "centered rolling window"),
    (r"pct_change\(\s*-\s*\d+", "negative pct_change"),
    (r"merge_asof\([^)]*direction\s*=\s*['\"]forward", "forward as-of join"),
    (r"\.tail\(\s*-\s*\d+", "negative tail"),
)


def replay_check(
    panel: pd.DataFrame,
    compute: Callable[[pd.DataFrame], pd.DataFrame],
    dates: Sequence[date] | None = None,
    feature_cols: Sequence[str] | None = None,
    tol: float = 0.0,
) -> AuditReport:
    """截断回放：逐日比较全量特征与截断后特征。"""
    if panel.empty:
        return AuditReport(stage="lookahead", meta={"dates": 0})
    for col in ("trade_date", "code"):
        if col not in panel.columns:
            raise SpecError(f"panel missing columns: {col}")

    data = panel.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    all_dates = sorted(data["trade_date"].dt.date.unique())
    check_dates = list(dates) if dates is not None else all_dates
    check_dates = [d for d in check_dates if d in set(all_dates)]

    full = compute(data)
    if feature_cols is None:
        feature_cols = [
            c for c in full.columns if c not in {"trade_date", "code"} and pd.api.types.is_numeric_dtype(full[c])
        ]
    feature_cols = list(feature_cols)

    report = AuditReport(stage="lookahead", meta={"dates": len(check_dates), "features": feature_cols[:20]})
    bad_rows = 0
    worst = 0.0
    samples: list[str] = []

    for day in check_dates:
        truncated = data.loc[data["trade_date"].dt.date <= day]
        part = compute(truncated)
        mask_full = full["trade_date"].dt.date == day
        mask_part = part["trade_date"].dt.date == day
        a = full.loc[mask_full, ["code", *feature_cols]].set_index("code")
        b = part.loc[mask_part, ["code", *feature_cols]].set_index("code").reindex(a.index)
        if a.empty:
            continue
        both_nan = a.isna() & b.isna()
        equal = (a - b).abs() <= tol
        diff = (~equal) & (~both_nan)
        n = int(diff.to_numpy().sum())
        if n:
            bad_rows += n
            worst = max(worst, float((a - b).abs().max(numeric_only=True).max()))
            if len(samples) < 5:
                hit_idx = diff.any(axis=1)
                samples += [f"{day}:{code}" for code in a.index[hit_idx][:5]]

    report.meta.update({"diff_rows": bad_rows, "max_abs_diff": worst})
    if bad_rows:
        report.add(
            RuleHit(
                rule_id=RULE_REPLAY,
                severity=Severity.BLOCKED,
                message="feature changes when data is truncated to t (look-ahead)",
                scope={"diff_rows": bad_rows, "max_abs_diff": worst},
                row_count=bad_rows,
                sample=tuple(samples),
            )
        )
    return report


def scan_source(paths: str | Path | Iterable[str | Path]) -> AuditReport:
    """静态扫描未来函数写法；命中即 BLOCKED。"""
    items = [paths] if isinstance(paths, (str, Path)) else list(paths)
    report = AuditReport(stage="lookahead-static")
    for item in items:
        p = Path(item)
        files = [p] if p.is_file() else sorted(p.rglob("*.py")) if p.is_dir() else []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                for pattern, label in _PATTERNS:
                    if re.search(pattern, line):
                        report.add(
                            RuleHit(
                                rule_id=RULE_STATIC,
                                severity=Severity.BLOCKED,
                                message=f"{label}: {line.strip()[:120]}",
                                scope={"file": str(f), "line": line_no, "pattern": label},
                                row_count=1,
                                sample=(f"{f}:{line_no}",),
                            )
                        )
    return report
