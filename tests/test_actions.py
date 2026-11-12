from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qrg.api import SpecError
from qrg.master.actions import (
    CorporateActionsImpl,
    KNOWN_EVENTS,
    build_factor,
    verify_against_source,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "rules" / "actions.cn.json"


def factor_at(frame: pd.DataFrame, code: str, day: str | date) -> float:
    stamp = pd.Timestamp(day)
    rows = frame.loc[(frame["code"] == code) & (frame["trade_date"] == stamp), "factor"]
    assert len(rows) == 1, f"expected one factor row for {code} on {stamp}, got {len(rows)}"
    return float(rows.iloc[0])


def test_stock_dividend_10_for_10_doubles_factor() -> None:
    events = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "stock_dividend",
            "payload": {"shares": 1.0},  # 10 送 10 = 每股送 1 股
        }
    ]

    factor = build_factor(events, start=date(2024, 6, 1), end=date(2024, 6, 5))

    assert list(factor.columns) == ["code", "trade_date", "factor"]
    assert factor_at(factor, "600001", "2024-06-02") == 1.0
    assert factor_at(factor, "600001", "2024-06-03") == 2.0
    assert factor_at(factor, "600001", "2024-06-04") == 2.0
    assert factor.attrs["uses_prices"] is False
    assert any("no prices" in note for note in factor.attrs["assumptions"])


def test_split_one_to_two_doubles_factor() -> None:
    events = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "split",
            "payload": {"ratio": 2.0},  # 1 拆 2
        }
    ]

    factor = build_factor(events, start=date(2024, 6, 1), end=date(2024, 6, 4))

    assert factor_at(factor, "600001", "2024-06-02") == 1.0
    assert factor_at(factor, "600001", "2024-06-03") == 2.0
    assert factor_at(factor, "600001", "2024-06-04") == 2.0


def test_cash_dividend_with_prices_uses_prev_close_ratio() -> None:
    prices = pd.DataFrame(
        {
            "code": ["600001", "600001", "600001"],
            "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "close": [10.0, 9.5, 9.6],
        }
    )
    events = [
        {
            "code": "600001",
            "ex_date": "2024-01-03",
            "event": "cash_dividend",
            "payload": {"dividend": 0.5},
        }
    ]

    factor = build_factor(events, prices)
    expected = 10.0 / (10.0 - 0.5)

    assert factor_at(factor, "600001", "2024-01-02") == 1.0
    assert factor_at(factor, "600001", "2024-01-03") == pytest.approx(expected)
    assert factor_at(factor, "600001", "2024-01-04") == pytest.approx(expected)
    assert factor.attrs["uses_prices"] is True
    assert any("exact factor" in note for note in factor.attrs["assumptions"])


def test_unsorted_and_duplicate_ex_date_is_deterministic_and_not_double_counted() -> None:
    events = [
        # 乱序 + 完全重复的送股事件：只能乘一次 2。
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "stock_dividend",
            "payload": {"shares": 1.0},
        },
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "split",
            "payload": {"ratio": 2.0},
        },
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "stock_dividend",
            "payload": {"shares": 1.0},
        },
    ]

    first = build_factor(events, start=date(2024, 6, 1), end=date(2024, 6, 5))
    second = build_factor(
        list(reversed(events)), start=date(2024, 6, 1), end=date(2024, 6, 5)
    )

    pd.testing.assert_frame_equal(first, second)
    assert factor_at(first, "600001", "2024-06-02") == 1.0
    # 送股 2 × 拆股 2 = 4；重复送股没有变成 8。
    assert factor_at(first, "600001", "2024-06-03") == 4.0


def test_unknown_event_raises_spec_error() -> None:
    bad = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "bonus_issue",
            "payload": {},
        }
    ]

    with pytest.raises(SpecError, match="unknown event"):
        build_factor(bad)
    with pytest.raises(SpecError, match="unknown event"):
        CorporateActionsImpl(events=bad)


