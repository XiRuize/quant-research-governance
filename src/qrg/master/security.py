"""证券主数据（F1）：事件表驱动的 :class:`qrg.api.SecurityMaster` 实现。

设计要点
--------
1. **事件表驱动、只追加**：证券生命周期由 ``listed / delisted / renamed /
   board_change / st_on / st_off`` 六类事件描述；任何查询按 ``effective_date``
   回放事件，绝不用当前快照回填历史。
2. **as-of 语义**：事件在 ``effective_date`` 当天生效。``listed`` 当天即可见；
   ``delisted`` 当天即不可见；``st_on`` / ``st_off`` 同样当天生效。
3. **代码复用**：同一 6 位代码退市后重新上市会生成新的 ``listing_id`` 与独立的
   ``_Lifecycle``；两段生命周期之间的空档 ``state()`` 返回 ``None``，不会把两段
   拼成一条连续序列。``listing_id(code, as_of)`` 可查询某时点所属生命周期，
   ``detect_reuse()`` 返回所有发生过复用的代码。
4. **板别与交易单位**：板别按代码前缀推断（60/00→main，68→star，
   30→chinext，8/4/920→bse）；``lot_size`` / ``tick_size`` 可被规则文件的
   ``defaults``、``board_overrides``、``code_overrides`` 以及 listed 事件
   payload 覆盖，优先级由高到低为：
   ``code_overrides`` > listed payload > ``board_overrides`` > ``defaults``。
5. **纯离线、确定性**：只读取本地 JSON，不访问网络、不调用 LLM；事件排序键为
   ``(effective_date, 原始行号)``，同一输入永远得到同一结果。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..api import CODE_LENGTH, SecurityState, SpecError

# ---------------------------------------------------------------- 常量

KNOWN_EVENTS: tuple[str, ...] = (
    "listed",
    "delisted",
    "renamed",
    "board_change",
    "st_on",
    "st_off",
)

KNOWN_BOARDS: tuple[str, ...] = ("main", "star", "chinext", "bse")

DEFAULT_BOARD_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "main": ("60", "00"),
    "star": ("68",),
    "chinext": ("30",),
    "bse": ("8", "4", "920"),
}

DEFAULT_LOT_SIZE = 100
DEFAULT_TICK_SIZE = 0.01

_EVENT_SET = frozenset(KNOWN_EVENTS)


# ---------------------------------------------------------------- 输入校验模型


class _EventIn(BaseModel):
    """单条事件的入参校验；结构错误统一转换为 :class:`SpecError`。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    event: str
    effective_date: date
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) != CODE_LENGTH or not value.isdigit():
            raise ValueError(f"code must be {CODE_LENGTH} decimal digits, got {value!r}")
        return value

    @field_validator("event")
    @classmethod
    def _check_event(cls, value: str) -> str:
        if value not in _EVENT_SET:
            raise ValueError(f"unknown event {value!r}; expected one of {sorted(_EVENT_SET)}")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_default(cls, value: Any) -> Any:
        # payload 允许省略/为 null，统一成空字典，便于向前兼容。
        return {} if value is None else value


# ---------------------------------------------------------------- 内部结构


@dataclass
class _Event:
    code: str
    event: str
    effective_date: date
    payload: dict[str, Any]
    index: int


@dataclass
class _Lifecycle:
    """一段连续的上市生命周期；代码复用会产生多个实例。"""

    code: str
    listing_id: str
    listed_on: date
    delisted_on: date | None = None
    listing_name: str = ""
    listing_board: str = ""
    listing_lot_size: int | None = None
    listing_tick_size: float | None = None
    # (effective_date, 原始行号, value)，按生效时间升序追加。
    names: list[tuple[date, int, str]] = field(default_factory=list)
    boards: list[tuple[date, int, str]] = field(default_factory=list)
    st_events: list[tuple[date, int, bool]] = field(default_factory=list)


# ---------------------------------------------------------------- 工具函数


