"""交易日历与会话（模块 F2）。

本模块严格实现 :class:`qrg.api.CalendarProvider` 与 :class:`qrg.api.Session`，
不修改冻结接口。设计决定：

* **数据来源**：交易日表可以是 ``list[date]``、CSV、Parquet（列名
  ``trade_date`` 或 ``date``），也可以是已读入的 ``pandas.DataFrame``。
  构造时校验：日期可解析、非空、严格递增、无重复、无周末；
  周末只有被规则文件 ``extra_sessions`` 显式列为调休时才合法。
* **规则应用**：``rules/sessions.cn.json``（或调用方传入的 mapping / 路径 /
  :class:`SessionRules`）提供 ``half_days``、``extra_sessions``、``holidays``。
  ``extra_sessions`` 并入交易日集合，``holidays`` 从交易日集合剔除；
  同一日期同时出现在两者中视为规则冲突，抛 :class:`qrg.api.SpecError`。
* **半日市语义**：冻结的 ``Session.is_half_day`` 定义为
  ``afternoon_open >= continuous_close``。因此半日市把 ``afternoon_open``
  置为 ``15:00``（即 ``continuous_close``）作为「无下午连续竞价」的哨兵，
  上午实际最后连续交易时间仍由 ``morning_close=11:30`` 表达；其余字段保持
  标准会话常量。普通日 ``afternoon_open=13:00``，``is_half_day=False``。
* **previous_session 语义**：包含当日。``day`` 本身是交易日时返回 ``day``；
  否则返回其之前最近的交易日；区间之前没有交易日时抛 ``SpecError``。
* **时区**：只声明与校验为 ``Asia/Shanghai``（不引入 pytz/zoneinfo，
  不做任何换算），时区不匹配抛 ``SpecError``。
"""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError

from ..api import Session, SpecError

# ---------------------------------------------------------------- 会话时间常量（Asia/Shanghai）

#: 集合竞价开始
CALL_AUCTION_OPEN = time(9, 15)
#: 连续竞价开始
CONTINUOUS_OPEN = time(9, 30)
#: 上午收盘
MORNING_CLOSE = time(11, 30)
#: 下午连续竞价开始
AFTERNOON_OPEN = time(13, 0)
#: 收盘集合竞价开始
CALL_AUCTION_CLOSE = time(14, 57)
#: 收盘
CONTINUOUS_CLOSE = time(15, 0)

#: 本模块唯一支持的时区声明（只声明、不换算）。
TIMEZONE = "Asia/Shanghai"
_SUPPORTED_TIMEZONES = frozenset({TIMEZONE})

#: 默认规则文件：仓库根/rules/sessions.cn.json（由 src/qrg/calendar/session.py 上溯 3 层）。
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "sessions.cn.json"

#: 允许的交易日列名，按优先级排列。
_TRADE_DATE_COLUMNS = ("trade_date", "date")