def test_verify_against_source_passes_within_tolerance() -> None:
    built = pd.DataFrame(
        {
            "code": ["600001", "600001", "600001"],
            "trade_date": ["2024-06-01", "2024-06-02", "2024-06-03"],
            "factor": [1.0, 1.0, 2.0],
        }
    )
    source = built.iloc[::-1].copy().reset_index(drop=True)
    source.loc[source["trade_date"] == "2024-06-03", "factor"] *= 1.00001

    error = verify_against_source(built, source)

    # 1.00001 倍源因子约等于 1e-5 相对误差（2.00002 分母带来极小偏差）。
    assert error == pytest.approx(1e-5, rel=1e-3)
    assert error <= 1e-4


def test_verify_against_source_fails_above_tolerance() -> None:
    built = pd.DataFrame(
        {
            "code": ["600001", "600001"],
            "trade_date": ["2024-06-01", "2024-06-02"],
            "factor": [1.0, 2.0],
        }
    )
    source = built.copy()
    source.loc[1, "factor"] *= 1.01

    with pytest.raises(SpecError, match="reconciliation failed"):
        verify_against_source(built, source)


def test_actions_as_of_range_and_factor_as_of() -> None:
    events = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "stock_dividend",
            "payload": {"shares": 1.0},
        },
        {
            "code": "600001",
            "ex_date": "2024-07-01",
            "event": "cash_dividend",
            "payload": {"dividend": 0.1, "prev_close": 5.0},
        },
    ]
    impl = CorporateActionsImpl(events=events)

    items = impl.actions("600001", date(2024, 6, 1), date(2024, 6, 30))

    assert len(items) == 1
    assert items[0]["code"] == "600001"
    assert items[0]["ex_date"] == date(2024, 6, 3)
    assert items[0]["event"] == "stock_dividend"
    assert items[0]["payload"] == {"shares": 1.0}
    # 区间闭区间；start > end 抛错。
    assert impl.actions("600001", date(2024, 6, 1), date(2024, 6, 3)) == items
    assert impl.actions("600001", date(2024, 8, 1), date(2024, 8, 31)) == []
    with pytest.raises(SpecError):
        impl.actions("600001", date(2024, 7, 1), date(2024, 6, 1))

    assert impl.factor("600001", date(2024, 6, 2)) == 1.0
    assert impl.factor("600001", date(2024, 6, 3)) == 2.0
    assert impl.factor("600001", date(2024, 7, 1)) == pytest.approx(2.0 * 1.02)


def test_no_price_cash_dividend_uses_prev_close_approximation_or_raises() -> None:
    events = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "cash_dividend",
            "payload": {"dividend": 0.5, "prev_close": 10.0},
        }
    ]

    factor = build_factor(events, start=date(2024, 6, 1), end=date(2024, 6, 4))

    assert factor_at(factor, "600001", "2024-06-03") == pytest.approx(1.05)
    assert any("approximation" in note for note in factor.attrs["assumptions"])

    missing_reference = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "cash_dividend",
            "payload": {"dividend": 0.5},
        }
    ]
    with pytest.raises(SpecError, match="requires prices or payload.prev_close"):
        build_factor(missing_reference)


def test_rights_issue_and_merge_multipliers_are_documented() -> None:
    events = [
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "rights_issue",
            "payload": {"ratio": 0.3, "price": 5.0},
        },
        {
            "code": "600001",
            "ex_date": "2024-06-03",
            "event": "merge",
            "payload": {"ratio": 1.5},
        },
    ]

    factor = build_factor(events, start=date(2024, 6, 2), end=date(2024, 6, 3))

    assert factor_at(factor, "600001", "2024-06-03") == pytest.approx(1.3 * 1.5)
    assert any("rights_issue" in note for note in factor.attrs["assumptions"])


def test_rules_file_schema_and_default_loading() -> None:
    raw = json.loads(RULES.read_text(encoding="utf-8"))
    assert set(raw["event_fields"]) == set(KNOWN_EVENTS)
    assert raw["events"]

    impl = CorporateActionsImpl.from_file(RULES)
    events = impl.actions("600001", date(2024, 1, 1), date(2026, 1, 1))

    assert [event["event"] for event in events] == [
        "cash_dividend",
        "stock_dividend",
        "split",
    ]
    built = impl.build_factor()
    # 合成演示：cash dividend 0.1 / prev_close 5.0 => 1 + 0.02。
    assert factor_at(built, "600001", "2024-06-19") == pytest.approx(1.02)