def _as_date(value: Any) -> date:
    """把 as_of 归一化为 ``date``；非法输入抛 :class:`SpecError`。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise SpecError(f"invalid as_of date: {value!r}") from exc
    raise SpecError(f"as_of must be a date or ISO date string, got {type(value).__name__}")


def _query_code(code: Any) -> str | None:
    """查询侧代码归一化；格式不合法返回 ``None``（视为未知标的）。"""

    if not isinstance(code, str):
        if isinstance(code, int) and not isinstance(code, bool):
            code = str(code)
        else:
            return None
    code = code.strip()
    if len(code) != CODE_LENGTH or not code.isdigit():
        return None
    return code


def _latest(
    timeline: Sequence[tuple[date, int, Any]],
    day: date,
    default: Any = None,
) -> Any:
    """取 ``timeline`` 中生效日 <= day 的最后一个值（时间线按生效日升序）。"""

    for event_day, _seq, value in reversed(timeline):
        if event_day <= day:
            return value
    return default


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{field_name} must be a positive integer")
    if float(value) != int(value) or int(value) <= 0:
        raise SpecError(f"{field_name} must be a positive integer")
    return int(value)


def _positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{field_name} must be a positive number")
    number = float(value)
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        raise SpecError(f"{field_name} must be a positive finite number")
    return number


# ---------------------------------------------------------------- 实现


class SecurityMasterImpl:
    """事件表驱动的证券主数据实现（模块 F1）。

    Parameters
    ----------
    rules:
        规则文件路径、规则字典，或事件列表。省略时按以下顺序寻找默认规则：
        ``$QRG_SECURITY_RULES`` → ``<repo>/rules/security.cn.json`` →
        ``<cwd>/rules/security.cn.json``。
    path:
        ``rules`` 的别名，便于 ``SecurityMasterImpl(path=...)`` 调用。
    events:
        直接提供事件列表的便捷入口，等价于 ``{"events": events}``。
    """

    def __init__(
        self,
        rules: str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        *,
        path: str | Path | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if path is not None:
            if rules is not None:
                raise SpecError("pass either rules or path, not both")
            rules = path
        if events is not None:
            if rules is not None:
                raise SpecError("pass either rules or events, not both")
            rules = {"events": events}
        if rules is None:
            rules = self._default_rules_path()

        self._source = rules
        raw = self._read_rules(rules)
        self._defaults, self._board_overrides, self._code_overrides, self._board_prefixes = (
            self._parse_config(raw)
        )

        events_raw = raw.get("events")
        if not isinstance(events_raw, list):
            raise SpecError("rules must contain an 'events' list")
        self._events = self._parse_events(events_raw)
        self._by_code: dict[str, list[_Lifecycle]] = {}
        self._replay(self._events)

    # ------------------------------------------------------------ 加载

    @classmethod
    def from_file(cls, path: str | Path) -> "SecurityMasterImpl":
        return cls(rules=path)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SecurityMasterImpl":
        return cls(rules=payload)

    @staticmethod
    def _default_rules_path() -> Path:
        candidates: list[Path] = []
        env_path = os.environ.get("QRG_SECURITY_RULES")
        if env_path:
            candidates.append(Path(env_path))
        # src/qrg/master/security.py → parents[3] 为仓库根。
        candidates.append(Path(__file__).resolve().parents[3] / "rules" / "security.cn.json")
        candidates.append(Path.cwd() / "rules" / "security.cn.json")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(c) for c in candidates)
        raise SpecError(f"security rules file not found; searched: {searched}")

    @staticmethod
    def _read_rules(rules: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
        if isinstance(rules, Mapping):
            raw: Any = dict(rules)
        elif isinstance(rules, (str, Path)):
            path = Path(rules)
            try:
                with path.open(encoding="utf-8") as fh:
                    raw = json.load(fh)
            except OSError as exc:
                raise SpecError(f"cannot read security rules {path}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise SpecError(f"invalid JSON in security rules {path}: {exc}") from exc
        else:
            raise SpecError("rules must be a path, mapping, or event list")

        if isinstance(raw, list):
            raw = {"events": raw}
        if not isinstance(raw, Mapping):
            raise SpecError("security rules root must be a JSON object")
        return raw

    # ------------------------------------------------------------ 配置

    def _parse_config(
        self, raw: Mapping[str, Any]
    ) -> tuple[dict[str, float | int], dict[str, dict[str, float | int]], dict[str, dict[str, float | int]], dict[str, tuple[str, ...]]]:
        defaults_raw = raw.get("defaults", {})
        if defaults_raw is None:
            defaults_raw = {}
        if not isinstance(defaults_raw, Mapping):
            raise SpecError("'defaults' must be an object")
        defaults: dict[str, float | int] = {
            "lot_size": _positive_int(defaults_raw.get("lot_size", DEFAULT_LOT_SIZE), "defaults.lot_size"),
            "tick_size": _positive_float(
                defaults_raw.get("tick_size", DEFAULT_TICK_SIZE), "defaults.tick_size"
            ),
        }

        board_raw = raw["board_overrides"] if "board_overrides" in raw else raw.get("boards", {})
        if board_raw is None:
            board_raw = {}
        if not isinstance(board_raw, Mapping):
            raise SpecError("'board_overrides' must be an object")
        board_overrides: dict[str, dict[str, float | int]] = {}
        for board, options in board_raw.items():
            if board not in KNOWN_BOARDS:
                raise SpecError(f"unknown board {board!r} in board_overrides")
            board_overrides[str(board)] = self._parse_options(options, f"board_overrides.{board}")

        code_raw = raw["code_overrides"] if "code_overrides" in raw else raw.get("codes", {})
        if code_raw is None:
            code_raw = {}
        if not isinstance(code_raw, Mapping):
            raise SpecError("'code_overrides' must be an object")
        code_overrides: dict[str, dict[str, float | int]] = {}
        for code, options in code_raw.items():
            code_str = str(code)
            if len(code_str) != CODE_LENGTH or not code_str.isdigit():
                raise SpecError(f"code_overrides key must be {CODE_LENGTH} digits, got {code!r}")
            code_overrides[code_str] = self._parse_options(options, f"code_overrides.{code_str}")

        prefixes: dict[str, tuple[str, ...]] = dict(DEFAULT_BOARD_PREFIXES)
        prefixes_raw = raw.get("board_prefixes")
        if prefixes_raw is not None:
            if not isinstance(prefixes_raw, Mapping):
                raise SpecError("'board_prefixes' must be an object")
            for board, values in prefixes_raw.items():
                if board not in KNOWN_BOARDS:
                    raise SpecError(f"unknown board {board!r} in board_prefixes")
                if not isinstance(values, (list, tuple)) or not values:
                    raise SpecError(f"board_prefixes.{board} must be a non-empty list")
                parsed: list[str] = []
                for value in values:
                    if not isinstance(value, str) or not value or not value.isdigit():
                        raise SpecError(f"board_prefixes.{board} entries must be digit strings")
                    parsed.append(value)
                prefixes[str(board)] = tuple(parsed)

        return defaults, board_overrides, code_overrides, prefixes

    @staticmethod
    def _parse_options(options: Any, label: str) -> dict[str, float | int]:
        if not isinstance(options, Mapping):
            raise SpecError(f"{label} must be an object")
        parsed: dict[str, float | int] = {}
        if "lot_size" in options:
            parsed["lot_size"] = _positive_int(options["lot_size"], f"{label}.lot_size")
        if "tick_size" in options:
            parsed["tick_size"] = _positive_float(options["tick_size"], f"{label}.tick_size")
        return parsed

    # ------------------------------------------------------------ 事件解析与回放

    def _parse_events(self, items: list[Any]) -> list[_Event]:
        parsed: list[_Event] = []
        for index, item in enumerate(items):
            try:
                model = _EventIn.model_validate(item)
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
                location = ".".join(str(part) for part in first.get("loc", ()))
                message = first.get("msg", "invalid event")
                raise SpecError(f"invalid event #{index} ({location}): {message}") from exc
            payload = self._normalise_payload(model.event, model.payload, index)
            parsed.append(_Event(model.code, model.event, model.effective_date, payload, index))
        return parsed

    @staticmethod
    def _normalise_payload(event: str, payload: Mapping[str, Any], index: int) -> dict[str, Any]:
        out = dict(payload)
        if event == "listed":
            name = out.get("name")
            if name is not None and not isinstance(name, str):
                raise SpecError(f"event #{index}: listed payload.name must be a string")
            board = out.get("board")
            if board is not None and board not in KNOWN_BOARDS:
                raise SpecError(f"event #{index}: unknown board {board!r}")
            listing_id = out.get("listing_id")
            if listing_id is not None and (not isinstance(listing_id, str) or not listing_id.strip()):
                raise SpecError(f"event #{index}: listed payload.listing_id must be a non-empty string")
            if "lot_size" in out:
                out["lot_size"] = _positive_int(out["lot_size"], f"event #{index}.payload.lot_size")
            if "tick_size" in out:
                out["tick_size"] = _positive_float(
                    out["tick_size"], f"event #{index}.payload.tick_size"
                )
        elif event == "renamed":
            name = out.get("name", out.get("new_name"))
            if not isinstance(name, str) or not name.strip():
                raise SpecError(f"event #{index}: renamed payload requires a non-empty 'name'")
            out["name"] = name
        elif event == "board_change":
            board = out.get("board", out.get("new_board"))
            if board not in KNOWN_BOARDS:
                raise SpecError(
                    f"event #{index}: board_change payload.board must be one of {list(KNOWN_BOARDS)}"
                )
            out["board"] = board
        return out

    def _replay(self, events: Sequence[_Event]) -> None:
        opened: dict[str, _Lifecycle] = {}
        counters: dict[str, int] = {}
        used_listing_ids: set[str] = set()

        for event in sorted(events, key=lambda ev: (ev.effective_date, ev.index)):
            code = event.code
            day = event.effective_date
            payload = event.payload
            index = event.index

            if event.event == "listed":
                if code in opened:
                    raise SpecError(
                        f"event #{index}: {code} listed again on {day} before being delisted"
                    )
                counter = counters.get(code, 0) + 1
                counters[code] = counter
                listing_id = payload.get("listing_id") or f"{code}-L{counter}"
                if listing_id in used_listing_ids:
                    raise SpecError(f"event #{index}: duplicate listing_id {listing_id!r}")
                used_listing_ids.add(listing_id)
                board = payload.get("board") or self._infer_board(code)
                if board is None:
                    raise SpecError(
                        f"event #{index}: cannot infer board for {code}; "
                        "set payload.board or rules.board_prefixes"
                    )
                lifecycle = _Lifecycle(
                    code=code,
                    listing_id=listing_id,
                    listed_on=day,
                    listing_name=payload.get("name") or "",
                    listing_board=board,
                    listing_lot_size=payload.get("lot_size"),
                    listing_tick_size=payload.get("tick_size"),
                )
                self._by_code.setdefault(code, []).append(lifecycle)
                opened[code] = lifecycle
                continue

            lifecycle = opened.get(code)
            if lifecycle is None:
                raise SpecError(
                    f"event #{index}: {event.event} for {code} on {day} "
                    "has no open listing lifecycle"
                )

            if event.event == "delisted":
                if day < lifecycle.listed_on:
                    raise SpecError(f"event #{index}: delisted date precedes listed date")
                lifecycle.delisted_on = day
                del opened[code]
            elif event.event == "renamed":
                lifecycle.names.append((day, index, payload["name"]))
            elif event.event == "board_change":
                lifecycle.boards.append((day, index, payload["board"]))
            elif event.event == "st_on":
                lifecycle.st_events.append((day, index, True))
            elif event.event == "st_off":
                lifecycle.st_events.append((day, index, False))
            else:  # pragma: no cover - _EventIn 已限制枚举
                raise SpecError(f"event #{index}: unsupported event {event.event!r}")

    # ------------------------------------------------------------ 板别与交易参数

    def _infer_board(self, code: str) -> str | None:
        for board in KNOWN_BOARDS:
            for prefix in self._board_prefixes.get(board, ()):
                if code.startswith(prefix):
                    return board
        return None

    def _lot_size(self, code: str, board: str, lifecycle: _Lifecycle) -> int:
        override = self._code_overrides.get(code, {})
        if "lot_size" in override:
            return int(override["lot_size"])
        if lifecycle.listing_lot_size is not None:
            return int(lifecycle.listing_lot_size)
        board_override = self._board_overrides.get(board, {})
        if "lot_size" in board_override:
            return int(board_override["lot_size"])
        return int(self._defaults["lot_size"])

    def _tick_size(self, code: str, board: str, lifecycle: _Lifecycle) -> float:
        override = self._code_overrides.get(code, {})
        if "tick_size" in override:
            return float(override["tick_size"])
        if lifecycle.listing_tick_size is not None:
            return float(lifecycle.listing_tick_size)
        board_override = self._board_overrides.get(board, {})
        if "tick_size" in board_override:
            return float(board_override["tick_size"])
        return float(self._defaults["tick_size"])

    # ------------------------------------------------------------ 查询

    def _active(self, code: str, day: date) -> _Lifecycle | None:
        for lifecycle in reversed(self._by_code.get(code, ())):
            if lifecycle.listed_on <= day and (
                lifecycle.delisted_on is None or day < lifecycle.delisted_on
            ):
                return lifecycle
        return None

    def state(self, code: str, as_of: date) -> SecurityState | None:
        """返回 ``as_of`` 当天该代码的生命周期状态；空档/未上市返回 ``None``。"""

        day = _as_date(as_of)
        normalised = _query_code(code)
        if normalised is None:
            return None
        lifecycle = self._active(normalised, day)
        if lifecycle is None:
            return None

        name = _latest(lifecycle.names, day)
        if name is None:
            name = lifecycle.listing_name
        board = _latest(lifecycle.boards, day)
        if board is None:
            board = lifecycle.listing_board
        is_st = bool(_latest(lifecycle.st_events, day, default=False))
        st_status = "known" if lifecycle.st_events else "unknown"

        return SecurityState(
            code=normalised,
            name=name,
            board=board,
            listed_on=lifecycle.listed_on,
            delisted_on=lifecycle.delisted_on,
            is_st=is_st,
            lot_size=self._lot_size(normalised, board, lifecycle),
            tick_size=self._tick_size(normalised, board, lifecycle),
            st_status=st_status,
        )

    def listable(self, as_of: date) -> set[str]:
        """返回 ``as_of`` 当天存续（未退市）的全部代码。"""

        day = _as_date(as_of)
        return {
            code
            for code in sorted(self._by_code)
            if self._active(code, day) is not None
        }

    def listing_id(self, code: str, as_of: date) -> str | None:
        """返回 ``as_of`` 所属生命周期标识；空档返回 ``None``。

        ``SecurityState`` 是冻结契约、不含 ``listing_id`` 字段，因此复用识别
        通过本方法暴露，供测试与上层区分同一代码的不同生命周期。
        """

        day = _as_date(as_of)
        normalised = _query_code(code)
        if normalised is None:
            return None
        lifecycle = self._active(normalised, day)
        return lifecycle.listing_id if lifecycle is not None else None

    def detect_reuse(self) -> list[str]:
        """返回发生过代码复用的代码列表（同一代码有多段生命周期），按代码排序。"""

        return [
            code
            for code in sorted(self._by_code)
            if len(self._by_code[code]) > 1
        ]

    @property
    def codes(self) -> tuple[str, ...]:
        """事件表中出现过的全部代码（排序后）。"""

        return tuple(sorted(self._by_code))

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"SecurityMasterImpl(codes={len(self._by_code)}, "
            f"events={len(self._events)}, reused={len(self.detect_reuse())})"
        )
