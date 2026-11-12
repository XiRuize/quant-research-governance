"""尾部压力情景（D3）：在 D2 容量曲线上重算冲击与净收益。

模型是**假设驱动、未校准（uncalibrated）**的：情景参数（波动放大、ADV 收缩、
锁仓比例）由研究假设给出，不来自拟合，也不构成对未来的预测。输出只用于回答
“如果这些假设成立，容量会怎样变化”，不得作为收益/风险预测对外表述。

口径约定
--------
- 容量曲线 ``base`` 由 D2 产出，按 AUM 升序排列，至少含
  ``aum / participation / impact_bps / fee_bps / net_bps``。
- 基准毛收益（未扣成本）由上游列反推：
  ``gross_bps = net_bps + impact_bps + fee_bps``。
- ADV 收缩到 ``adv_multiplier``（<1 表示流动性变差）时，参与率按
  ``1 / adv_multiplier`` 放大。
- ``locked_ratio`` 的仓位无法交易：可交易部分按 ``(1 - locked_ratio)``
  缩放参与率；被锁部分不再产生冲击，但其收益暴露按比例缺失，作为
  ``net_bps`` 的拖累（``gross_bps * (1 - locked_ratio)``）。费用仍按
  全仓计提，属于保守处理。
- 波动 ``vol_multiplier`` 直接放大平方根冲击模型中的 ``sigma``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from ..api import AuditReport, RuleHit, Severity, SpecError
from ..cost.impact import SqrtImpact

RULE_STRESS_ASSUMPTION = "TAIL.stress_assumption"

BASE_COLUMNS: tuple[str, ...] = ("aum", "participation", "impact_bps", "fee_bps", "net_bps")

#: 触发 WARN 的极端假设阈值。
ADV_STRESS_THRESHOLD = 0.5
LOCKED_STRESS_THRESHOLD = 0.3


@dataclass(frozen=True)
class Scenario:
    """一个未校准的压力情景假设。"""

    name: str
    vol_multiplier: float = 1.0
    adv_multiplier: float = 1.0
    locked_ratio: float = 0.0
    seed: int = 0

    def __post_init__(self) -> None:
        _check_scenario(self)


def _check_scenario(scenario: Scenario) -> None:
    if scenario.vol_multiplier <= 0:
        raise SpecError(f"vol_multiplier must be > 0, got {scenario.vol_multiplier!r}")
    if scenario.adv_multiplier <= 0:
        raise SpecError(f"adv_multiplier must be > 0, got {scenario.adv_multiplier!r}")
    if not 0.0 <= scenario.locked_ratio <= 1.0:
        raise SpecError(f"locked_ratio must be in [0, 1], got {scenario.locked_ratio!r}")


def _validate_base(base: pd.DataFrame) -> None:
    if not isinstance(base, pd.DataFrame):
        raise SpecError(f"base must be a pandas.DataFrame, got {type(base).__name__}")
    missing = [c for c in BASE_COLUMNS if c not in base.columns]
    if missing:
        raise SpecError(f"base missing columns: {missing}")


def apply_scenario(base: pd.DataFrame, scenario: Scenario, impact: SqrtImpact) -> pd.DataFrame:
    """返回 ``base`` 的副本，重算冲击与净收益，并追加 ``scenario`` 列。

    同一 ``scenario`` 与同一输入逐值确定；函数内不使用全局随机源或时间。
    """
    _validate_base(base)
    _check_scenario(scenario)

    out = base.copy()
    out["scenario"] = scenario.name

    if out.empty:
        # 保证空输入也带齐数值列，下游 concat 不因 dtype/缺列失败。
        for col in ("participation", "impact_bps", "fee_bps", "net_bps"):
            out[col] = out[col].astype("float64")
        return out

    tradable = 1.0 - float(scenario.locked_ratio)
    participation = out["participation"].astype("float64") / float(scenario.adv_multiplier)
    participation = participation * tradable
    impact_bps = out["impact_bps"].astype("float64")
    fee_bps = out["fee_bps"].astype("float64")
    net_bps = out["net_bps"].astype("float64")

    # 反推毛收益：净收益已扣除冲击与费用。
    gross_bps = net_bps + impact_bps + fee_bps

    sigma = float(scenario.vol_multiplier)
    new_impact = pd.Series(
        [impact.bps(float(p), sigma) for p in participation.to_numpy()],
        index=out.index,
        dtype="float64",
    )

    out["participation"] = participation
    out["impact_bps"] = new_impact
    out["fee_bps"] = fee_bps
    # 被锁部分失去收益暴露，但不再产生冲击；费用仍按全仓计提（保守）。
    out["net_bps"] = gross_bps * tradable - new_impact - fee_bps
    return out


def _capacity(frame: pd.DataFrame) -> float | None:
    """net_bps 首次 <= 0 的 AUM；上游保证按 AUM 升序，按行序取首个交叉点。"""
    if frame.empty:
        return None
    net = frame["net_bps"].astype("float64").fillna(float("nan"))
    mask = (net <= 0.0).to_numpy()
    if not bool(mask.any()):
        return None
    first = frame.iloc[int(mask.argmax())]
    return float(first["aum"])


def run_scenarios(
    base: pd.DataFrame,
    scenarios: Sequence[Scenario],
    impact: SqrtImpact,
) -> tuple[pd.DataFrame, AuditReport]:
    """逐情景重算并拼接，审计报告记录每个情景的容量交叉点。

    ``meta["scenarios"]`` 为 ``[{"name", "capacity", ...}]``；任一情景
    ``adv_multiplier < 0.5`` 或 ``locked_ratio > 0.3`` 时追加
    ``TAIL.stress_assumption`` WARN。
    """
    _validate_base(base)
    report = AuditReport(stage="tail")

    frames: list[pd.DataFrame] = []
    entries: list[dict[str, object]] = []
    stressed: list[str] = []
    for scenario in scenarios:
        _check_scenario(scenario)
        frame = apply_scenario(base, scenario, impact)
        frames.append(frame)
        capacity = _capacity(frame)
        entries.append(
            {
                "name": scenario.name,
                "capacity": capacity,
                "vol_multiplier": float(scenario.vol_multiplier),
                "adv_multiplier": float(scenario.adv_multiplier),
                "locked_ratio": float(scenario.locked_ratio),
                "seed": int(scenario.seed),
            }
        )
        if (
            scenario.adv_multiplier < ADV_STRESS_THRESHOLD
            or scenario.locked_ratio > LOCKED_STRESS_THRESHOLD
        ):
            stressed.append(scenario.name)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
    else:
        combined = base.iloc[0:0].copy()
        combined["scenario"] = pd.Series(dtype="object")

    report.meta["scenarios"] = entries
    # 同名情景按名称汇总会互相覆盖，仅作便捷索引；完整信息以 scenarios 列表为准。
    report.meta["capacity"] = {e["name"]: e["capacity"] for e in entries}
    report.meta["rows"] = int(len(combined))
    report.meta["uncalibrated"] = True
    report.meta["note"] = (
        "hypothesis-driven tail simulation; parameters are uncalibrated and "
        "must not be read as return/risk forecasts"
    )

    if stressed:
        report.add(
            RuleHit(
                rule_id=RULE_STRESS_ASSUMPTION,
                severity=Severity.WARN,
                message=(
                    "stress assumptions exceed warning thresholds "
                    f"(adv_multiplier < {ADV_STRESS_THRESHOLD} or "
                    f"locked_ratio > {LOCKED_STRESS_THRESHOLD})"
                ),
                scope={
                    "scenarios": list(stressed),
                    "adv_multiplier_threshold": ADV_STRESS_THRESHOLD,
                    "locked_ratio_threshold": LOCKED_STRESS_THRESHOLD,
                },
                row_count=len(combined),
                sample=tuple(stressed),
            )
        )
    return combined, report
