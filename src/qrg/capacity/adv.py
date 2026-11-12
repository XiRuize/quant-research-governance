"""ADV 与参与率（D1）。

ADV 必须只用 t 之前的数据（默认 lag=1），否则用它做仓位上限就是前视。
停牌/缺失不参与滚动平均；覆盖不足时输出 NaN 而不是 0。
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

from ..api import AuditReport, RuleHit, Severity, SpecError

RULE_LOW_COVERAGE = "ADV.low_coverage"


def compute_adv(
    panel: pd.DataFrame,
    windows: Sequence[int] = (20, 60, 120),
    *,
    value_col: str = "amount",
    volume_col: str = "volume",
    lag: int = 1,
    min_periods: int | None = None,
    coverage_warn: float = 0.6,
) -> tuple[pd.DataFrame, AuditReport]:
    """返回 [trade_date, code, adv_amount_w…, adv_volume_w…, adv_pct_w]（w 为最大窗口）。"""
    if panel.empty:
        return pd.DataFrame(columns=["trade_date", "code"]), AuditReport(stage="adv", meta={"rows": 0})
    required = ["trade_date", "code", value_col, volume_col]
    missing = [c for c in required if c not in panel.columns]
    if missing:
        raise SpecError(f"panel missing columns: {missing}")
    if lag < 1:
        raise SpecError("lag must be >= 1 (ADV uses only past data)")
    if not windows or any(w <= 0 for w in windows):
        raise SpecError("windows must be positive")

    out = panel.loc[:, ["trade_date", "code", value_col, volume_col]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["code"] = out["code"].astype("string")
    out = out.sort_values(["code", "trade_date"]).reset_index(drop=True)
    grouped = out.groupby("code", observed=True)

    for w in windows:
        mp = min_periods if min_periods is not None else max(1, w // 2)
        out[f"adv_amount_{w}"] = grouped[value_col].transform(
            lambda s, w=w, mp=mp: s.shift(lag).rolling(w, min_periods=mp).mean()
        )
        out[f"adv_volume_{w}"] = grouped[volume_col].transform(
            lambda s, w=w, mp=mp: s.shift(lag).rolling(w, min_periods=mp).mean()
        )

    largest = max(windows)
    adv_col = f"adv_amount_{largest}"
    out[f"adv_pct_{largest}"] = out.groupby("trade_date", observed=True)[adv_col].rank(pct=True)

    report = AuditReport(stage="adv", meta={"rows": int(len(out)), "windows": list(windows), "lag": lag})
    coverage = float(out[adv_col].notna().mean())
    report.meta["coverage"] = coverage
    if coverage < coverage_warn:
        report.add(
            RuleHit(
                rule_id=RULE_LOW_COVERAGE,
                severity=Severity.WARN,
                message="ADV coverage below threshold (insufficient history or many suspensions)",
                scope={"rate": 1.0 - coverage, "window": largest},
                row_count=int(out[adv_col].isna().sum()),
            )
        )
    keep = ["trade_date", "code"] + [f"adv_amount_{w}" for w in windows] + [f"adv_volume_{w}" for w in windows] + [
        f"adv_pct_{largest}"
    ]
    return out.loc[:, keep], report


def participation(notional: float, adv: float) -> float:
    """单笔成交额占 ADV 的比例；ADV 无效时返回 0。"""
    if adv is None or adv <= 0:
        return 0.0
    return max(0.0, float(notional) / float(adv))