class SessionRules(BaseModel):
    """交易日历规则（半日市 / 调休 / 休市）。

    字段均为 ISO 日期集合；pydantic 负责把 ``"YYYY-MM-DD"`` 解析为
    :class:`datetime.date`，解析失败统一转成 :class:`SpecError`。
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    timezone: str | None = None
    half_days: frozenset[date] = frozenset()
    extra_sessions: frozenset[date] = frozenset()
    holidays: frozenset[date] = frozenset()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SessionRules":
        """从 mapping 构造；结构/日期非法时抛 ``SpecError``。"""
        if not isinstance(payload, Mapping):
            raise SpecError(f"会话规则必须是 mapping，实际为 {type(payload).__name__}")
        try:
            return cls.model_validate(dict(payload))
        except ValidationError as exc:
            raise SpecError(f"会话规则不合法: {exc}") from exc


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"无法读取会话规则文件 {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"会话规则文件不是合法 JSON {path}: {exc}") from exc


def _coerce_date(value: Any) -> date:
    """把 date/datetime/pd.Timestamp/ISO 字符串/YYYYMMDD 整数解析为 date。"""
    if isinstance(value, datetime):  # 含 pd.Timestamp
        if pd.isna(value):
            raise SpecError("日期为 NaT/NaT 等价空值")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise SpecError("日期为空字符串")
        try:
            parsed = pd.to_datetime(text, errors="raise")
        except (ValueError, TypeError, OverflowError) as exc:
            raise SpecError(f"无法解析日期: {value!r}") from exc
    elif isinstance(value, int) and not isinstance(value, bool):
        try:
            parsed = pd.to_datetime(str(value), format="%Y%m%d", errors="raise")
        except (ValueError, TypeError, OverflowError) as exc:
            raise SpecError(f"无法解析日期: {value!r}") from exc
    else:
        raise SpecError(f"不支持的日期类型: {type(value).__name__} ({value!r})")
    if pd.isna(parsed):
        raise SpecError(f"无法解析日期: {value!r}")
    return parsed.date()


def _coerce_dates(values: Iterable[Any]) -> list[date]:
    return [_coerce_date(value) for value in values]


class SessionCalendar:
    """确定性交易日历；实现 :class:`qrg.api.CalendarProvider`。"""

    def __init__(
        self,
        days: Iterable[Any] | pd.DataFrame | str | Path,
        rules: SessionRules | Mapping[str, Any] | str | Path | None = None,
        *,
        rules_path: str | Path | None = None,
        timezone: str = TIMEZONE,
    ) -> None:
        if not isinstance(timezone, str) or timezone not in _SUPPORTED_TIMEZONES:
            raise SpecError(
                f"不支持的时区声明: {timezone!r}；当前仅支持 {sorted(_SUPPORTED_TIMEZONES)}"
            )
        self.timezone = timezone

        if rules is not None and rules_path is not None:
            raise SpecError("rules 与 rules_path 不能同时提供")
        if rules_path is not None:
            rules = rules_path
        self._rules = self._resolve_rules(rules)
        if self._rules.timezone is not None and self._rules.timezone != self.timezone:
            raise SpecError(
                f"规则文件时区 {self._rules.timezone!r} 与日历时区 {self.timezone!r} 不一致"
            )

        overlap = self._rules.extra_sessions & self._rules.holidays
        if overlap:
            sample = ", ".join(sorted(d.isoformat() for d in overlap))
            raise SpecError(f"日期同时被声明为 extra_sessions 与 holidays: {sample}")

        raw_days = self._load_days(days)
        self._validate_raw_days(raw_days, self._rules.extra_sessions)

        ordered = sorted(
            (set(raw_days) | set(self._rules.extra_sessions))
            - set(self._rules.holidays)
        )
        if not ordered:
            raise SpecError("应用 extra_sessions/holidays 后没有可用交易日")
        for day in ordered:
            if day.weekday() >= 5 and day not in self._rules.extra_sessions:
                raise SpecError(f"交易日集合包含未声明的周末: {day.isoformat()}")

        self._days: tuple[date, ...] = tuple(ordered)
        self._day_set: frozenset[date] = frozenset(ordered)
        self._effective_half_days: frozenset[date] = frozenset(
            day for day in self._rules.half_days if day in self._day_set
        )
        self._sessions: dict[date, Session] = {
            day: self._build_session(day) for day in self._days
        }

    # ------------------------------------------------------------ 构造辅助

    def _resolve_rules(
        self, source: SessionRules | Mapping[str, Any] | str | Path | None
    ) -> SessionRules:
        if source is None:
            if _DEFAULT_RULES_PATH.is_file():
                return SessionRules.from_mapping(_read_json(_DEFAULT_RULES_PATH))
            return SessionRules()
        if isinstance(source, SessionRules):
            return source
        if isinstance(source, (str, Path)):
            return SessionRules.from_mapping(_read_json(Path(source)))
        if isinstance(source, Mapping):
            return SessionRules.from_mapping(source)
        raise SpecError(
            "rules 必须是 SessionRules / mapping / JSON 路径，"
            f"实际为 {type(source).__name__}"
        )

    def _load_days(self, days: Iterable[Any] | pd.DataFrame | str | Path) -> list[date]:
        if isinstance(days, pd.DataFrame):
            return self._dates_from_frame(days)
        if isinstance(days, (str, Path)):
            path = Path(days)
            if not path.is_file():
                raise SpecError(f"交易日表文件不存在: {path}")
            suffix = path.suffix.lower()
            try:
                if suffix == ".csv":
                    frame = pd.read_csv(path)
                elif suffix in {".parquet", ".pq"}:
                    frame = pd.read_parquet(path)
                else:
                    raise SpecError(f"不支持的交易日表格式: {suffix or '(无扩展名)'}")
            except SpecError:
                raise
            except (OSError, ValueError, ImportError) as exc:
                raise SpecError(f"读取交易日表失败 {path}: {exc}") from exc
            return self._dates_from_frame(frame)
        try:
            return _coerce_dates(days)
        except TypeError as exc:
            raise SpecError(
                "days 必须是 list[date]、CSV/Parquet 路径或 DataFrame"
            ) from exc

    @staticmethod
    def _dates_from_frame(frame: pd.DataFrame) -> list[date]:
        for column in _TRADE_DATE_COLUMNS:
            if column in frame.columns:
                series = frame[column]
                break
        else:
            raise SpecError(
                f"交易日表缺少列 {' 或 '.join(_TRADE_DATE_COLUMNS)}；"
                f"实际列: {list(frame.columns)}"
            )
        if series.isna().any():
            raise SpecError(f"交易日表列 {column!r} 存在空值")
        try:
            if pd.api.types.is_numeric_dtype(series):
                # CSV 里 YYYYMMDD 会被读成整数，按 8 位数字解析。
                parsed = pd.to_datetime(
                    series.astype("int64").astype(str), format="%Y%m%d", errors="raise"
                )
            else:
                parsed = pd.to_datetime(series, errors="raise")
        except (ValueError, TypeError, OverflowError) as exc:
            raise SpecError(f"交易日表列 {column!r} 存在无法解析的日期: {exc}") from exc
        if parsed.isna().any():
            raise SpecError(f"交易日表列 {column!r} 存在无法解析的日期")
        return [timestamp.date() for timestamp in parsed]

    @staticmethod
    def _validate_raw_days(days: list[date], extra_sessions: frozenset[date]) -> None:
        if not days:
            raise SpecError("交易日表为空")
        seen: set[date] = set()
        for index, day in enumerate(days):
            if day in seen:
                raise SpecError(f"交易日表存在重复日期: {day.isoformat()}")
            seen.add(day)
            if index and day <= days[index - 1]:
                raise SpecError(
                    "交易日表必须严格递增: "
                    f"{days[index - 1].isoformat()} -> {day.isoformat()}"
                )
        for day in days:
            if day.weekday() >= 5 and day not in extra_sessions:
                raise SpecError(
                    f"交易日表包含周末: {day.isoformat()}；"
                    "如为调休请写入 rules.extra_sessions"
                )

    def _build_session(self, day: date) -> Session:
        afternoon_open = CONTINUOUS_CLOSE if day in self._effective_half_days else AFTERNOON_OPEN
        return Session(
            day=day,
            call_auction_open=CALL_AUCTION_OPEN,
            continuous_open=CONTINUOUS_OPEN,
            morning_close=MORNING_CLOSE,
            afternoon_open=afternoon_open,
            continuous_close=CONTINUOUS_CLOSE,
            call_auction_close=CALL_AUCTION_CLOSE,
        )

    # ------------------------------------------------------------ 类构造器

    @classmethod
    def from_csv(cls, path: str | Path, **kwargs: Any) -> "SessionCalendar":
        if Path(path).suffix.lower() != ".csv":
            raise SpecError(f"from_csv 需要 .csv 文件: {path}")
        return cls(path, **kwargs)

    @classmethod
    def from_parquet(cls, path: str | Path, **kwargs: Any) -> "SessionCalendar":
        if Path(path).suffix.lower() not in {".parquet", ".pq"}:
            raise SpecError(f"from_parquet 需要 .parquet 文件: {path}")
        return cls(path, **kwargs)

    # ------------------------------------------------------------ CalendarProvider

    def sessions(self, start: date, end: date) -> list[Session]:
        """返回 ``[start, end]`` 内的会话列表（含首尾，按日期升序）。"""
        start_day = _coerce_date(start)
        end_day = _coerce_date(end)
        if start_day > end_day:
            raise SpecError(
                f"sessions 区间起点 {start_day.isoformat()} 晚于终点 {end_day.isoformat()}"
            )
        left = bisect_left(self._days, start_day)
        right = bisect_right(self._days, end_day)
        return [self._sessions[day] for day in self._days[left:right]]

    def is_session(self, day: date) -> bool:
        return _coerce_date(day) in self._day_set

    def previous_session(self, day: date) -> date:
        """返回 ``<= day`` 的最近交易日；``day`` 本身为交易日则返回自身。"""
        target = _coerce_date(day)
        index = bisect_right(self._days, target) - 1
        if index < 0:
            raise SpecError(f"{target.isoformat()} 之前没有交易日")
        return self._days[index]

    # ------------------------------------------------------------ 便捷访问

    @property
    def days(self) -> tuple[date, ...]:
        return self._days

    @property
    def rules(self) -> SessionRules:
        return self._rules

    @property
    def half_days(self) -> frozenset[date]:
        """命中的半日市（仅保留实际交易日）。"""
        return self._effective_half_days

    def session(self, day: date) -> Session:
        """返回某交易日的会话；非交易日抛 ``SpecError``。"""
        target = _coerce_date(day)
        try:
            return self._sessions[target]
        except KeyError as exc:
            raise SpecError(f"{target.isoformat()} 不是交易日") from exc

    def is_half_day(self, day: date) -> bool:
        return _coerce_date(day) in self._effective_half_days

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, day: object) -> bool:
        try:
            return _coerce_date(day) in self._day_set
        except SpecError:
            return False

    def __repr__(self) -> str:
        return (
            f"SessionCalendar(days={len(self._days)}, "
            f"{self._days[0].isoformat()}..{self._days[-1].isoformat()}, "
            f"timezone={self.timezone!r})"
        )
