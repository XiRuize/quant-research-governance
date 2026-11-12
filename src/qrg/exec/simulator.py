"""成交模拟（C4）。

把研究侧的下单意图（:class:`qrg.api.Order`）按可观察的市场状态映射成
逐单成交记录（:class:`qrg.api.Fill`）。设计约束：

* **纯离线、确定性**：不联网、不读时钟、不使用随机数；同一输入两次运行
  的顺序与字段完全一致。
* **不静默丢单**：每个 ``Order`` 恰好产出一条 ``Fill``；未成交时
  ``qty=0``、``price=ref_price``，并给出 ``UNFILLED_REASONS`` 中的原因码。
* **判定顺序固定**：缺失行情/停牌 -> 涨跌停锁死 -> T+1 可卖量 ->
  成交量参与上限 -> 成交价。先触发的前置规则直接决定 qty=0。
* **不猜口径**：成交价只按调用方给定的 ``price_basis`` 计算；成交价
  缺失时按 ``no_price`` 处理，不回退到人为价格。

``adv`` 预留给后续冲击成本/ADV 约束，本模块不读取它。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Mapping, Sequence

import pandas as pd

from ..api import AuditReport, Fill, Order, RuleHit, Severity, Side

__all__ = ["simulate"]

RULE_UNFILLED = "EXEC.unfilled"

_FILLED = "filled"
_PARTIAL = "partial"

#: 视为“已完全成交”的原因码；其余（含 partial）在 meta 中单独计数。
_EXECUTED_REASONS: frozenset[str] = frozenset({_FILLED})


def _day_key(value: Any) -> pd.Timestamp:
    """把 date/Timestamp/字符串统一成归一化后的 Timestamp（仅日期）。"""
    return pd.Timestamp(value).normalize()


def _finite(value: Any) -> float | None:
    """数值且有限才返回 float，否则 None（NaN/inf/非数值都算缺失）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _flag(record: Mapping[str, Any], name: str) -> bool:
    """读取标志列；缺失、None、NaN 一律按 False，避免 ``bool(nan) == True``。"""
    value = record.get(name, False)
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):  # 非标量（理论上不会出现在面板里）
        pass
    return bool(value)


def _index_frame(frame: pd.DataFrame | None) -> dict[tuple[pd.Timestamp, str], Mapping[str, Any]]:
    """(trade_date, code) -> 行记录；重复行保留最后一条。"""
    if frame is None or frame.empty:
        return {}
    columns = set(frame.columns)
    if not {"trade_date", "code"} <= columns:
        return {}
    indexed: dict[tuple[pd.Timestamp, str], Mapping[str, Any]] = {}
    for record in frame.to_dict("records"):
        indexed[(_day_key(record["trade_date"]), str(record["code"]))] = record
    return indexed


def _resolve_price(record: Mapping[str, Any] | None, price_basis: str) -> float | None:
    """按 price_basis 取成交价；vwap 不可用时回退 close。缺失返回 None。"""
    if record is None:
        return None
    close = _finite(record.get("close"))
    if price_basis == "vwap":
        volume = _finite(record.get("volume"))
        amount = _finite(record.get("amount"))
        if volume is not None and volume > 0 and amount is not None and amount > 0:
            return amount / volume
    if close is not None and close > 0:
        return close
    return None


def _volume_cap(record: Mapping[str, Any] | None, participation_cap: float) -> float:
    """当日该股可成交上限 = volume * participation_cap；缺失/无效按 0 处理。"""
    if record is None:
        return 0.0
    volume = _finite(record.get("volume"))
    cap = _finite(participation_cap)
    if volume is None or cap is None or volume <= 0 or cap <= 0:
        return 0.0
    return volume * cap


def _zero_fill(order: Order, reason: str) -> Fill:
    """未成交记录：qty=0，price 用到达价 ref_price。"""
    return Fill(
        order_id=order.order_id,
        trade_date=order.trade_date,
        code=order.code,
        side=order.side,
        qty=0.0,
        price=float(order.ref_price),
        fee=0.0,
        reason=reason,
    )


