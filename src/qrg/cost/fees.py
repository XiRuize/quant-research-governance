"""费用科目（C1）：按制度日历计算成交费用。

口径：
- 只对 ``FeeRule.covers(day)`` 且 ``side`` 匹配（buy / sell / both）的规则计费；
- 佣金 ``commission`` = ``max(notional * rate, min_amount)``，最低值只对佣金生效；
- 印花税 ``stamp_duty`` 只在卖出方向计，其余科目双边；
- 每个科目金额用 ``Decimal`` + ``ROUND_HALF_UP`` 四舍五入到分，不使用银行家舍入；
- 返回结果按科目名排序、只含非零科目，不额外塞“合计”等隐藏项。

同一科目若当日命中多条规则（正常数据中时序区间不重叠），取 ``effective_from``
最新的一条，与制度日历 ``limit`` 的取值口径保持一致，避免重复计费。
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Iterable, Sequence

from ..api import Fill, FeeRule, Side

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环依赖
    from ..calendar.regime import RegimeCalendar

_CENT = Decimal("0.01")

#: 佣金科目名；最低收费只对它生效。
COMMISSION = "commission"

#: 印花税科目名；只在卖出方向计。
STAMP_DUTY = "stamp_duty"

_SIDES = {"buy", "sell"}


def _side_value(side: Side | str) -> str:
    """把 Side 枚举或字符串统一成 ``buy`` / ``sell``。"""
    value = side.value if isinstance(side, Side) else str(side)
    if value not in _SIDES:
        raise ValueError(f"invalid side: {side!r}")
    return value


def _decimal(value: float | int | str) -> Decimal:
    """用字符串构造 Decimal，避免二进制浮点误差进入金额计算。"""
    return Decimal(str(value))


def _round_cent(amount: Decimal) -> Decimal:
    """四舍五入到分（ROUND_HALF_UP）。"""
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _applicable(rules: Iterable[FeeRule], day: date, side: str) -> list[FeeRule]:
    """筛出当日、该方向适用且同一科目最新的一条规则。"""
    latest: dict[str, FeeRule] = {}
    for rule in rules:
        if not rule.covers(day):
            continue
        if rule.side not in (side, "both"):
            continue
        if rule.item == STAMP_DUTY and side != "sell":
            # 印花税单边：即使规则误配成 both，也不对买入计费
            continue
        current = latest.get(rule.item)
        if current is None or rule.effective_from > current.effective_from:
            latest[rule.item] = rule
    return [latest[item] for item in sorted(latest)]


def _rule_amount(rule: FeeRule, notional: Decimal) -> Decimal:
    """按单条规则算出到分的金额；最低收费只作用于佣金。"""
    amount = notional * _decimal(rule.rate)
    if rule.item == COMMISSION:
        minimum = _decimal(rule.min_amount)
        if amount < minimum:
            amount = minimum
    return _round_cent(amount)


def compute_fees(notional: float, side: Side, day: date, regime: RegimeCalendar) -> dict[str, float]:
    """计算单笔成交（成交金额 ``notional``）在 ``day`` 的费用明细。

    返回按科目名排序的 ``{item: amount}``，金额为到分的 float，只含非零科目。
    ``regime`` 需提供 RegimeRules 协议中的 ``fees(day)``。
    """
    if not isinstance(notional, (int, float)) or isinstance(notional, bool):
        raise TypeError(f"notional must be a number, got {type(notional).__name__}")
    value = float(notional)
    if not math.isfinite(value):
        raise ValueError("notional must be finite")
    if value < 0:
        raise ValueError(f"notional must be >= 0, got {value}")

    side_value = _side_value(side)
    notional_dec = _decimal(value)
    rules = _applicable(regime.fees(day), day, side_value)  # type: ignore[attr-defined]

    fees: dict[str, float] = {}
    for rule in rules:
        amount = _rule_amount(rule, notional_dec)
        if amount != 0:
            fees[rule.item] = float(amount)
    return {item: fees[item] for item in sorted(fees)}


def fee_total(fills: Sequence[Fill], regime: RegimeCalendar) -> dict[str, float]:
    """汇总一组成交的分科目费用；每笔成交单独套用最低收费与到分取整。"""
    totals: dict[str, Decimal] = {}
    for fill in fills:
        for item, amount in compute_fees(fill.notional, fill.side, fill.trade_date, regime).items():
            totals[item] = totals.get(item, Decimal("0")) + _decimal(amount)
    return {item: float(totals[item]) for item in sorted(totals) if totals[item] != 0}


def apply_fees(fills: Sequence[Fill], regime: RegimeCalendar) -> list[Fill]:
    """返回填好 ``fee`` 的新 Fill 列表；不修改输入对象（Fill 为 frozen dataclass）。"""
    priced: list[Fill] = []
    for fill in fills:
        breakdown = compute_fees(fill.notional, fill.side, fill.trade_date, regime)
        total = sum((_decimal(v) for v in breakdown.values()), Decimal("0"))
        priced.append(replace(fill, fee=float(_round_cent(total))))
    return priced
