"""滑点判定（C5）：以 arrival price 为基准，拆成冲击/延迟/价差/隔夜跳空 + 残差。

口径（全部为 bps，正数 = 对策略不利）：
    total = side_sign * (fill.price - arrival) / arrival * 1e4
    impact   : 由冲击模型给出（自身订单）
    delay    : 信号价 -> 到达价的漂移
    spread   : 买卖价差/微观结构，默认 0
    overnight: 昨收 -> 到达价的跳空
    residual : total - 其余四项；|residual| 超阈值必须留痕
"""
from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from ..api import AuditReport, Fill, RuleHit, Severity, Side, SlippageBreakdown, SpecError

RULE_RESIDUAL = "SLIP.unexplained_residual"
RULE_NO_FILL = "SLIP.no_fill"


def _sign(side: Side) -> float:
    return 1.0 if side is Side.BUY else -1.0


def signed_slippage_bps(fill: Fill, arrival: float) -> float:
    if arrival <= 0:
        raise SpecError("arrival must be > 0")
    if fill.qty <= 0:
        return 0.0
    return _sign(fill.side) * (fill.price - arrival) / arrival * 1e4


def decompose(
    fill: Fill,
    arrival: float,
    *,
    prev_close: float | None = None,
    delay_price: float | None = None,
    impact_bps: float = 0.0,
    spread_bps: float = 0.0,
) -> SlippageBreakdown:
    """单笔分解；未成交返回全 0（不制造噪声）。"""
    total = signed_slippage_bps(fill, arrival)
    if fill.qty <= 0:
        return SlippageBreakdown()
    delay = 0.0
    if delay_price is not None and delay_price > 0:
        delay = _sign(fill.side) * (delay_price - arrival) / arrival * 1e4
    overnight = 0.0
    if prev_close is not None and prev_close > 0:
        overnight = _sign(fill.side) * (arrival - prev_close) / prev_close * 1e4
    residual = total - (impact_bps + delay + spread_bps + overnight)
    return SlippageBreakdown(
        impact=float(impact_bps),
        delay=float(delay),
        spread=float(spread_bps),
        overnight=float(overnight),
        residual=float(residual),
    )


def decompose_many(
    fills: Sequence[Fill],
    arrivals: Mapping[str, float],
    *,
    impact_bps: Mapping[str, float] | None = None,
    prev_close: Mapping[str, float] | None = None,
    delay_price: Mapping[str, float] | None = None,
    spread_bps: float = 0.0,
    residual_tol_bps: float = 5.0,
) -> tuple[pd.DataFrame, AuditReport]:
    """批量分解；返回逐笔表与审计报告。"""
    impact_bps = impact_bps or {}
    prev_close = prev_close or {}
    delay_price = delay_price or {}
    report = AuditReport(stage="slippage")
    rows = []
    no_fill = 0

    for fill in fills:
        if fill.order_id not in arrivals:
            raise SpecError(f"missing arrival for order {fill.order_id}")
        arrival = float(arrivals[fill.order_id])
        part = decompose(
            fill,
            arrival,
            prev_close=prev_close.get(fill.code),
            delay_price=delay_price.get(fill.order_id),
            impact_bps=float(impact_bps.get(fill.order_id, 0.0)),
            spread_bps=spread_bps,
        )
        total = signed_slippage_bps(fill, arrival)
        if fill.qty <= 0:
            no_fill += 1
        rows.append(
            {
                "order_id": fill.order_id,
                "code": fill.code,
                "side": fill.side.value,
                "qty": fill.qty,
                "price": fill.price,
                "arrival": arrival,
                "total_bps": total,
                "impact_bps": part.impact,
                "delay_bps": part.delay,
                "spread_bps": part.spread,
                "overnight_bps": part.overnight,
                "residual_bps": part.residual,
                "filled": fill.qty > 0,
            }
        )

    frame = pd.DataFrame(rows)
    report.meta.update({"fills": len(fills), "no_fill": no_fill})
    if no_fill:
        report.add(
            RuleHit(
                rule_id=RULE_NO_FILL,
                severity=Severity.INFO,
                message="zero-qty fills carry no slippage",
                scope={"no_fill": no_fill},
                row_count=no_fill,
            )
        )
    if not frame.empty:
        worst = float(frame["residual_bps"].abs().max())
        report.meta["max_abs_residual_bps"] = worst
        if worst > residual_tol_bps:
            bad = frame.loc[frame["residual_bps"].abs() > residual_tol_bps]
            report.add(
                RuleHit(
                    rule_id=RULE_RESIDUAL,
                    severity=Severity.WARN,
                    message="slippage residual above tolerance",
                    scope={"tolerance_bps": residual_tol_bps, "max_abs_bps": worst},
                    row_count=int(len(bad)),
                    sample=tuple(bad["order_id"].head(5).tolist()),
                )
            )
    return frame, report
