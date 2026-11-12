"""模块 F2：交易日历与会话的契约测试。

覆盖：
* 合成交易日（跨月、跨年）的 sessions 首尾包含；
* previous_session 的周末/节假日回退（含「当日为交易日返回自身」语义）；
* 重复/乱序/周末/不可解析日期的 SpecError；
* 半日市 is_half_day=True 且普通日为 False；
* extra_sessions 的周末调休、holidays 剔除；
* CSV / Parquet 载入与规则文件载入。
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import date, time
from pathlib import Path
from typing import Iterator

import pandas as pd
import pytest

from qrg.api import CalendarProvider, Session, SpecError
from qrg.calendar import (
    AFTERNOON_OPEN,
    CALL_AUCTION_CLOSE,
    CALL_AUCTION_OPEN,
    CONTINUOUS_CLOSE,
    CONTINUOUS_OPEN,
    MORNING_CLOSE,
    TIMEZONE,
    SessionCalendar,
    SessionRules,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "rules" / "sessions.cn.json"


def business_days(start: str, end: str) -> list[date]:
    """合成工作日交易日（仅用于测试，不含真实节假日）。"""
    return [stamp.date() for stamp in pd.date_range(start, end, freq="B")]


@pytest.fixture
def trading_days() -> list[date]:
    # 覆盖 2023-12 跨月、2023/2024 跨年、2024-02 跨月。
    return business_days("2023-12-20", "2024-02-05")


@pytest.fixture
def calendar(trading_days: list[date]) -> SessionCalendar:
    return SessionCalendar(trading_days)


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    """工作区内临时目录。

    不使用 pytest ``tmp_path``：部分 Windows 沙箱环境的系统临时目录 ACL
    不可写，会让与业务无关的 fixture 失败。
    """
    base = Path(__file__).resolve().parent / "_session_artifacts"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"case-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()  # 仅在最后一个用例清理后为空时移除
        except OSError:
            pass


# ---------------------------------------------------------------- 规则文件


def test_default_rules_file_schema() -> None:
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    assert payload["timezone"] == TIMEZONE
    assert isinstance(payload["half_days"], list)
    assert isinstance(payload["extra_sessions"], list)
    assert isinstance(payload["holidays"], list)
    # 默认规则文件可被构造器直接加载。
    cal = SessionCalendar(business_days("2024-01-02", "2024-01-05"))
    assert cal.rules.timezone == TIMEZONE
    assert cal.timezone == TIMEZONE


# ---------------------------------------------------------------- sessions


def test_sessions_include_both_endpoints_across_year(calendar: SessionCalendar) -> None:
    start = date(2023, 12, 29)  # 周五
    end = date(2024, 1, 2)  # 周二（合成交易日，未扣真实元旦）
    got = calendar.sessions(start, end)
    assert [s.day for s in got] == [
        date(2023, 12, 29),
        date(2024, 1, 1),
        date(2024, 1, 2),
    ]
    assert got[0].day == start
    assert got[-1].day == end
    assert all(start <= s.day <= end for s in got)


def test_sessions_include_both_endpoints_across_month(calendar: SessionCalendar) -> None:
    start = date(2024, 1, 31)  # 周三
    end = date(2024, 2, 2)  # 周五
    got = calendar.sessions(start, end)
    assert [s.day for s in got] == [
        date(2024, 1, 31),
        date(2024, 2, 1),
        date(2024, 2, 2),
    ]


def test_sessions_only_returns_trading_days(calendar: SessionCalendar) -> None:
    # 2024-01-06/07 是周末，不得出现。
    got = calendar.sessions(date(2024, 1, 5), date(2024, 1, 8))
    assert [s.day for s in got] == [date(2024, 1, 5), date(2024, 1, 8)]


def test_sessions_outside_range_is_empty(calendar: SessionCalendar) -> None:
    assert calendar.sessions(date(2030, 1, 1), date(2030, 1, 31)) == []


def test_sessions_inverted_range_raises(calendar: SessionCalendar) -> None:
    with pytest.raises(SpecError):
        calendar.sessions(date(2024, 1, 5), date(2024, 1, 1))


def test_session_fields_match_constants(calendar: SessionCalendar) -> None:
    sess = calendar.session(date(2024, 1, 2))
    assert isinstance(sess, Session)
    assert sess.call_auction_open == CALL_AUCTION_OPEN == time(9, 15)
    assert sess.continuous_open == CONTINUOUS_OPEN == time(9, 30)
    assert sess.morning_close == MORNING_CLOSE == time(11, 30)
    assert sess.afternoon_open == AFTERNOON_OPEN == time(13, 0)
    assert sess.call_auction_close == CALL_AUCTION_CLOSE == time(14, 57)
    assert sess.continuous_close == CONTINUOUS_CLOSE == time(15, 0)
    assert sess.is_half_day is False


# ---------------------------------------------------------------- previous_session


def test_previous_session_on_trading_day_is_inclusive(calendar: SessionCalendar) -> None:
    """设计决定：previous_session 包含当日；交易日返回自身。"""
    day = date(2024, 1, 3)
    assert calendar.previous_session(day) == day


def test_previous_session_rolls_back_over_weekend(calendar: SessionCalendar) -> None:
    assert calendar.previous_session(date(2024, 1, 6)) == date(2024, 1, 5)  # 周六 -> 周五
    assert calendar.previous_session(date(2024, 1, 7)) == date(2024, 1, 5)  # 周日 -> 周五


def test_previous_session_rolls_back_over_holiday(trading_days: list[date]) -> None:
    cal = SessionCalendar(trading_days, rules={"holidays": ["2024-01-01"]})
    assert cal.is_session(date(2024, 1, 1)) is False
    assert cal.previous_session(date(2024, 1, 1)) == date(2023, 12, 29)
    # 节假日之后紧邻的交易日仍可正常回退。
    assert cal.previous_session(date(2024, 1, 2)) == date(2024, 1, 2)


def test_previous_session_before_first_session_raises(calendar: SessionCalendar) -> None:
    with pytest.raises(SpecError):
        calendar.previous_session(date(2000, 1, 1))


def test_is_session_and_membership(calendar: SessionCalendar) -> None:
    assert calendar.is_session(date(2024, 1, 2)) is True
    assert calendar.is_session(date(2024, 1, 6)) is False
    assert date(2024, 1, 2) in calendar
    assert date(2024, 1, 6) not in calendar


# ---------------------------------------------------------------- 构造校验


def test_duplicate_dates_raise() -> None:
    day = date(2024, 1, 2)
    with pytest.raises(SpecError, match="重复"):
        SessionCalendar([day, day])


def test_out_of_order_dates_raise() -> None:
    with pytest.raises(SpecError, match="严格递增"):
        SessionCalendar([date(2024, 1, 3), date(2024, 1, 2)])


def test_weekend_without_extra_session_raises() -> None:
    with pytest.raises(SpecError, match="周末"):
        SessionCalendar([date(2024, 1, 5), date(2024, 1, 6)])


def test_unparseable_date_raises() -> None:
    with pytest.raises(SpecError, match="无法解析"):
        SessionCalendar(["2024-13-01"])


def test_empty_calendar_raises() -> None:
    with pytest.raises(SpecError, match="为空"):
        SessionCalendar([])


def test_bad_timezone_raises() -> None:
    with pytest.raises(SpecError, match="时区"):
        SessionCalendar([date(2024, 1, 2)], timezone="UTC")


def test_extra_session_and_holiday_conflict_raises() -> None:
    with pytest.raises(SpecError, match="同时"):
        SessionCalendar(
            [date(2024, 1, 2)],
            rules={"extra_sessions": ["2024-01-06"], "holidays": ["2024-01-06"]},
        )


def test_protocol_conformance(calendar: SessionCalendar) -> None:
    assert isinstance(calendar, CalendarProvider)


# ---------------------------------------------------------------- 半日市


def test_half_day_sets_sentinel_and_is_half_day(trading_days: list[date]) -> None:
    """半日市语义：afternoon_open=15:00（continuous_close），触发冻结 API 的
    ``afternoon_open >= continuous_close``，因此 is_half_day=True；上午收盘仍是 11:30。
    """
    cal = SessionCalendar(trading_days, rules={"half_days": ["2024-01-02"]})
    half = cal.session(date(2024, 1, 2))
    assert half.is_half_day is True
    assert half.afternoon_open == CONTINUOUS_CLOSE == time(15, 0)
    assert half.continuous_close == time(15, 0)
    assert half.morning_close == time(11, 30)
    assert cal.is_half_day(date(2024, 1, 2)) is True

    normal = cal.session(date(2024, 1, 3))
    assert normal.is_half_day is False
    assert normal.afternoon_open == time(13, 0)
    assert cal.is_half_day(date(2024, 1, 3)) is False
    # 非交易日不是半日市。
    assert cal.is_half_day(date(2024, 1, 6)) is False


def test_half_day_not_in_trading_days_is_ignored(trading_days: list[date]) -> None:
    cal = SessionCalendar(trading_days, rules={"half_days": ["2030-01-02"]})
    assert cal.half_days == frozenset()
    assert all(not s.is_half_day for s in cal.sessions(trading_days[0], trading_days[-1]))


# ---------------------------------------------------------------- 调休 / 休市


def test_extra_weekend_session_is_trading_day(trading_days: list[date]) -> None:
    cal = SessionCalendar(
        trading_days,
        rules={"extra_sessions": ["2024-01-06"]},  # 周六调休
    )
    assert cal.is_session(date(2024, 1, 6)) is True
    got = cal.sessions(date(2024, 1, 5), date(2024, 1, 8))
    assert [s.day for s in got] == [
        date(2024, 1, 5),
        date(2024, 1, 6),
        date(2024, 1, 8),
    ]
    assert cal.previous_session(date(2024, 1, 7)) == date(2024, 1, 6)
    assert cal.is_half_day(date(2024, 1, 6)) is False


def test_extra_weekend_allowed_in_input_list(trading_days: list[date]) -> None:
    days = sorted(trading_days + [date(2024, 1, 6)])
    cal = SessionCalendar(days, rules={"extra_sessions": ["2024-01-06"]})
    assert date(2024, 1, 6) in cal


def test_holiday_is_removed(trading_days: list[date]) -> None:
    cal = SessionCalendar(trading_days, rules={"holidays": ["2024-01-03"]})
    assert cal.is_session(date(2024, 1, 3)) is False
    got = cal.sessions(date(2024, 1, 2), date(2024, 1, 4))
    assert [s.day for s in got] == [date(2024, 1, 2), date(2024, 1, 4)]
    assert cal.previous_session(date(2024, 1, 3)) == date(2024, 1, 2)


# ---------------------------------------------------------------- CSV / Parquet / 规则来源


def test_csv_with_trade_date_column(tmp_dir: Path, trading_days: list[date]) -> None:
    path = tmp_dir / "sessions.csv"
    pd.DataFrame({"trade_date": [d.isoformat() for d in trading_days]}).to_csv(
        path, index=False
    )
    cal = SessionCalendar.from_csv(path)
    assert list(cal.days) == trading_days


def test_csv_with_date_column_and_numeric_yyyymmdd(tmp_dir: Path) -> None:
    path = tmp_dir / "sessions.csv"
    pd.DataFrame({"date": [20240102, 20240103, 20240104]}).to_csv(path, index=False)
    cal = SessionCalendar(path)
    assert list(cal.days) == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def test_parquet_with_trade_date_column(tmp_dir: Path, trading_days: list[date]) -> None:
    path = tmp_dir / "sessions.parquet"
    pd.DataFrame({"trade_date": trading_days}).to_parquet(path, index=False)
    cal = SessionCalendar.from_parquet(path)
    assert list(cal.days) == trading_days


def test_csv_missing_date_column_raises(tmp_dir: Path) -> None:
    path = tmp_dir / "bad.csv"
    pd.DataFrame({"foo": [1, 2]}).to_csv(path, index=False)
    with pytest.raises(SpecError, match="缺少列"):
        SessionCalendar(path)


def test_unsupported_file_suffix_raises(tmp_dir: Path) -> None:
    path = tmp_dir / "sessions.txt"
    path.write_text("2024-01-02\n", encoding="utf-8")
    with pytest.raises(SpecError, match="格式"):
        SessionCalendar(path)


def test_rules_loaded_from_json_file(tmp_dir: Path) -> None:
    rules_file = tmp_dir / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "timezone": TIMEZONE,
                "half_days": ["2024-01-02"],
                "extra_sessions": ["2024-01-06"],
                "holidays": ["2024-01-03"],
            }
        ),
        encoding="utf-8",
    )
    cal = SessionCalendar(
        [date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)],
        rules_path=rules_file,
    )
    assert cal.session(date(2024, 1, 2)).is_half_day is True
    assert cal.is_session(date(2024, 1, 6)) is True
    assert cal.is_session(date(2024, 1, 3)) is False


def test_session_rules_instance_is_accepted() -> None:
    rules = SessionRules(
        half_days=frozenset({date(2024, 1, 2)}),
        extra_sessions=frozenset(),
        holidays=frozenset(),
    )
    cal = SessionCalendar([date(2024, 1, 2)], rules=rules)
    assert cal.session(date(2024, 1, 2)).is_half_day is True


def test_missing_rules_file_raises(tmp_dir: Path) -> None:
    with pytest.raises(SpecError, match="无法读取"):
        SessionCalendar([date(2024, 1, 2)], rules=tmp_dir / "missing.json")


def test_invalid_rules_payload_raises() -> None:
    with pytest.raises(SpecError, match="规则"):
        SessionCalendar([date(2024, 1, 2)], rules={"half_days": ["not-a-date"]})
