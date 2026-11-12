"""逐笔成本归因（C3）：把费用与滑点落到同一张对照表上。

    total_cost = Σ 费用科目 + Σ 滑点四项（冲击/延迟/价差/隔夜）

硬约束：分项之和必须等于 total_cost（误差 0），否则审计直接报 BLOCKED。
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

from ..api import AuditReport, Fill, RuleHit, Severity, SpecError
from ..calendar.regime import RegimeCalendar
from ..cost.fees import compute_fees

RULE_UNBALANCED = "ATTR.unbalanced"
RULE_NO_SLIPPAGE = "ATTR.no_slippage"

SLIP_COLS = ("impact_bps", "delay_bps", "spread_bps", "overnight_bps", "residual_bps")
SLIP_MAP = {
    "impact_bps": "impact_cost",
    "delay_bps": "delay_cost",
    "spread_bps": "spread_cost",
    "overnight_bps": "overnight_cost",
    "residual_bps": "residual_cost",
}


def attribute_fills(
    fills: Sequence[Fill],
    regime: RegimeCalendar,
    slippage: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, AuditReport]:
    """返回逐笔归因表与审计报告。"""
    report = AuditReport(stage="attribution")
    slip_index = {}
    if slippage is not None and not slippage.empty:
        slip_index = slippage.set_index("order_id").to_dict(orient="index")

    rows = []
    for fill in fills:
        notional = float(fill.qty) * float(fill.price)
        fee_items = compute_fees(notional, fill.side, fill.trade_date, regime) if notional > 0 else {}
        slip = slip_index.get(fill.order_id, {})
        row = {
            "order_id": fill.order_id,
            "trade_date": pd.Timestamp(fill.trade_date),
            "code": fill.code,
            "side": fill.side.value,
            "qty": float(fill.qty),
            "price": float(fill.price),
            "notional": notional,
            "fee_total": 0.0,
            "total_cost": 0.0,
            "total_bps": 0.0,
        }
        for item, amount in fee_items.items():
            row[f"fee:{item}"] = float(amount)
        row["fee_total"] = float(sum(fee_items.values()))

        for bps_col, cost_col in SLIP_MAP.items():
            bps = float(slip.get(bps_col, 0.0))
            row[cost_col] = bps / 10_000.0 * notional
        row["slippage_total"] = sum(row[c] for c in SLIP_MAP.values())
        row["total_cost"] = row["fee_total"] + row["slippage_total"]
        row["total_bps"] = (row["total_cost"] / notional * 10_000.0) if notional > 0 else 0.0
        rows.append(row)

    frame = pd.DataFrame(rows)
    report.meta["fills"] = len(fills)
    if frame.empty:
        return frame, report

    item_cols = [c for c in frame.columns if c.startswith("fee:")]
    # 某些科目只在一侧发生（如印花税只在卖出），缺失方补 0，避免 NaN 传播
    for col in [*item_cols, *SLIP_MAP.values(), "fee_total", "slippage_total"]:
        if col in frame.columns:
            frame[col] = frame[col].astype("float64").fillna(0.0)
    rebuilt = frame[item_cols].sum(axis=1) + frame[[*SLIP_MAP.values()]].sum(axis=1)
    gap = (rebuilt - frame["total_cost"]).abs()
    report.meta["max_gap"] = float(gap.max())
    if float(gap.max()) > 1e-6:
        report.add(
            RuleHit(
                rule_id=RULE_UNBALANCED,
                severity=Severity.BLOCKED,
                message="attribution items do not sum to total_cost",
                scope={"max_gap": float(gap.max())},
                row_count=int((gap > 1e-6).sum()),
            )
        )
    if slippage is None or slippage.empty:
        report.add(
            RuleHit(
                rule_id=RULE_NO_SLIPPAGE,
                severity=Severity.WARN,
                message="no slippage table supplied; only fees attributed",
                row_count=len(frame),
            )
        )
    ordered = ["order_id", "trade_date", "code", "side", "qty", "price", "notional", "fee_total"]
    ordered += item_cols
    ordered += ["impact_cost", "delay_cost", "spread_cost", "overnight_cost", "residual_cost"]
    ordered += ["slippage_total", "total_cost", "total_bps"]
    return frame.loc[:, ordered], report


def aggregate(frame: pd.DataFrame, by: Sequence[str] = ("trade_date",)) -> pd.DataFrame:
    """按维度聚合；校验聚合后总额与逐笔总额一致。"""
    keys = list(by)
    missing = [k for k in keys if k not in frame.columns]
    if missing:
        raise SpecError(f"missing keys: {missing}")
    value_cols = [c for c in frame.columns if c.endswith("_cost") or c.startswith("fee:") or c == "notional"]
    grouped = frame.groupby(keys, observed=True)[value_cols].sum().reset_index()
    if abs(float(grouped["total_cost"].sum()) - float(frame["total_cost"].sum())) > 1e-6:
        raise SpecError("aggregate total mismatch")
    return grouped
