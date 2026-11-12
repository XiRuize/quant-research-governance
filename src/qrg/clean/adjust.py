"""复权（B2）：存 raw + 逐日累计因子，任意基准派生 qfq。

口径（见 spec.price_adjustment）：
    factor = close_hfq / close_raw（上市累计、非递减）
    adj(b) = raw * factor / factor[b]

因子是阶跃函数，按 as-of 取值是正确做法；价格本身仍然禁止 ffill。
"""
from __future__ import annotations

import pandas as pd

from ..api import AuditReport, PanelSpec, RuleHit, Severity, SpecError

PRICE_COLS = ("open", "high", "low", "close")
FACTOR_COLS = ("code", "trade_date", "factor")

RULE_MISSING_FACTOR = "ADJ.missing_factor"
RULE_BASE_PROXY = "ADJ.base_proxy"


def _prepare_factors(factors: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in FACTOR_COLS if c not in factors.columns]
    if missing:
        raise SpecError(f"factors missing columns: {missing}")
    f = factors.loc[:, list(FACTOR_COLS)].copy()
    f["code"] = f["code"].astype("string")
    f["trade_date"] = pd.to_datetime(f["trade_date"]).astype("datetime64[ns]")
    f = f.dropna(subset=["factor"])
    f = f.sort_values(["code", "trade_date"]).drop_duplicates(["code", "trade_date"], keep="last")
    bad = f.groupby("code", observed=True)["factor"].diff() < 0
    if bool(bad.fillna(False).any()):
        codes = sorted(f.loc[bad.fillna(False), "code"].unique().tolist())[:5]
        raise SpecError(f"factor must be non-decreasing, violated by: {codes}")
    return f


def adjust_panel(
    df: pd.DataFrame,
    factors: pd.DataFrame,
    spec: PanelSpec,
    base_date: str | None = None,
    price_cols: tuple[str, ...] = PRICE_COLS,
    suffix: str = "_qfq",
) -> tuple[pd.DataFrame, AuditReport]:
    """返回 (带 qfq 列的面板, 审计报告)；不修改输入。"""
    if df.empty:
        return df.copy(), AuditReport(stage="adjust", meta={"rows": 0})
    missing = [c for c in ("trade_date", "code", *price_cols) if c not in df.columns]
    if missing:
        raise SpecError(f"panel missing columns: {missing}")

    base = pd.Timestamp(base_date) if base_date else pd.Timestamp(spec.base_date)
    f = _prepare_factors(factors)

    out = df.copy()
    out["code"] = out["code"].astype("string")
    out["trade_date"] = pd.to_datetime(out["trade_date"]).astype("datetime64[ns]")
    out = out.sort_values(["trade_date", "code"]).reset_index(drop=True)

    merged = pd.merge_asof(
        out,
        f.sort_values(["trade_date", "code"]).rename(columns={"factor": "_factor"}),
        on="trade_date",
        by="code",
        direction="backward",
    )

    # 基准因子：每个 code 在 base_date 的 as-of 值；若上市晚于基准，用首个可用因子并标记
    base_rows = f.loc[f["trade_date"] <= base].groupby("code", observed=True).tail(1)
    base_map = base_rows.set_index("code")["factor"]
    first_rows = f.groupby("code", observed=True).head(1)
    first_map = first_rows.set_index("code")["factor"]
    first_dates = first_rows.set_index("code")["trade_date"]

    proxy_codes = [
        code
        for code in merged["code"].dropna().unique()
        if code not in base_map.index and code in first_map.index
    ]
    base_factor = merged["code"].map(base_map)
    fallback = merged["code"].map(first_map)
    base_factor = base_factor.fillna(fallback)

    report = AuditReport(stage="adjust", meta={"rows": int(len(out)), "base_date": str(base.date())})
    unresolved = base_factor.isna() | merged["_factor"].isna()
    if bool(unresolved.any()):
        report.add(
            RuleHit(
                rule_id=RULE_MISSING_FACTOR,
                severity=Severity.L2,
                message="missing factor for some rows; qfq left NaN",
                scope={"rate": float(unresolved.mean())},
                row_count=int(unresolved.sum()),
                sample=tuple(out.loc[unresolved, "code"].head(5).tolist()),
            )
        )
    if proxy_codes:
        report.add(
            RuleHit(
                rule_id=RULE_BASE_PROXY,
                severity=Severity.WARN,
                message="code listed after base_date; base factor取自首个可用因子",
                scope={"codes": proxy_codes[:20], "first_dates": {c: str(first_dates[c].date()) for c in proxy_codes[:20]}},
                row_count=len(proxy_codes),
                sample=tuple(proxy_codes[:5]),
            )
        )

    ratio = merged["_factor"] / base_factor
    for col in price_cols:
        merged[f"{col}{suffix}"] = merged[col] * ratio

    keep = [c for c in merged.columns if c not in {"_factor"}]
    out_df = merged.loc[:, keep].sort_values(["trade_date", "code"]).reset_index(drop=True)
    return out_df, report


def roundtrip_error(raw: pd.Series, factor: pd.Series, base_factor: float) -> pd.Series:
    """adj -> raw 的还原误差，用于验收“任意基准可逆”。"""
    adj = raw * factor / base_factor
    return (adj * base_factor / factor - raw).abs()