def simulate(
    orders: Sequence[Order],
    flags: pd.DataFrame,
    panel: pd.DataFrame,
    *,
    price_basis: str = "vwap",
    participation_cap: float = 0.1,
    t_plus_1: bool = True,
    positions: Mapping[str, float] | None = None,
    adv: pd.DataFrame | None = None,
) -> tuple[list[Fill], AuditReport]:
    """按固定规则顺序把 ``orders`` 模拟成逐单 ``Fill``。

    规则顺序（命中即决定该单结果）：

    1. 无当日 flags 行或 code 不在册 -> ``no_price``。
    2. ``suspended`` -> ``suspended``。
    3. 买入遇涨停锁死 / 卖出遇跌停锁死 -> ``limit_locked``。
    4. ``t_plus_1`` 且卖出超过“非当日买入”可用量 -> 截断；
       完全不可卖为 ``t_plus_1``，部分可卖为 ``partial``。
    5. 超过 ``volume * participation_cap`` -> 截断为上限，``partial``；
       上限非正为 ``no_liquidity``。
    6. 成交价按 ``price_basis`` 解析；缺失 -> ``no_price``。
    7. 最终 qty>0 且未被截断 -> ``filled``。

    ``positions`` 视为该时点的总持仓（含当日买入），于是“非当日买入”
    可用量 = ``positions[code] - 当日同 code 买单数量``。多笔卖单按顺序
    消耗同一可用额度。``positions`` 为 ``None`` 时按空持仓处理。

    ``adv`` 当前不参与计算，保留给后续冲击/ADV 规则。
    """
    flag_map = _index_frame(flags)
    panel_map = _index_frame(panel)

    # 当日同 code 的 BUY 总量（按订单意图累计，与成交先后无关）。
    today_buys: Counter[tuple[pd.Timestamp, str]] = Counter()
    for order in orders:
        if order.side == Side.BUY:
            today_buys[(_day_key(order.trade_date), str(order.code))] += float(order.qty)

    position_map: dict[str, float] = {}
    if positions:
        for code, qty in positions.items():
            value = _finite(qty)
            if value is not None and value > 0:
                position_map[str(code)] = value

    # (trade_date, code) -> 剩余可卖（非当日买入）数量；惰性初始化。
    available_sell: dict[tuple[pd.Timestamp, str], float] = {}

    fills: list[Fill] = []
    for order in orders:
        day = _day_key(order.trade_date)
        code = str(order.code)
        key = (day, code)

        flag = flag_map.get(key)
        if flag is None:
            fills.append(_zero_fill(order, "no_price"))
            continue

        if _flag(flag, "suspended"):
            fills.append(_zero_fill(order, "suspended"))
            continue

        if order.side == Side.BUY and _flag(flag, "limit_up") and _flag(flag, "limit_locked"):
            fills.append(_zero_fill(order, "limit_locked"))
            continue
        if order.side == Side.SELL and _flag(flag, "limit_down") and _flag(flag, "limit_locked"):
            fills.append(_zero_fill(order, "limit_locked"))
            continue

        qty = float(order.qty)
        reason = ""
        is_partial = False

        # 规则 4：T+1 可卖量（只影响卖出）。
        if t_plus_1 and order.side == Side.SELL:
            if key not in available_sell:
                held = position_map.get(code, 0.0)
                bought_today = float(today_buys.get(key, 0.0))
                available_sell[key] = max(0.0, held - bought_today)
            available = available_sell[key]
            if qty > available:
                qty = available
                if qty > 0:
                    is_partial = True
                else:
                    reason = "t_plus_1"

        # 规则 5：参与率上限（逐单对比 panel 当日 volume）。
        cap_qty = _volume_cap(panel_map.get(key), participation_cap)
        if qty > 0 and qty > cap_qty:
            qty = cap_qty
            if qty > 0:
                is_partial = True
            else:
                reason = "no_liquidity"

        # 规则 6：成交价；有量但价格缺失则整单作废。
        price: float | None = None
        if qty > 0:
            price = _resolve_price(panel_map.get(key), price_basis)
            if price is None or price <= 0:
                qty = 0.0
                reason = "no_price"

        if qty <= 0:
            fills.append(_zero_fill(order, reason or "no_price"))
            continue

        if t_plus_1 and order.side == Side.SELL:
            available_sell[key] = max(0.0, available_sell.get(key, 0.0) - qty)

        fill_reason = _PARTIAL if is_partial else _FILLED
        fills.append(
            Fill(
                order_id=order.order_id,
                trade_date=order.trade_date,
                code=order.code,
                side=order.side,
                qty=qty,
                price=price,
                fee=0.0,
                reason=fill_reason,
            )
        )

    report = _build_report(fills)
    return fills, report


def _build_report(fills: list[Fill]) -> AuditReport:
    """汇总计数与原因分布；存在零成交订单时追加 WARN 命中。"""
    counts = Counter(fill.reason for fill in fills)
    filled = counts.get(_FILLED, 0)
    partial = counts.get(_PARTIAL, 0)
    unfilled = len(fills) - filled - partial

    report = AuditReport(
        stage="exec",
        meta={
            "orders": len(fills),
            "filled": filled,
            "partial": partial,
            "unfilled": unfilled,
            "reasons": {reason: counts[reason] for reason in sorted(counts)},
        },
    )

    if unfilled > 0:
        unfilled_counts = {
            reason: counts[reason]
            for reason in sorted(counts)
            if reason not in _EXECUTED_REASONS and reason != _PARTIAL
        }
        sample = tuple(
            fill.order_id or f"{fill.code}:{fill.trade_date.isoformat()}"
            for fill in fills
            if fill.reason not in _EXECUTED_REASONS and fill.reason != _PARTIAL
        )[:5]
        report.add(
            RuleHit(
                rule_id=RULE_UNFILLED,
                severity=Severity.WARN,
                message="orders received no fill",
                scope={"reasons": unfilled_counts},
                row_count=unfilled,
                sample=sample,
            )
        )
    return report
