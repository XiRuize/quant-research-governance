"""容量曲线（D2）：给定组合权重、ADV 与冲击模型，反解 AUM-净收益。

    net_bps(A) = gross_alpha_bps - impact_bps(A) - fee_bps(A)
    capacity   = 最大的 A 使 net_bps > 0

口径提醒：impact 参数默认 uncalibrated；对外结论必须带这个标记，不能当作预测。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..api import SpecError
from ..calendar.regime import RegimeCalendar
from ..cost.fees import compute_fees
from ..cost.impact import SqrtImpact
from .adv import participation


def _normalise(weights: Mapping[str, float]) -> dict[str, float]:
    if not weights:
        raise SpecError("weights must not be empty")
    clean = {str(k): float(v) for k, v in weights.items()}
    if any(v < 0 for v in clean.values()):
        raise SpecError("weights must be non-negative")
    total = sum(clean.values())
    if total <= 0:
        raise SpecError("weights sum must be > 0")
    return {k: v / total for k, v in clean.items()}


def capacity_curve(
    weights: Mapping[str, float],
    adv: Mapping[str, float],
    impact: SqrtImpact,
    regime: RegimeCalendar,
    as_of: date,
    *,
    gross_alpha_bps: float,
    turnover: float = 1.0,
    sigma: float = 1.0,
    aum_grid: Sequence[float] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """返回 (曲线表, 摘要)。曲线列为 aum/participation/impact_bps/fee_bps/net_bps/feasible。"""
    w = _normalise(weights)
    missing = [code for code in w if code not in adv]
    if missing:
        raise SpecError(f"missing ADV for: {sorted(missing)[:10]}")
    if turnover <= 0:
        raise SpecError("turnover must be > 0")
    if gross_alpha_bps <= 0:
        raise SpecError("gross_alpha_bps must be > 0")

    grid = np.asarray(aum_grid, dtype="float64") if aum_grid is not None else np.logspace(6, 11, 41)
    if (grid <= 0).any():
        raise SpecError("aum_grid must be positive")

    impact_by_code = {}
    for code, weight in w.items():
        a = float(adv[code])
        if a <= 0:
            raise SpecError(f"ADV must be > 0 for {code}")
        impact_by_code[code] = (weight, a)

    rows = []
    for aum in grid:
        traded = float(aum) * float(turnover)
        weighted_participation = 0.0
        weighted_impact = 0.0
        for code, (weight, a) in impact_by_code.items():
            p = participation(traded * weight, a)
            weighted_participation += weight * p
            weighted_impact += weight * impact.bps(p, sigma, cap=None)

        buy = compute_fees(traded, _buy(), as_of, regime)
        sell = compute_fees(traded, _sell(), as_of, regime)
        fees = sum(buy.values()) + sum(sell.values())
        fee_bps = fees / traded * 10_000.0 if traded > 0 else 0.0

        net = float(gross_alpha_bps) - weighted_impact - fee_bps
        rows.append(
            {
                "aum": float(aum),
                "participation": weighted_participation,
                "impact_bps": weighted_impact,
                "fee_bps": fee_bps,
                "net_bps": net,
                "feasible": net > 0,
            }
        )

    curve = pd.DataFrame(rows)
    feasible = curve.loc[curve["feasible"], "aum"]
    capacity = float(feasible.max()) if not feasible.empty else None
    summary = {
        "capacity_aum": capacity,
        "gross_alpha_bps": float(gross_alpha_bps),
        "turnover": float(turnover),
        "sigma": float(sigma),
        "as_of": str(as_of),
        "positions": len(w),
        "uncalibrated": True,
        "impact": impact.to_dict(),
    }
    return curve, summary


def _buy():
    from ..api import Side

    return Side.BUY


def _sell():
    from ..api import Side

    return Side.SELL

