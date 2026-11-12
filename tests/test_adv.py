from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qrg.api import SpecError
from qrg.calendar.regime import RegimeCalendar
from qrg.capacity.adv import compute_adv
from qrg.cost.impact import SqrtImpact

D = date(2024, 1, 2)


def panel(days: int = 30) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=days, freq="D")
    return pd.DataFrame(
        {
            "trade_date": dates,
            "code": ["600001"] * days,
            "volume": [1000.0] * days,
            "amount": [1e7] * days,
        }
    )


def test_adv_uses_past_only() -> None:
    df = panel(5)
    out, _ = compute_adv(df, windows=(3,), min_periods=1, lag=1)
    # 第一天没有历史 -> NaN；第二天 = 第一天的 amount
    assert pd.isna(out.loc[0, "adv_amount_3"])
    assert out.loc[1, "adv_amount_3"] == pytest.approx(1e7)


def test_adv_window_average() -> None:
    df = panel(10)
    df.loc[9, "amount"] = 3e7  # 当日巨量不得进入当日 ADV
    out, _ = compute_adv(df, windows=(3,), min_periods=1)
    assert out.loc[9, "adv_amount_3"] == pytest.approx(1e7)


def test_adv_pct_between_zero_and_one() -> None:
    df = pd.concat([panel(10), panel(10).assign(code="600002", amount=2e7)], ignore_index=True)
    out, _ = compute_adv(df, windows=(3,), min_periods=1)
    pct = out["adv_pct_3"].dropna()
    assert ((pct > 0) & (pct <= 1)).all()


def test_adv_rejects_bad_input() -> None:
    with pytest.raises(SpecError):
        compute_adv(panel().drop(columns=["amount"]))
    with pytest.raises(SpecError):
        compute_adv(panel(), lag=0)


def test_low_coverage_warns() -> None:
    out, report = compute_adv(panel(3), windows=(120,), min_periods=60)
    assert out["adv_amount_120"].isna().all()
    assert "ADV.low_coverage" in {h.rule_id for h in report.hits}
