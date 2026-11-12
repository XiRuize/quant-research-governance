"""行业日历（模块 B7）：版本化、as-of 的申万行业归属。

设计要点
--------
1. **事件表驱动、只追加**：``rules/industry.cn.json`` 记录分类版本窗口
   （``version`` + ``effective_from`` / ``effective_to``）与成分变更事件。
   每条事件描述「某股票在某段时间属于某行业」，区间为
   ``[effective_from, effective_to)``；``effective_to`` 为空表示仍然有效。
2. **严格 as-of**：``classify`` 只返回 ``effective_from <= as_of < effective_to``
   的最新一条记录；查不到返回 ``None``，绝不用当前分类回填历史，也不返回
   ``effective_from`` 晚于查询日的未来变更。
3. **版本切换边界**：``version_at`` 也按左闭右开区间判断，切换日当天返回新
   版本，切换日前一天返回旧版本；没有版本覆盖的日期返回空字符串。
4. **停用行业代码**：``discontinued_industries`` 记录已停用的行业代码及其
   有效区间，只做元数据/审计用途，不参与股票归属计算。
5. **载入即校验**：``effective_from`` 必填；``effective_to`` 必须晚于
   ``effective_from``；同一股票代码的重叠区间视为冲突并抛
   :class:`qrg.api.SpecError`；分类版本窗口重叠同样抛 ``SpecError``。
6. **纯离线、确定性**：只读取本地 JSON，不联网、不调用 LLM；同一条输入
   永远得到同一结果。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ..api import IndustryNode, SpecError

#: 默认规则文件：仓库根/rules/industry.cn.json
#: （``src/qrg/calendar/industry.py`` 上溯 3 层即仓库根）。
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "industry.cn.json"

#: 环境变量覆盖默认规则文件路径。
_RULES_ENV = "QRG_INDUSTRY_RULES"

#: 规则文件中的版本字段别名（兼容 ``versions`` / ``classification_versions``）。
_VERSION_KEYS = ("versions", "classification_versions")
#: 规则文件中的停用行业字段别名。
_DISCONTINUED_KEYS = ("discontinued_industries", "discontinued")


# ---------------------------------------------------------------- 输入校验模型


def _non_empty(value: Any, label: str) -> str:
    """把字符串去空白并校验非空；非法值交给 pydantic 转成 SpecError。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    return value.strip()


