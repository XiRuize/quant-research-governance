"""行业日历（B7）验收用例：as-of、版本切换、冲突与成分。"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest

from qrg.api import IndustryCalendar, SpecError

ROOT = Path(__file__).resolve().parents[1]
_SESSION_PATH = ROOT / "src" / "qrg" / "calendar" / "session.py"


def _import_impl():
    """导入实现类；session 模块尚不存在时注入临时占位，避免测试被并行开发阻塞。"""

    if not _SESSION_PATH.is_file() and "qrg.calendar.session" not in sys.modules:
        stub = types.ModuleType("qrg.calendar.session")
        for name in (
            "AFTERNOON_OPEN",
            "CALL_AUCTION_CLOSE",
            "CALL_AUCTION_OPEN",
            "CONTINUOUS_CLOSE",
            "CONTINUOUS_OPEN",
            "MORNING_CLOSE",
            "TIMEZONE",
            "SessionCalendar",
            "SessionRules",
        ):
            setattr(stub, name, type(name, (), {}))
        sys.modules["qrg.calendar.session"] = stub

    from qrg.calendar.industry import IndustryCalendarImpl

    return IndustryCalendarImpl


IndustryCalendarImpl = _import_impl()

SWITCH = date(2021, 12, 13)

RULES = {
    "versions": [
        {"version": "SW2014", "effective_from": "2014-01-01", "effective_to": "2021-12-13"},
        {"version": "SW2021", "effective_from": "2021-12-13", "effective_to": None},
    ],
    "events": [
        {
            "code": "600001",
            "industry_code": "801010",
            "industry_name": "农林牧渔",
            "level": 1,
            "version": "SW2014",
            "effective_from": "2014-01-01",
            "effective_to": "2021-12-13",
        },
        {
            "code": "600001",
            "industry_code": "801080",
            "industry_name": "电子",
            "level": 1,
            "version": "SW2021",
            "effective_from": "2021-12-13",
            "effective_to": None,
        },
        {
            "code": "600002",
            "industry_code": "801010",
            "industry_name": "农林牧渔",
            "level": 1,
            "version": "SW2014",
            "effective_from": "2014-01-01",
            "effective_to": "2021-06-30",
        },
        {
            "code": "600003",
            "industry_code": "801020",
            "industry_name": "基础化工",
            "level": 1,
            "version": "SW2021",
            "effective_from": "2023-01-01",
            "effective_to": None,
        },
    ],
    "discontinued_industries": [
        {
            "industry_code": "801010",
            "industry_name": "农林牧渔",
            "level": 1,
            "version": "SW2014",
            "effective_from": "2014-01-01",
            "effective_to": "2021-12-13",
            "reason": "synthetic-demo",
        }
    ],
}


@pytest.fixture
def calendar() -> IndustryCalendarImpl:
    return IndustryCalendarImpl.from_dict(RULES)


def test_switch_day_returns_old_before_and_new_on_day(calendar: IndustryCalendarImpl) -> None:
    before = calendar.classify("600001", date(2021, 12, 12))
    on_day = calendar.classify("600001", date(2021, 12, 13))

    assert before is not None and on_day is not None
    assert before.code == "801010"
    assert before.version == "SW2014"
    assert before.effective_to == SWITCH
    assert on_day.code == "801080"
    assert on_day.version == "SW2021"
    assert on_day.name == "电子"


def test_version_at_switch_boundary(calendar: IndustryCalendarImpl) -> None:
    assert calendar.version_at(date(2021, 12, 12)) == "SW2014"
    assert calendar.version_at("2021-12-13") == "SW2021"
    assert calendar.version_at(date(2013, 12, 31)) == ""
    assert calendar.version_at("2026-01-01") == "SW2021"


def test_as_of_does_not_return_future_change(calendar: IndustryCalendarImpl) -> None:
    # 600003 在 2023-01-01 才首次有归属，查询历史日不得提前命中。
    assert calendar.classify("600003", date(2021, 12, 13)) is None
    assert calendar.classify("600003", date(2022, 12, 31)) is None
    future = calendar.classify("600003", date(2023, 1, 1))
    assert future is not None and future.code == "801020"

    # 600001 的历史归属不受 2021-12-13 变更影响。
    history = calendar.classify("600001", date(2020, 5, 1))
    assert history is not None and history.code == "801010"


def test_conflicting_records_raise_spec_error() -> None:
    conflicting = {
        "events": [
            {
                "code": "600001",
                "industry_code": "801010",
                "industry_name": "农林牧渔",
                "level": 1,
                "version": "SW2014",
                "effective_from": "2020-01-01",
                "effective_to": "2021-01-01",
            },
            {
                "code": "600001",
                "industry_code": "801080",
                "industry_name": "电子",
                "level": 1,
                "version": "SW2014",
                "effective_from": "2020-06-01",
                "effective_to": "2022-01-01",
            },
        ]
    }
    with pytest.raises(SpecError, match="conflicting"):
        IndustryCalendarImpl.from_dict(conflicting)


def test_unknown_code_and_prehistory_return_none(calendar: IndustryCalendarImpl) -> None:
    assert calendar.classify("999999", date(2021, 12, 13)) is None
    assert calendar.classify("600001", date(1990, 1, 1)) is None
    assert calendar.classify("", date(2021, 12, 13)) is None


def test_members_only_include_active_constituents(calendar: IndustryCalendarImpl) -> None:
    # 600002 在 2021-06-30 当天已不在册（effective_to 右开）。
    assert calendar.members("801010", date(2021, 6, 29)) == {"600001", "600002"}
    assert calendar.members("801010", date(2021, 6, 30)) == {"600001"}

    # 切换日当天 600001 已转入新行业，旧行业当天无成分。
    assert calendar.members("801010", date(2021, 12, 13)) == set()
    assert calendar.members("801080", date(2021, 12, 13)) == {"600001"}

    # 未来才生效的成分不得提前出现在历史成分中。
    assert calendar.members("801020", date(2021, 12, 13)) == set()
    assert calendar.members("801020", date(2023, 1, 1)) == {"600003"}


def test_missing_effective_from_and_invalid_range_raise() -> None:
    missing_from = {
        "events": [
            {
                "code": "600001",
                "industry_code": "801010",
                "industry_name": "农林牧渔",
                "level": 1,
                "version": "SW2014",
                "effective_to": "2021-01-01",
            }
        ]
    }
    with pytest.raises(SpecError):
        IndustryCalendarImpl.from_dict(missing_from)

    invalid_range = {
        "events": [
            {
                "code": "600001",
                "industry_code": "801010",
                "industry_name": "农林牧渔",
                "level": 1,
                "version": "SW2014",
                "effective_from": "2021-01-01",
                "effective_to": "2021-01-01",
            }
        ]
    }
    with pytest.raises(SpecError):
        IndustryCalendarImpl.from_dict(invalid_range)


def test_discontinued_industry_recorded(calendar: IndustryCalendarImpl) -> None:
    assert calendar.is_discontinued("801010", date(2021, 12, 12))
    assert not calendar.is_discontinued("801010", date(2021, 12, 13))
    assert [record.industry_code for record in calendar.discontinued_industries] == ["801010"]


def test_implements_protocol(calendar: IndustryCalendarImpl) -> None:
    assert isinstance(calendar, IndustryCalendar)


def test_default_rules_file_loads_and_switches() -> None:
    calendar = IndustryCalendarImpl.from_file(ROOT / "rules" / "industry.cn.json")
    assert calendar.version_at(date(2021, 12, 12)) == "SW2014"
    assert calendar.version_at(date(2021, 12, 13)) == "SW2021"

    before = calendar.classify("300750", date(2021, 12, 12))
    after = calendar.classify("300750", date(2021, 12, 13))
    assert before is not None and before.code == "801731"
    assert after is not None and after.code == "801730"
