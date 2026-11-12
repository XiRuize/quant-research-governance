"""费用科目（C1）测试：方向、最低佣金、税率切换、取整与不变性。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from qrg.api import Fill, Side
from qrg.calendar.regime import RegimeCalendar, RegimeDoc, load_regime
from qrg.cost.fees import apply_fees, compute_fees, fee_total

ROOT = Path(__file__).resolve().parents[1]
CAL = RegimeCalendar(load_regime(ROOT / "rules" / "regime.cn.json"))

BEFORE_CUT = date(2023, 8, 25)
AFTER_CUT = date(2023, 8, 28)


def fill(
    *,
    qty: float = 100_000.0,
    price: float = 1.0,
    side: Side = Side.BUY,
    day: date = AFTER_CUT,
    order_id: str = "o1",
) -> Fill:
    return Fill(order_id=order_id, trade_date=day, code="600001", side=side, qty=qty, price=price)


def single_rule_calendar(item: str, rate: float, side: str = "both", min_amount: float = 0.0) -> RegimeCalendar:
    """构造只含一条费用规则的测试日历（limit 仅用于通过校验）。"""
    doc = RegimeDoc.model_validate(
        {
            "limit": [{"board": "main", "pct": 0.1, "effective_from": "2000-01-01", "source": "test"}],
            "fees": [
                {
                    "item": item,
                    "rate": rate,
                    "side": side,
                    "min_amount": min_amount,
                    "effective_from": "2000-01-01",
                    "source": "test",
                }
            ],
        }
    )
    return RegimeCalendar(doc)


# ------------------------------------------------------------------ 方向


def test_buy_has_no_stamp_duty() -> None:
    fees = compute_fees(100_000.0, Side.BUY, AFTER_CUT, CAL)
    assert "stamp_duty" not in fees
    assert fees == {
        "commission": 25.0,
        "exchange_fee": 4.87,
        "regulatory_fee": 2.0,
        "transfer_fee": 1.0,
    }


def test_sell_pays_stamp_duty_and_other_items_both_sides() -> None:
    buy = compute_fees(100_000.0, Side.BUY, AFTER_CUT, CAL)
    sell = compute_fees(100_000.0, Side.SELL, AFTER_CUT, CAL)
    assert sell["stamp_duty"] == 50.0
    for item in ("commission", "transfer_fee", "exchange_fee", "regulatory_fee"):
        assert buy[item] == sell[item]
    assert sell["stamp_duty"] not in buy.values()


# ------------------------------------------------------------------ 最低佣金


def test_commission_minimum_is_five_yuan() -> None:
    fees = compute_fees(1_000.0, Side.BUY, AFTER_CUT, CAL)  # 1000 * 0.00025 = 0.25
    assert fees["commission"] == 5.0


def test_commission_above_minimum_uses_rate() -> None:
    fees = compute_fees(100_000.0, Side.BUY, AFTER_CUT, CAL)  # 25 > 5
    assert fees["commission"] == 25.0


def test_min_amount_does_not_apply_to_non_commission_items() -> None:
    cal = single_rule_calendar("transfer_fee", 0.0, min_amount=5.0)
    assert compute_fees(100_000.0, Side.BUY, AFTER_CUT, cal) == {}


# ------------------------------------------------------------------ 税率切换


def test_stamp_duty_follows_regime_switch() -> None:
    before = compute_fees(100_000.0, Side.SELL, BEFORE_CUT, CAL)
    after = compute_fees(100_000.0, Side.SELL, AFTER_CUT, CAL)
    assert before["stamp_duty"] == 100.0  # 0.001
    assert after["stamp_duty"] == 50.0  # 0.0005
    assert before["stamp_duty"] == pytest.approx(after["stamp_duty"] * 2)


# ------------------------------------------------------------------ 取整


def test_round_half_up_not_bankers_rounding() -> None:
    cal = single_rule_calendar("transfer_fee", 0.0005)
    # 10010 * 0.0005 = 5.005 -> 分位 ROUND_HALF_UP -> 5.01（银行家舍入会得 5.00）
    assert compute_fees(10_010.0, Side.BUY, AFTER_CUT, cal) == {"transfer_fee": 5.01}


def test_amounts_are_rounded_to_cent() -> None:
    cal = single_rule_calendar("regulatory_fee", 0.00002)
    fees = compute_fees(12_345.0, Side.BUY, AFTER_CUT, cal)  # 0.2469
    assert fees == {"regulatory_fee": 0.25}


# ------------------------------------------------------------------ 汇总与合计


def test_result_sorted_and_nonzero_only() -> None:
    fees = compute_fees(100_000.0, Side.SELL, AFTER_CUT, CAL)
    assert list(fees) == sorted(fees)
    assert all(amount != 0 for amount in fees.values())


def test_dict_total_equals_sum_of_items() -> None:
    for side in (Side.BUY, Side.SELL):
        fees = compute_fees(123_456.78, side, AFTER_CUT, CAL)
        assert sum(fees.values()) == pytest.approx(float(sum(Decimal(str(v)) for v in fees.values())))


def test_fee_total_single_fill_matches_compute_fees() -> None:
    f = fill(qty=123_456.78, price=1.0, side=Side.SELL)
    assert fee_total([f], CAL) == compute_fees(f.notional, f.side, f.trade_date, CAL)


def test_fee_total_aggregates_per_fill_rounding() -> None:
    fills = [
        fill(qty=100_000.0, price=1.0, side=Side.BUY, order_id="a"),
        fill(qty=100_000.0, price=1.0, side=Side.SELL, order_id="b"),
        fill(qty=1_000.0, price=1.0, side=Side.BUY, order_id="c"),
    ]
    total = fee_total(fills, CAL)
    expected: dict[str, float] = {}
    for f in fills:
        for item, amount in compute_fees(f.notional, f.side, f.trade_date, CAL).items():
            expected[item] = round(expected.get(item, 0.0) + amount, 2)
    assert total == expected
    assert list(total) == sorted(total)
    assert total["commission"] == 55.0  # 25 + 25 + 5（每笔单独套最低佣金）
    assert total["stamp_duty"] == 50.0  # 只有那笔卖出计印花税


def test_fee_total_empty() -> None:
    assert fee_total([], CAL) == {}


def test_apply_fees_sets_fill_fee_to_computed_total() -> None:
    fills = [fill(qty=100_000.0, price=1.0, side=Side.SELL)]
    priced = apply_fees(fills, CAL)
    expected = compute_fees(fills[0].notional, fills[0].side, fills[0].trade_date, CAL)
    assert priced[0].fee == pytest.approx(sum(expected.values()))
    assert priced[0].fee == pytest.approx(82.87)  # 25 + 50 + 1 + 4.87 + 2


# ------------------------------------------------------------------ 不变性与确定性


def test_apply_fees_does_not_mutate_inputs() -> None:
    original = fill(qty=100_000.0, price=1.0, side=Side.SELL)
    fills = [original]
    priced = apply_fees(fills, CAL)
    assert original.fee == 0.0
    assert priced[0] is not original
    assert fills == [original]
    with pytest.raises(FrozenInstanceError):
        original.fee = 1.0  # type: ignore[misc]


def test_same_fill_twice_is_identical() -> None:
    f = fill(qty=12_345.0, price=7.89, side=Side.SELL)
    first = compute_fees(f.notional, f.side, f.trade_date, CAL)
    second = compute_fees(f.notional, f.side, f.trade_date, CAL)
    assert first == second
    assert apply_fees([f], CAL)[0].fee == apply_fees([f], CAL)[0].fee