class _DatedRecord(BaseModel):
    """带左闭右开有效区间的记录基类。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _check_period(self) -> "_DatedRecord":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                "effective_to must be later than effective_from "
                f"({self.effective_from.isoformat()} -> {self.effective_to.isoformat()})"
            )
        return self


class _VersionIn(_DatedRecord):
    """分类版本窗口。"""

    version: str

    @field_validator("version", mode="before")
    @classmethod
    def _check_version(cls, value: Any) -> str:
        return _non_empty(value, "version")


class _EventIn(_DatedRecord):
    """单条成分变更事件。"""

    code: str
    industry_code: str
    industry_name: str
    level: int
    version: str

    @field_validator("code", "industry_code", "industry_name", "version", mode="before")
    @classmethod
    def _check_text(cls, value: Any, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("level", mode="before")
    @classmethod
    def _check_level(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"level must be a positive integer, got {value!r}")
        return value


class _DiscontinuedIn(_DatedRecord):
    """停用行业代码记录（元数据，不直接产生股票归属）。"""

    industry_code: str
    industry_name: str
    level: int
    version: str

    @field_validator("industry_code", "industry_name", "version", mode="before")
    @classmethod
    def _check_text(cls, value: Any, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("level", mode="before")
    @classmethod
    def _check_level(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"level must be a positive integer, got {value!r}")
        return value


# ---------------------------------------------------------------- 日期/代码工具


def _as_date(value: Any) -> date:
    """把 as_of 归一化为 :class:`datetime.date`；非法输入抛 ``SpecError``。"""

    if isinstance(value, datetime):  # 含 pandas.Timestamp
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise SpecError(f"invalid as_of date: {value!r}") from exc
    raise SpecError(f"as_of must be a date or ISO date string, got {type(value).__name__}")


def _query_code(code: Any) -> str | None:
    """查询侧股票代码归一化；非法/空值返回 ``None``（视为未知标的）。"""

    if isinstance(code, int) and not isinstance(code, bool):
        code = str(code)
    if not isinstance(code, str):
        return None
    code = code.strip()
    return code or None


def _query_industry(industry_code: Any) -> str | None:
    if not isinstance(industry_code, str):
        return None
    text = industry_code.strip()
    return text or None


def _covers(record: _DatedRecord, day: date) -> bool:
    return record.effective_from <= day and (
        record.effective_to is None or day < record.effective_to
    )


# ---------------------------------------------------------------- 规则解析


def _read_rules(source: str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        raw: Any = dict(source)
    elif isinstance(source, (str, Path)):
        path = Path(source)
        try:
            with path.open(encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as exc:
            raise SpecError(f"cannot read industry rules {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SpecError(f"invalid JSON in industry rules {path}: {exc}") from exc
    elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        raw = {"events": list(source)}
    else:
        raise SpecError("rules must be a path, mapping, or event list")

    if isinstance(raw, list):
        raw = {"events": raw}
    if not isinstance(raw, Mapping):
        raise SpecError("industry rules root must be a JSON object")
    return raw


def _first_present(raw: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return default


def _parse_records(items: Any, model: type[BaseModel], label: str) -> list[Any]:
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise SpecError(f"'{label}' must be a list")
    parsed: list[Any] = []
    for index, item in enumerate(items):
        try:
            parsed.append(model.model_validate(item))
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0] if errors else {"loc": (), "msg": str(exc)}
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = first.get("msg", "invalid record")
            raise SpecError(
                f"invalid {label} #{index}" + (f" ({location})" if location else "") + f": {message}"
            ) from exc
    return parsed


def _check_event_conflicts(events: Sequence[_EventIn]) -> None:
    """同一股票代码的有效区间不得重叠，否则 as-of 归属不唯一。"""

    grouped: dict[str, list[_EventIn]] = defaultdict(list)
    for event in events:
        grouped[event.code].append(event)

    for code in sorted(grouped):
        ordered = sorted(grouped[code], key=lambda item: (item.effective_from, item.industry_code))
        previous: _EventIn | None = None
        for current in ordered:
            if previous is not None:
                previous_to = previous.effective_to or date.max
                if current.effective_from < previous_to:
                    raise SpecError(
                        f"conflicting industry records for code {code}: "
                        f"[{previous.effective_from.isoformat()}, "
                        f"{previous.effective_to.isoformat() if previous.effective_to else '∞'}) "
                        f"overlaps [{current.effective_from.isoformat()}, "
                        f"{current.effective_to.isoformat() if current.effective_to else '∞'})"
                    )
            previous = current


def _build_version_windows(
    versions: Sequence[_VersionIn], events: Sequence[_EventIn]
) -> tuple[tuple[date, date | None, str], ...]:
    """构造版本生效窗口；规则未显式给出时按事件的版本起点推断。"""

    if versions:
        ordered = sorted(versions, key=lambda item: (item.effective_from, item.version))
        for index, current in enumerate(ordered):
            if index == 0:
                continue
            previous = ordered[index - 1]
            if previous.effective_to is None or current.effective_from < previous.effective_to:
                raise SpecError(
                    "classification version windows overlap: "
                    f"{previous.version!r} and {current.version!r}"
                )
        return tuple((item.effective_from, item.effective_to, item.version) for item in ordered)

    # 兼容只给 events 的最小规则：以每个版本最早出现的事件日作为切换日。
    starts: dict[str, date] = {}
    for event in events:
        current = starts.get(event.version)
        if current is None or event.effective_from < current:
            starts[event.version] = event.effective_from
    ordered_versions = sorted(starts.items(), key=lambda item: (item[1], item[0]))
    windows: list[tuple[date, date | None, str]] = []
    for index, (version, start) in enumerate(ordered_versions):
        end = ordered_versions[index + 1][1] if index + 1 < len(ordered_versions) else None
        windows.append((start, end, version))
    return tuple(windows)


# ---------------------------------------------------------------- 实现


class IndustryCalendarImpl:
    """版本化行业日历，实现 :class:`qrg.api.IndustryCalendar`。

    Parameters
    ----------
    rules:
        规则文件路径、规则字典，或事件列表。省略时按以下顺序寻找默认规则：
        ``$QRG_INDUSTRY_RULES`` → ``<repo>/rules/industry.cn.json`` →
        ``<cwd>/rules/industry.cn.json``。
    path:
        ``rules`` 的别名，便于 ``IndustryCalendarImpl(path=...)`` 调用。
    events:
        直接提供事件列表的便捷入口；可同时提供 ``versions`` /
        ``discontinued``。
    versions:
        分类版本窗口列表；仅在通过 ``events`` 便捷入口构造时生效。
    discontinued:
        停用行业代码列表；仅在通过 ``events`` 便捷入口构造时生效。
    """

    def __init__(
        self,
        rules: str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        *,
        path: str | Path | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        versions: Sequence[Mapping[str, Any]] | None = None,
        discontinued: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if path is not None:
            if rules is not None:
                raise SpecError("pass either rules or path, not both")
            rules = path

        if events is not None:
            if rules is not None:
                raise SpecError("pass either rules or events, not both")
            payload: dict[str, Any] = {"events": list(events)}
            if versions is not None:
                payload["versions"] = list(versions)
            if discontinued is not None:
                payload["discontinued_industries"] = list(discontinued)
            rules = payload
        elif versions is not None or discontinued is not None:
            if rules is None:
                rules = {}
            if not isinstance(rules, Mapping):
                raise SpecError(
                    "versions/discontinued overrides require rules to be a mapping"
                )
            payload = dict(rules)
            if versions is not None:
                payload["versions"] = list(versions)
            if discontinued is not None:
                payload["discontinued_industries"] = list(discontinued)
            rules = payload

        if rules is None:
            rules = self._default_rules_path()

        self._source = rules
        raw = _read_rules(rules)

        version_items = _parse_records(
            _first_present(raw, _VERSION_KEYS, []), _VersionIn, "classification version"
        )
        event_items = _parse_records(raw.get("events"), _EventIn, "industry event")
        discontinued_items = _parse_records(
            _first_present(raw, _DISCONTINUED_KEYS, []),
            _DiscontinuedIn,
            "discontinued industry",
        )

        _check_event_conflicts(event_items)

        self._versions: tuple[_VersionIn, ...] = tuple(version_items)
        self._events: tuple[_EventIn, ...] = tuple(
            sorted(event_items, key=lambda item: item.effective_from)
        )
        self._discontinued: tuple[_DiscontinuedIn, ...] = tuple(discontinued_items)
        self._version_windows = _build_version_windows(self._versions, self._events)

        by_code: dict[str, list[_EventIn]] = defaultdict(list)
        by_industry: dict[str, list[_EventIn]] = defaultdict(list)
        for event in self._events:
            by_code[event.code].append(event)
            by_industry[event.industry_code].append(event)
        self._by_code: dict[str, tuple[_EventIn, ...]] = {
            code: tuple(records) for code, records in by_code.items()
        }
        self._by_industry: dict[str, tuple[_EventIn, ...]] = {
            code: tuple(records) for code, records in by_industry.items()
        }

    # ------------------------------------------------------------ 类构造器

    @classmethod
    def from_file(cls, path: str | Path) -> "IndustryCalendarImpl":
        return cls(rules=path)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IndustryCalendarImpl":
        return cls(rules=payload)

    @classmethod
    def from_events(
        cls,
        events: Sequence[Mapping[str, Any]],
        *,
        versions: Sequence[Mapping[str, Any]] | None = None,
        discontinued: Sequence[Mapping[str, Any]] | None = None,
    ) -> "IndustryCalendarImpl":
        return cls(events=events, versions=versions, discontinued=discontinued)

    @staticmethod
    def _default_rules_path() -> Path:
        candidates: list[Path] = []
        env_path = os.environ.get(_RULES_ENV)
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(_DEFAULT_RULES_PATH)
        candidates.append(Path.cwd() / "rules" / "industry.cn.json")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise SpecError(f"industry rules file not found; searched: {searched}")

    # ------------------------------------------------------------ 查询

    def classify_arrays(self, codes: Sequence[str], days: Sequence[Any]) -> dict[str, np.ndarray]:
        """逐对 (code, day) 的向量化 as-of 查询，避免 PIT 阶段逐行调用。"""
        codes_arr = np.asarray([str(c) for c in codes], dtype=object)
        day_arr = np.asarray(days, dtype="datetime64[D]") if not isinstance(days, np.ndarray) else days
        if day_arr.dtype != np.dtype("datetime64[D]"):
            day_arr = day_arr.astype("datetime64[D]")
        n = int(codes_arr.size)
        out_code = np.full(n, "UNKNOWN", dtype=object)
        out_name = np.full(n, "", dtype=object)
        out_version = np.full(n, "", dtype=object)
        if n == 0:
            return {"industry_code": out_code, "industry_name": out_name, "industry_version": out_version}

        order = np.argsort(codes_arr, kind="stable")
        sorted_codes = codes_arr[order]
        sorted_days = day_arr[order]
        start = 0
        while start < n:
            end = start + 1
            while end < n and sorted_codes[end] == sorted_codes[start]:
                end += 1
            code = str(sorted_codes[start])
            idx = order[start:end]
            block_days = sorted_days[start:end]
            records = self._by_code.get(code, ())
            for record in sorted(records, key=lambda r: r.effective_from):
                mask = block_days >= np.datetime64(record.effective_from, "D")
                if record.effective_to is not None:
                    mask &= block_days < np.datetime64(record.effective_to, "D")
                if not mask.any():
                    continue
                out_code[idx[mask]] = record.industry_code
                out_name[idx[mask]] = record.industry_name
                out_version[idx[mask]] = record.version
            start = end
        return {"industry_code": out_code, "industry_name": out_name, "industry_version": out_version}

    def classify(self, code: str, as_of: date) -> IndustryNode | None:
        """返回股票 ``code`` 在 ``as_of`` 当天的行业归属；查不到返回 ``None``。

        严格左闭右开：只在 ``effective_from <= as_of < effective_to`` 的记录中
        取 ``effective_from`` 最新的一条，绝不返回未来变更、也不回填当前分类。
        """

        day = _as_date(as_of)
        normalised = _query_code(code)
        if normalised is None:
            return None
        records = self._by_code.get(normalised)
        if not records:
            return None

        for record in reversed(records):
            if _covers(record, day):
                return IndustryNode(
                    code=record.industry_code,
                    name=record.industry_name,
                    level=record.level,
                    version=record.version,
                    effective_from=record.effective_from,
                    effective_to=record.effective_to,
                )
        return None

    def version_at(self, as_of: date) -> str:
        """返回 ``as_of`` 当天生效的分类版本；无版本覆盖时返回空字符串。"""

        day = _as_date(as_of)
        for start, end, version in self._version_windows:
            if start <= day and (end is None or day < end):
                return version
        return ""

    def members(self, industry_code: str, as_of: date) -> set[str]:
        """返回 ``as_of`` 当天属于 ``industry_code`` 的在册股票代码集合。"""

        day = _as_date(as_of)
        normalised = _query_industry(industry_code)
        if normalised is None:
            return set()

        members: set[str] = set()
        for record in self._by_industry.get(normalised, ()):
            if not _covers(record, day):
                continue
            # 用 classify 复核「当天最新」，避免历史记录在版本切换后误算。
            node = self.classify(record.code, day)
            if node is not None and node.code == normalised:
                members.add(record.code)
        return members

    def is_discontinued(self, industry_code: str, as_of: date) -> bool:
        """``as_of`` 当天该行业代码是否已被登记为停用。"""

        day = _as_date(as_of)
        normalised = _query_industry(industry_code)
        if normalised is None:
            return False
        return any(
            record.industry_code == normalised and _covers(record, day)
            for record in self._discontinued
        )

    # ------------------------------------------------------------ 便捷访问

    @property
    def versions(self) -> tuple[_VersionIn, ...]:
        return self._versions

    @property
    def events(self) -> tuple[_EventIn, ...]:
        return self._events

    @property
    def discontinued_industries(self) -> tuple[_DiscontinuedIn, ...]:
        return self._discontinued

    @property
    def industry_codes(self) -> tuple[str, ...]:
        """规则中出现过的全部行业代码（含已停用），排序后。"""

        codes = {event.industry_code for event in self._events}
        codes.update(record.industry_code for record in self._discontinued)
        return tuple(sorted(codes))

    @property
    def version_windows(self) -> tuple[tuple[date, date | None, str], ...]:
        return self._version_windows

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"IndustryCalendarImpl(versions={len(self._versions)}, "
            f"events={len(self._events)}, "
            f"industries={len(self.industry_codes)}, "
            f"discontinued={len(self._discontinued)})"
        )
