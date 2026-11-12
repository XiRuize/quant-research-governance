"""冲击模型（C2）：参与率驱动的平方根冲击。

    impact_bps = coef_bps * participation^exponent * sigma

- participation = 成交量 / ADV（0~1）；
- sigma 为归一化波动（默认 1.0，表示“标准波动”）；
- 参数全部外置到 rules/impact.json，默认标 uncalibrated。

模型是**可关闭、可替换**的：不传模型时冲击为 0，绝不静默使用未校准参数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..api import Side, SpecError

DEFAULT_RULES = Path(__file__).resolve().parents[3] / "rules" / "impact.json"


@dataclass(frozen=True)
class SqrtImpact:
    coef_bps: float = 10.0
    exponent: float = 0.5

    def __post_init__(self) -> None:
        if self.coef_bps < 0:
            raise SpecError("coef_bps must be >= 0")
        if not 0 < self.exponent <= 1:
            raise SpecError("exponent must be in (0, 1]")

    def bps(self, participation: float, sigma: float = 1.0, cap: float | None = 1.0) -> float:
        if participation <= 0:
            return 0.0
        if sigma < 0:
            raise SpecError("sigma must be >= 0")
        p = float(participation) if cap is None else min(float(participation), float(cap))
        return float(self.coef_bps) * (p**self.exponent) * float(sigma)

    def price(self, ref_price: float, side: Side, participation: float, sigma: float = 1.0) -> float:
        if ref_price <= 0:
            raise SpecError("ref_price must be > 0")
        adj = self.bps(participation, sigma) / 10_000.0
        return ref_price * (1 + adj) if side is Side.BUY else ref_price * (1 - adj)

    def to_dict(self) -> dict[str, Any]:
        return {"model": "sqrt", "coef_bps": self.coef_bps, "exponent": self.exponent}


def load_impact(path: str | Path = DEFAULT_RULES) -> SqrtImpact:
    p = Path(path)
    if not p.exists():
        raise SpecError(f"impact rules not found: {p}")
    try:
        payload: Mapping[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"impact rules not json: {exc}") from exc
    if payload.get("model", "sqrt") != "sqrt":
        raise SpecError(f"unsupported impact model: {payload.get('model')}")
    return SqrtImpact(coef_bps=float(payload["coef_bps"]), exponent=float(payload.get("exponent", 0.5)))


def participation(volume: float, adv: float) -> float:
    """成交量占 ADV 比例；ADV<=0 时返回 0（不制造无穷冲击）。"""
    if adv is None or adv <= 0:
        return 0.0
    return max(0.0, float(volume) / float(adv))

