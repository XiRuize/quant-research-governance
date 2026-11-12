from __future__ import annotations

from datetime import date

import pandas as pd

from qrg.api import FLAG_COLUMNS, Fill, Order, Side
from qrg.exec.simulator import simulate

D = date(2024, 1, 2)
CODE = "600001"

FLAG_DEFAULTS: dict[str, object] = {
    "prev_close": 10.0,
    "limit_pct": 0.1,
    "limit_up_price": 11.0,
    "limit_down_price": 9.0,
    "suspended": False,
    "limit_up": False,
    "limit_down": False,
    "limit_locked": False,
    "is_st": False,
    "is_new": False,
    "tradable": True,
    "listed": True,
}


def flags_frame(rows: list[dict] | None = None, **overrides: object) -> pd.DataFrame:
    rows = rows if rows is not None else [{"trade_date": D, "code": CODE, **overrides}]
    if not rows:
        return pd.DataFrame(columns=["trade_date", "code", *FLAG_COLUMNS])
    records = [{**FLAG_DEFAULTS, **row} for row in rows]
    frame = pd.DataFrame(records)
    for column in FLAG_COLUMNS:
        if column not in frame.columns:
            frame[column] = False
    return frame[["trade_date", "code", *FLAG_COLUMNS]]


def panel_frame(
    volume: float = 1000.0,
    amount: float = 10_000.0,
    close: float = 10.0,
    high: float = 10.0,
    low: float = 10.0,
    rows: list[dict] | None = None,
) -> pd.DataFrame:
    if rows is None:
        rows = [
            {
                "trade_date": D,
                "code": CODE,
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        ]
    return pd.DataFrame(rows)


def buy(qty: float, order_id: str = "b1", ref_price: float = 10.0) -> Order:
    return Order(D, CODE, Side.BUY, qty, ref_price, order_id)


def sell(qty: float, order_id: str = "s1", ref_price: float = 10.0) -> Order:
    return Order(D, CODE, Side.SELL, qty, ref_price, order_id)


def fill_by_id(fills: list[Fill], order_id: str) -> Fill:
    return next(fill for fill in fills if fill.order_id == order_id)


# ------------------------------------------------------------------ 正常成交


def test_buy_is_capped_by_volume_times_participation() -> None:
    """volume=1000, cap=0.1 -> 最多成交 100 股，reason=partial。"""
    fills, report = simulate([buy(500)], flags_frame(), panel_frame(volume=1000.0))

    assert len(fills) == 1
    fill = fills[0]
    assert fill.qty == 100.0
    assert fill.price == 10.0  # amount / volume = 10000 / 1000
    assert fill.reason == "partial"
    assert report.stage == "exec"
    assert report.meta["orders"] == 1
    assert report.meta["partial"] == 1
    assert report.meta["unfilled"] == 0
    assert report.meta["reasons"] == {"partial": 1}
    assert report.hits == []


def test_buy_within_cap_is_filled() -> None:
    fills, report = simulate([buy(100)], flags_frame(), panel_frame(volume=1000.0))

    assert fills[0].qty == 100.0
    assert fills[0].reason == "filled"
    assert report.meta["filled"] == 1


def test_close_price_basis() -> None:
    fills, _ = simulate(
        [buy(100)],
        flags_frame(),
        panel_frame(volume=1000.0, amount=9_999.0, close=9.87),
        price_basis="close",
    )

    assert fills[0].price == 9.87


# ------------------------------------------------------------ 停牌 / 锁死 / 无行情


def test_suspended_has_zero_qty() -> None:
    fills, report = simulate([buy(100)], flags_frame(suspended=True), panel_frame())

    fill = fills[0]
    assert fill.qty == 0.0
    assert fill.reason == "suspended"
    assert fill.price == 10.0  # qty=0 使用 ref_price
    assert report.meta["unfilled"] == 1
    hit = report.hits[0]
    assert hit.rule_id == "EXEC.unfilled"
    assert hit.row_count == 1
    assert hit.scope["reasons"] == {"suspended": 1}


def test_buy_limit_up_locked_is_rejected() -> None:
    flags = flags_frame(limit_up=True, limit_locked=True)
    fills, _ = simulate([buy(100)], flags, panel_frame(high=11.0, low=11.0))

    assert fills[0].qty == 0.0
    assert fills[0].reason == "limit_locked"


def test_sell_limit_down_locked_is_rejected() -> None:
    flags = flags_frame(limit_down=True, limit_locked=True)
    fills, _ = simulate([sell(100)], flags, panel_frame(high=9.0, low=9.0), positions={CODE: 100})

    assert fills[0].qty == 0.0
    assert fills[0].reason == "limit_locked"


def test_missing_flags_row_is_no_price() -> None:
    # 用空的 flags 模拟「当日无该股 flags 行」
    fills, report = simulate([buy(100)], flags_frame([]), panel_frame())
    assert fills[0].qty == 0.0
    assert fills[0].reason == "no_price"
    assert report.meta["unfilled"] == 1


def test_missing_price_is_no_price() -> None:
    panel = panel_frame(volume=0.0, amount=0.0, close=0.0)
    fills, _ = simulate([buy(100)], flags_frame(), panel)

    # volume 为 0 时先触发参与率上限 -> no_liquidity（规则 5 在价格之前）
    assert fills[0].reason == "no_liquidity"

    panel_without_price = panel_frame(volume=1000.0, amount=0.0, close=float("nan"))
    fills, _ = simulate([buy(100)], flags_frame(), panel_without_price)
    assert fills[0].qty == 0.0
    assert fills[0].reason == "no_price"


# ------------------------------------------------------------------ T+1


def test_t_plus_1_blocks_same_day_buy_then_sell() -> None:
    orders = [buy(100, "buy"), sell(100, "sell")]
    fills, report = simulate(orders, flags_frame(), panel_frame(), positions={CODE: 100})

    assert len(fills) == 2
    assert fill_by_id(fills, "sell").qty == 0.0
    assert fill_by_id(fills, "sell").reason == "t_plus_1"
    assert report.meta["reasons"]["t_plus_1"] == 1


def test_t_plus_1_partial_available_can_be_sold() -> None:
    orders = [buy(100, "buy"), sell(100, "sell")]
    fills, _ = simulate(orders, flags_frame(), panel_frame(), positions={CODE: 150})

    sell_fill = fill_by_id(fills, "sell")
    assert sell_fill.qty == 50.0  # 150 总持仓 - 当日买入 100
    assert sell_fill.reason == "partial"


def test_t_plus_1_disabled_ignores_today_buy() -> None:
    orders = [buy(100, "buy"), sell(100, "sell")]  # 无 positions
    fills, _ = simulate(orders, flags_frame(), panel_frame(), t_plus_1=False)

    assert fill_by_id(fills, "sell").qty == 100.0
    assert fill_by_id(fills, "sell").reason == "filled"


# ------------------------------------------------------------ 涨停未锁死可成交


def test_limit_up_not_locked_is_tradable() -> None:
    flags = flags_frame(limit_up=True, limit_locked=False)
    fills, _ = simulate([buy(100)], flags, panel_frame(high=11.0, low=10.0))

    assert fills[0].qty == 100.0
    assert fills[0].reason == "filled"


# ------------------------------------------------------------ 完整性 / 确定性


def test_every_order_gets_exactly_one_fill() -> None:
    flags = flags_frame(
        rows=[
            {"trade_date": D, "code": CODE},
            {"trade_date": D, "code": "600002", "suspended": True},
        ]
    )
    panel = panel_frame(
        rows=[
            {"trade_date": D, "code": CODE, "close": 10.0, "volume": 1000.0, "amount": 10_000.0},
            {"trade_date": D, "code": "600002", "close": 20.0, "volume": 1000.0, "amount": 20_000.0},
        ]
    )
    orders = [
        Order(D, CODE, Side.BUY, 50.0, 10.0, "a"),
        Order(D, "600002", Side.BUY, 50.0, 20.0, "b"),
        Order(D, "600003", Side.BUY, 50.0, 30.0, "c"),  # 不在册
    ]

    fills, _ = simulate(orders, flags, panel)

    assert len(fills) == len(orders)
    assert [fill.order_id for fill in fills] == ["a", "b", "c"]
    assert [fill.reason for fill in fills] == ["filled", "suspended", "no_price"]


def test_simulation_is_deterministic() -> None:
    flags = flags_frame(
        rows=[
            {"trade_date": D, "code": CODE},
            {"trade_date": D, "code": "600002", "limit_up": True, "limit_locked": True},
        ]
    )
    panel = panel_frame(
        rows=[
            {"trade_date": D, "code": CODE, "close": 10.0, "volume": 100.0, "amount": 1000.0},
            {"trade_date": D, "code": "600002", "close": 20.0, "volume": 100.0, "amount": 2000.0},
        ]
    )
    orders = [
        Order(D, CODE, Side.BUY, 500.0, 10.0, "a"),
        Order(D, CODE, Side.SELL, 200.0, 10.0, "b"),
        Order(D, "600002", Side.BUY, 100.0, 20.0, "c"),
        Order(D, "600003", Side.BUY, 100.0, 30.0, "d"),
    ]

    fills1, report1 = simulate(orders, flags, panel, positions={CODE: 10})
    fills2, report2 = simulate(orders, flags, panel, positions={CODE: 10})

    assert fills1 == fills2
    assert report1.to_dict() == report2.to_dict()
