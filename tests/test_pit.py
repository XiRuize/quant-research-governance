from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qrg.api import SpecError
from qrg.calendar.industry import IndustryCalendarImpl
from qrg.clean.pit import UNKNOWN, pit_merge

DAYS = pd.date_range("2024-01-02", periods=3, freq="D")


def panel(code: str = "600001") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": DAYS,
            "code": code,
            "close": [10.0, 10.0, 10.0],
        }
    )


def industry() -> IndustryCalendarImpl:
    return IndustryCalendarImpl(
        versions=[
            {"version": "SW2014", "effective_from": "2014-01-01", "effective_to": "2021-12-13"},
            {"version": "SW2021", "effective_from": "2021-12-13", "effective_to": None},
        ],
        events=[
            {
                "code": "600001",
                "industry_code": "801010",
                "industry_name": "old",
                "level": 1,
                "version": "SW2014",
                "effective_from": "2014-01-01",
                "effective_to": "2021-12-13",
            },
            {
                "code": "600001",
                "industry_code": "801020",
                "industry_name": "new",
                "level": 1,
                "version": "SW2021",
                "effective_from": "2021-12-13",
                "effective_to": None,
            },
        ],
    )


def financial(notice: str = "2024-01-02") -> pd.DataFrame:
    return pd.DataFrame(
        [{"code": "600001", "notice_date": notice, "item": "net_profit", "value": 100.0}]
    )


def test_industry_is_as_of() -> None:
    out, report = pit_merge(panel(), industry=industry())
    assert out.iloc[0]["industry_code"] == "801020"
    assert out.iloc[0]["industry_version"] == "SW2021"
    assert report.meta["industry_unknown_rate"] == 0.0


def test_financial_not_visible_on_notice_day() -> None:
    out, report = pit_merge(panel(), financial=financial("2024-01-02"))
    assert pd.isna(out.iloc[0]["net_profit"])
    assert out.iloc[1]["net_profit"] == pytest.approx(100.0)
    assert report.meta["financial_coverage"] == pytest.approx(2 / 3)


def test_unknown_industry_marked_and_reported() -> None:
    out, report = pit_merge(panel(), industry=None)
    assert (out["industry_code"] == UNKNOWN).all()
    assert "PIT.industry_unknown" in {h.rule_id for h in report.hits}


def test_missing_financial_is_nan_not_filled() -> None:
    out, report = pit_merge(panel(), financial=financial("2030-01-01"))
    assert out["net_profit"].isna().all()
    assert "PIT.financial_missing" in {h.rule_id for h in report.hits}


def test_negative_lag_rejected() -> None:
    with pytest.raises(SpecError):
        pit_merge(panel(), lag_days=-1)


def test_missing_columns_rejected() -> None:
    with pytest.raises(SpecError):
        pit_merge(pd.DataFrame({"code": ["600001"]}))
