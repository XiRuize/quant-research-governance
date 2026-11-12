from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qrg.api import Fill, Side
from qrg.calendar.regime import RegimeCalendar
from qrg.cost.attribution import aggregate, attribute_fills

REGIME = RegimeCalendar()
D = date(2024, 1, 2)


def fills() -> list[Fill]:
    return [
        Fill("o1", D, "600001", Side.BUY, 1000.0, 10.0),
        Fill("o2", D, "600001", Side.SELL, 1000.0, 11.0),
    ]


def slippage() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"order_id": "o1", "impact_bps": 5.0, "delay_bps": 1.0, "spread_bps": 0.0, "overnight_bps": 0.0, "residual_bps": 0.0},
            {"order_id": "o2", "impact_bps": 4.0, "delay_bps": 0.0, "spread_bps": 2.0, "overnight_bps": -1.0, "residual_bps": 0.5},
        ]
    )


def test_fees_and_slippage_sum_exactly() -> None:
    frame, report = attribute_fills(fills(), REGIME, slippage())
    assert report.ok
    item_cols = [c for c in frame.columns if c.startswith("fee:")]
    rebuilt = frame[item_cols].sum(axis=1) + frame[["impact_cost", "delay_cost", "spread_cost", "overnight_cost", "residual_cost"]].sum(axis=1)
    assert ((rebuilt - frame["total_cost"]).abs() < 1e-9).all()


def test_stamp_duty_only_on_sell() -> None:
    frame, _ = attribute_fills(fills(), REGIME, slippage())
    buy = frame.loc[frame["order_id"] == "o1"].iloc[0]
    sell = frame.loc[frame["order_id"] == "o2"].iloc[0]
    assert buy.get("fee:stamp_duty", 0.0) == 0.0
    assert sell["fee:stamp_duty"] > 0


def test_without_slippage_warns() -> None:
    frame, report = attribute_fills(fills(), REGIME, None)
    assert "ATTR.no_slippage" in {h.rule_id for h in report.hits}
    assert (frame["impact_cost"] == 0).all()


def test_total_bps_matches_cost() -> None:
    frame, _ = attribute_fills(fills(), REGIME, slippage())
    for _, row in frame.iterrows():
        assert row["total_bps"] == pytest.approx(row["total_cost"] / row["notional"] * 1e4)


def test_aggregate_preserves_total() -> None:
    frame, _ = attribute_fills(fills(), REGIME, slippage())
    total = float(frame["total_cost"].sum())
    grouped = aggregate(frame, by=("code",))
    assert float(grouped["total_cost"].sum()) == pytest.approx(total)
