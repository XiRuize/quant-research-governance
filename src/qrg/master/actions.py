"""公司行动（F3）：事件表驱动的逐日累计复权因子。

设计要点
--------
1. **口径**：因子采用后复权口径，与 ``spec/cleaning.spec.json`` 的
   ``factor = close_hfq / close_raw`` 一致::

       F_t = Π multiplier(event)   对所有 ex_date <= t 的事件
       close_hfq = close_raw * F_t
       adj(b)   = close_raw * F_t / F_b

   首个事件之前 ``F = 1``。10 送 10（每股送 1 股）令 ``F`` 从 1 变为 2；
   1 拆 2（``split.ratio = 2``）同样令 ``F`` 乘以 2。
2. **事件字段**（canonical 字段名见 ``rules/actions.cn.json``）：

   ==================  ==========================================
   event               payload
   ==================  ==========================================
   cash_dividend       ``dividend``（每股税前派现）；可选
                       ``prev_close``（无 prices 时用于近似）
   stock_dividend      ``shares``（每股送股数；10 送 10 写 1.0）
   rights_issue        ``ratio``（每股配股数）、``price``（配股价）
   split               ``ratio``（拆股比例；1 拆 2 写 2.0）
   merge               ``ratio``（换股比例；1 股换 1.5 股写 1.5）
   ==================  ==========================================

3. **现金分红**：提供 ``prices`` 时使用精确比例
   ``close_prev / (close_prev - dividend)``；未提供价格但 payload 带
   ``prev_close`` 时使用近似 ``1 + dividend / prev_close``。两者都没有时
   直接抛 :class:`SpecError`，绝不静默把现金分红当成 1。
4. **配股**：按需求使用 ``1 + ratio`` 近似，**不**使用配股价重算除权价，
   因此配股事件属于近似口径（``build_factor(...).attrs["assumptions"]``
   会显式记录）。
5. **确定性**：事件先按 ``(code, ex_date, event, payload)`` 排序并去除
   完全重复项，再计算累计乘积；乱序输入、重复 ex_date 都得到同一结果，
   同一事件重复出现不会重复乘。
6. **纯离线**：只依赖 stdlib + pandas + pydantic，不访问网络/LLM。
"""

from __future__ import annotations

import json
import math
import os
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..api import CODE_LENGTH, SpecError

# ---------------------------------------------------------------- 常量

KNOWN_EVENTS: tuple[str, ...] = (
    "cash_dividend",
    "stock_dividend",
    "rights_issue",
    "split",
    "merge",
)

#: 同一天多个事件时的稳定排序顺序（乘法可交换，但排序保证可复现）。
EVENT_ORDER: Mapping[str, int] = {name: index for index, name in enumerate(KNOWN_EVENTS)}

#: 规则文件/文档使用的 canonical payload 字段。
EVENT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "cash_dividend": ("dividend", "prev_close"),
    "stock_dividend": ("shares",),
    "rights_issue": ("ratio", "price"),
    "split": ("ratio",),
    "merge": ("ratio",),
}

DEFAULT_TOLERANCE = 1e-4
FACTOR_COLUMNS: tuple[str, ...] = ("code", "trade_date", "factor")

FACTOR_DEFINITION = (
    "cumulative hfq factor F_t = product of event multipliers with ex_date <= t; "
    "F = 1 before the first event; close_hfq = close_raw * F; "
    "adj(b) = close_raw * F_t / F_b"
)

_EVENT_SET = frozenset(KNOWN_EVENTS)


# ---------------------------------------------------------------- 输入校验


class _EventIn(BaseModel):
    """单条公司行动事件的入参校验。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    ex_date: date
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", mode="before")
    @classmethod
    def _check_code(cls, value: Any) -> str:
        try:
            return _validate_code(value, "action event code")
        except SpecError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("event")
    @classmethod
    def _check_event(cls, value: str) -> str:
        if value not in _EVENT_SET:
            raise ValueError(
                f"unknown event {value!r}; expected one of {sorted(_EVENT_SET)}"
            )
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_default(cls, value: Any) -> Any:
        return {} if value is None else value


@dataclass(frozen=True)
class _Event:
    code: str
    ex_date: date
    event: str
    payload: dict[str, Any]
    index: int


# ---------------------------------------------------------------- 基础工具


def _validate_code(value: Any, label: str = "code") -> str:
    """把代码归一化为 6 位数字字符串；非法输入抛 :class:`SpecError`。"""

    if isinstance(value, bool):
        raise SpecError(f"{label} must be a {CODE_LENGTH}-digit string")
    if isinstance(value, int):
        value = f"{value:0{CODE_LENGTH}d}"
    if not isinstance(value, str):
        raise SpecError(f"{label} must be a {CODE_LENGTH}-digit string, got {value!r}")
    value = value.strip()
    if len(value) != CODE_LENGTH or not value.isdigit():
        raise SpecError(
            f"{label} must be exactly {CODE_LENGTH} decimal digits, got {value!r}"
        )
    return value


def _as_date(value: Any, label: str = "date") -> date:
    """把 date/datetime/ISO 字符串归一化为 ``date``。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise SpecError(f"invalid {label}: {value!r}") from exc
    raise SpecError(f"{label} must be a date or ISO date string, got {type(value).__name__}")


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    """有限数值校验；``positive=True`` 时要求 > 0，否则要求 >= 0。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{label} must be a finite number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise SpecError(f"{label} must be a finite number, got {value!r}")
    if positive and number <= 0:
        raise SpecError(f"{label} must be > 0, got {value!r}")
    if not positive and number < 0:
        raise SpecError(f"{label} must be >= 0, got {value!r}")
    return number


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _default_rules_path() -> Path:
    candidates: list[Path] = []
    env_path = os.environ.get("QRG_ACTIONS_RULES")
    if env_path:
        candidates.append(Path(env_path))
    # src/qrg/master/actions.py -> parents[3] 为仓库根。
    candidates.append(Path(__file__).resolve().parents[3] / "rules" / "actions.cn.json")
    candidates.append(Path.cwd() / "rules" / "actions.cn.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise SpecError(f"actions rules file not found; searched: {searched}")


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise SpecError(f"cannot read actions rules {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"invalid JSON in actions rules {path}: {exc}") from exc


# ---------------------------------------------------------------- payload 解析


def _pick(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _normalise_payload(event: str, payload: Mapping[str, Any], index: int) -> dict[str, Any]:
    """把事件 payload 归一化为 canonical 字段，并做数值校验。"""

    if not isinstance(payload, Mapping):
        raise SpecError(f"action event #{index}: payload must be an object")
    raw = dict(payload)
    label = f"action event #{index}"

    if event == "cash_dividend":
        dividend_raw = _pick(raw, "dividend", "cash_per_share", "per_share")
        if dividend_raw is None:
            raise SpecError(f"{label}: cash_dividend payload requires 'dividend'")
        out = {"dividend": _number(dividend_raw, f"{label}.payload.dividend")}
        prev_close_raw = _pick(raw, "prev_close", "price")
        if prev_close_raw is not None:
            out["prev_close"] = _number(
                prev_close_raw, f"{label}.payload.prev_close", positive=True
            )
        return out

    if event == "stock_dividend":
        shares_raw = _pick(raw, "shares", "per_share", "ratio")
        if shares_raw is None:
            raise SpecError(f"{label}: stock_dividend payload requires 'shares'")
        return {"shares": _number(shares_raw, f"{label}.payload.shares")}

    if event == "rights_issue":
        ratio_raw = _pick(raw, "ratio", "shares")
        price_raw = _pick(raw, "price", "subscription_price")
        if ratio_raw is None or price_raw is None:
            raise SpecError(
                f"{label}: rights_issue payload requires both 'ratio' and 'price'"
            )
        return {
            "ratio": _number(ratio_raw, f"{label}.payload.ratio"),
            "price": _number(price_raw, f"{label}.payload.price", positive=True),
        }

    if event == "split":
        ratio_raw = _pick(raw, "ratio", "split_ratio")
        if ratio_raw is None:
            raise SpecError(f"{label}: split payload requires 'ratio'")
        return {"ratio": _number(ratio_raw, f"{label}.payload.ratio", positive=True)}

    if event == "merge":
        ratio_raw = _pick(raw, "ratio", "exchange_ratio")
        if ratio_raw is None:
            raise SpecError(f"{label}: merge payload requires 'ratio'")
        return {"ratio": _number(ratio_raw, f"{label}.payload.ratio", positive=True)}

    # _EventIn 已限制枚举，这里只做防御性兜底。
    raise SpecError(f"{label}: unsupported event {event!r}")


# ---------------------------------------------------------------- 事件加载


def _load_events(source: Any) -> list[_Event]:
    """从路径 / 字典 / 列表加载并规范化事件，去重后稳定排序。"""

    if isinstance(source, (str, Path)):
        raw: Any = _read_json(Path(source))
    elif isinstance(source, Mapping):
        raw = dict(source)
    elif isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        raw = list(source)
    else:
        raise SpecError("events must be a path, mapping, or event list")

    if isinstance(raw, Mapping):
        if "events" in raw:
            items_raw = raw["events"]
        elif "code" in raw and "ex_date" in raw:
            items_raw = [raw]
        else:
            raise SpecError("actions rules object must contain an 'events' list")
    else:
        items_raw = raw

    if not isinstance(items_raw, list):
        raise SpecError("'events' must be a list")

    parsed: list[_Event] = []
    seen: set[tuple[Any, ...]] = set()
    for index, item in enumerate(items_raw):
        if not isinstance(item, Mapping):
            raise SpecError(f"action event #{index}: must be an object")
        try:
            model = _EventIn.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = first.get("msg", "invalid event")
            raise SpecError(
                f"invalid action event #{index} ({location}): {message}"
            ) from exc

        payload = _normalise_payload(model.event, model.payload, index)
        event = _Event(model.code, model.ex_date, model.event, payload, index)
        key = (event.code, event.ex_date, event.event, _canonical_json(payload))
        if key in seen:
            # 完全重复的事件只保留一条，避免重复乘。
            continue
        seen.add(key)
        parsed.append(event)

    parsed.sort(
        key=lambda ev: (
            ev.code,
            ev.ex_date,
            EVENT_ORDER[ev.event],
            _canonical_json(ev.payload),
        )
    )
    return parsed


def _event_mapping(event: _Event) -> dict[str, Any]:
    return {
        "code": event.code,
        "ex_date": event.ex_date,
        "event": event.event,
        "payload": dict(event.payload),
    }


# ---------------------------------------------------------------- 价格辅助


def _normalise_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """校验并规范化参考价格表，至少包含 code/trade_date/close。"""

    if not isinstance(prices, pd.DataFrame):
        raise SpecError("prices must be a pandas DataFrame")
    required = ("code", "trade_date", "close")
    missing = [name for name in required if name not in prices.columns]
    if missing:
        raise SpecError(f"prices missing required columns: {missing}")

    frame = prices.loc[:, list(required)].copy()
    try:
        frame["code"] = frame["code"].map(lambda value: _validate_code(value, "prices.code"))
    except SpecError:
        raise

    try:
        parsed_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    except (TypeError, ValueError) as exc:
        raise SpecError(f"prices.trade_date cannot be parsed: {exc}") from exc
    if parsed_dates.isna().any():
        raise SpecError("prices.trade_date contains unparseable values")
    frame["trade_date"] = parsed_dates.dt.normalize().astype("datetime64[ns]")

    close = pd.to_numeric(frame["close"], errors="coerce")
    if close.isna().any() or not close.map(math.isfinite).all():
        raise SpecError("prices.close must contain finite numbers")
    if bool((close <= 0).any()):
        raise SpecError("prices.close must be > 0")
    frame["close"] = close.astype(float)

    if bool(frame.duplicated(subset=["code", "trade_date"]).any()):
        raise SpecError("prices contains duplicate (code, trade_date) rows")

    return frame.sort_values(["code", "trade_date"], kind="mergesort").reset_index(drop=True)


class _PriceBook:
    """按代码索引的参考价格，用于取除权日之前的最近收盘价。"""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._dates: dict[str, list[date]] = {}
        self._closes: dict[str, list[float]] = {}
        self._timestamps: dict[str, list[pd.Timestamp]] = {}
        for code, group in frame.groupby("code", sort=True):
            self._dates[str(code)] = [timestamp.date() for timestamp in group["trade_date"]]
            self._closes[str(code)] = [float(value) for value in group["close"]]
            self._timestamps[str(code)] = [pd.Timestamp(value) for value in group["trade_date"]]

    def timestamps(self, code: str) -> list[pd.Timestamp]:
        return self._timestamps.get(code, [])

    def previous_close(self, code: str, day: date) -> float:
        """返回严格早于 ``day`` 的最近收盘价；没有则抛 SpecError。"""

        dates = self._dates.get(code)
        if not dates:
            raise SpecError(f"cash_dividend for {code} on {day} requires prices, but no prices found")
        position = bisect_left(dates, day) - 1
        if position < 0:
            raise SpecError(
                f"cash_dividend for {code} on {day} requires a close before ex_date, "
                "but prices start on or after ex_date"
            )
        return self._closes[code][position]


# ---------------------------------------------------------------- 因子计算


def _event_multiplier(event: _Event, price_book: _PriceBook | None) -> float:
    """单个事件的乘数；现金分红依赖价格时在这里解析。"""

    payload = event.payload
    if event.event == "stock_dividend":
        return 1.0 + float(payload["shares"])
    if event.event == "split":
        return float(payload["ratio"])
    if event.event == "rights_issue":
        # 需求指定的近似：忽略配股价，以 1 + 配股比例 作为乘数。
        return 1.0 + float(payload["ratio"])
    if event.event == "merge":
        return float(payload["ratio"])
    if event.event == "cash_dividend":
        dividend = float(payload["dividend"])
        if dividend == 0.0:
            return 1.0
        if price_book is not None:
            previous = price_book.previous_close(event.code, event.ex_date)
            if previous <= dividend:
                raise SpecError(
                    f"cash_dividend for {event.code} on {event.ex_date}: "
                    f"previous close {previous} must exceed dividend {dividend}"
                )
            return previous / (previous - dividend)
        previous = payload.get("prev_close")
        if previous is None:
            raise SpecError(
                f"cash_dividend for {event.code} on {event.ex_date} requires prices "
                "or payload.prev_close for the 1 + dividend/prev_close approximation"
            )
        previous = float(previous)
        if previous <= dividend:
            raise SpecError(
                f"cash_dividend for {event.code} on {event.ex_date}: "
                f"payload.prev_close {previous} must exceed dividend {dividend}"
            )
        return 1.0 + dividend / previous

    # 防御性兜底；正常情况下 _EventIn 已拦截。
    raise SpecError(f"unsupported event {event.event!r}")


def _build_curves(
    events: Sequence[_Event], price_book: _PriceBook | None
) -> dict[str, list[tuple[date, float]]]:
    """为每个代码生成 [(ex_date, 累计因子)]，同日事件合并为一条。"""

    grouped: dict[str, list[_Event]] = defaultdict(list)
    for event in events:
        grouped[event.code].append(event)

    curves: dict[str, list[tuple[date, float]]] = {}
    for code, code_events in grouped.items():
        points: list[tuple[date, float]] = []
        cumulative = 1.0
        for event in code_events:
            cumulative *= _event_multiplier(event, price_book)
            if not math.isfinite(cumulative) or cumulative <= 0:
                raise SpecError(
                    f"factor overflow/non-positive for {code} on {event.ex_date}: {cumulative!r}"
                )
            if points and points[-1][0] == event.ex_date:
                points[-1] = (event.ex_date, cumulative)
            else:
                points.append((event.ex_date, cumulative))
        curves[code] = points
    return curves


def _factor_at(points: Sequence[tuple[date, float]], day: date) -> float:
    for event_day, factor in reversed(points):
        if event_day <= day:
            return factor
    return 1.0


def _empty_factor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": pd.Series(dtype="object"),
            "trade_date": pd.Series(dtype="datetime64[ns]"),
            "factor": pd.Series(dtype="float64"),
        }
    )


def _finalise_frame(frame: pd.DataFrame, events: Sequence[_Event], uses_prices: bool) -> pd.DataFrame:
    frame = frame.loc[:, list(FACTOR_COLUMNS)].copy()
    frame = frame.sort_values(["code", "trade_date"], kind="mergesort").reset_index(drop=True)
    frame["factor"] = frame["factor"].astype(float)

    assumptions: list[str] = []
    if any(event.event == "cash_dividend" for event in events):
        if uses_prices:
            assumptions.append(
                "cash_dividend: exact factor = prev_close / (prev_close - dividend) using prices"
            )
        else:
            assumptions.append(
                "cash_dividend: approximation 1 + dividend / prev_close using payload.prev_close "
                "(prices not supplied)"
            )
    if any(event.event == "rights_issue" for event in events):
        assumptions.append(
            "rights_issue: approximation 1 + ratio, subscription price is ignored"
        )
    if uses_prices:
        assumptions.append("grid: price trade dates union event ex_dates")
    elif events:
        assumptions.append(
            "no prices: daily calendar grid over [start, end]; no exchange calendar is applied"
        )

    frame.attrs["factor_definition"] = FACTOR_DEFINITION
    frame.attrs["uses_prices"] = uses_prices
    frame.attrs["assumptions"] = tuple(assumptions)
    return frame


def _build_factor_frame(
    events: Sequence[_Event],
    prices: pd.DataFrame | None,
    *,
    start: Any = None,
    end: Any = None,
) -> pd.DataFrame:
    price_frame = _normalise_prices(prices) if prices is not None else None
    price_book = _PriceBook(price_frame) if price_frame is not None else None
    curves = _build_curves(events, price_book)

    if start is not None:
        start_day = _as_date(start, "start")
    else:
        start_day = None
    if end is not None:
        end_day = _as_date(end, "end")
    else:
        end_day = None
    if start_day is not None and end_day is not None and start_day > end_day:
        raise SpecError(f"start {start_day} must be <= end {end_day}")

    # 事件点 DataFrame：每个 (code, ex_date) 一条累计因子。
    point_rows: list[tuple[str, pd.Timestamp, float]] = []
    for code, points in curves.items():
        for event_day, factor in points:
            point_rows.append((code, pd.Timestamp(event_day), factor))
    points = (
        pd.DataFrame(point_rows, columns=["code", "trade_date", "factor"])
        if point_rows
        else _empty_factor_frame()
    )
    if not points.empty:
        points["trade_date"] = pd.to_datetime(points["trade_date"]).astype("datetime64[ns]")

    if price_frame is not None:
        grid = pd.concat(
            [
                price_frame.loc[:, ["code", "trade_date"]],
                points.loc[:, ["code", "trade_date"]],
            ],
            ignore_index=True,
        )
        if start_day is not None:
            grid = grid.loc[grid["trade_date"] >= pd.Timestamp(start_day)]
        if end_day is not None:
            grid = grid.loc[grid["trade_date"] <= pd.Timestamp(end_day)]
    else:
        if not curves:
            return _finalise_frame(_empty_factor_frame(), events, uses_prices=False)
        if start_day is None:
            start_day = min(event.ex_date for event in events)
        if end_day is None:
            end_day = max(event.ex_date for event in events)
        if start_day > end_day:
            return _finalise_frame(_empty_factor_frame(), events, uses_prices=False)
        dates = pd.date_range(start=start_day, end=end_day, freq="D")
        codes = sorted(curves)
        grid = pd.MultiIndex.from_product(
            [codes, dates], names=["code", "trade_date"]
        ).to_frame(index=False)

    if grid.empty:
        return _finalise_frame(_empty_factor_frame(), events, uses_prices=price_frame is not None)

    grid["trade_date"] = pd.to_datetime(grid["trade_date"]).astype("datetime64[ns]")
    grid = (
        grid.drop_duplicates(subset=["code", "trade_date"])
        .sort_values(["code", "trade_date"], kind="mergesort")
        .reset_index(drop=True)
    )

    if points.empty:
        grid["factor"] = 1.0
        result = grid
    else:
        points = points.sort_values(
            ["code", "trade_date"], kind="mergesort"
        ).reset_index(drop=True)
        result = pd.merge_asof(
            grid,
            points,
            on="trade_date",
            by="code",
            direction="backward",
            allow_exact_matches=True,
        )
        result["factor"] = result["factor"].fillna(1.0)

    return _finalise_frame(result, events, uses_prices=price_frame is not None)


# ---------------------------------------------------------------- 对外函数


def build_factor(
    events: Any,
    prices: pd.DataFrame | None = None,
    *,
    start: Any = None,
    end: Any = None,
) -> pd.DataFrame:
    """由事件表构建逐日累计复权因子。

    Parameters
    ----------
    events:
        规则文件路径、``{"events": [...]}`` 字典、事件列表，或
        :class:`CorporateActionsImpl` 实例。
    prices:
        可选参考价格表，需含 ``code`` / ``trade_date`` / ``close``。提供时
        现金分红使用精确公式，且输出网格取价格交易日与除权日的并集。
    start, end:
        可选日期边界。无 prices 时默认取事件表最小/最大 ex_date，按自然日
        生成网格；有 prices 时只过滤输出行，不影响事件累计。

    Returns
    -------
    pandas.DataFrame
        列 ``[code, trade_date, factor]``，按 ``(code, trade_date)`` 排序。
        ``factor`` 是后复权累计因子，首个事件前为 1。

    Notes
    -----
    - ``stock_dividend.shares`` 是**每股**送股数：10 送 10 应写 ``1.0``。
    - 无 prices 时，现金分红用 ``1 + dividend / prev_close`` 近似；若 payload
      没有 ``prev_close``（或其别名 ``price``）则抛 :class:`SpecError`。
    - 配股使用 ``1 + ratio`` 近似，忽略配股价，详见 ``DataFrame.attrs``。
    """

    if isinstance(events, CorporateActionsImpl):
        parsed = events._events
        if prices is None:
            prices = events._prices
    else:
        parsed = _load_events(events)
    return _build_factor_frame(parsed, prices, start=start, end=end)


def verify_against_source(
    built: pd.DataFrame,
    source: pd.DataFrame,
    tolerance: float = DEFAULT_TOLERANCE,
) -> float:
    """与源因子对账，返回最大相对误差；超过 ``tolerance`` 抛 SpecError。

    ``built`` / ``source`` 均需含 ``code`` / ``trade_date`` / ``factor``，
    按 (code, trade_date) 内连接比较。相对误差定义为
    ``|built - source| / |source|``；当 source 为 0 时退化为绝对误差。

    选择“超限即抛 :class:`SpecError`”的契约，测试固定该行为。
    """

    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise SpecError("tolerance must be a finite number >= 0")
    tolerance = float(tolerance)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise SpecError("tolerance must be a finite number >= 0")

    def normalise(frame: pd.DataFrame, label: str) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise SpecError(f"{label} must be a pandas DataFrame")
        missing = [name for name in FACTOR_COLUMNS if name not in frame.columns]
        if missing:
            raise SpecError(f"{label} missing required columns: {missing}")
        out = frame.loc[:, list(FACTOR_COLUMNS)].copy()
        out["code"] = out["code"].map(lambda value: _validate_code(value, f"{label}.code"))
        try:
            out["trade_date"] = (
                pd.to_datetime(out["trade_date"], errors="coerce")
                .dt.normalize()
                .astype("datetime64[ns]")
            )
        except (TypeError, ValueError) as exc:
            raise SpecError(f"{label}.trade_date cannot be parsed: {exc}") from exc
        if out["trade_date"].isna().any():
            raise SpecError(f"{label}.trade_date contains unparseable values")
        out["factor"] = pd.to_numeric(out["factor"], errors="coerce")
        if out["factor"].isna().any() or not out["factor"].map(math.isfinite).all():
            raise SpecError(f"{label}.factor must contain finite numbers")
        if bool(out.duplicated(subset=["code", "trade_date"]).any()):
            raise SpecError(f"{label} contains duplicate (code, trade_date) rows")
        return out

    built_frame = normalise(built, "built")
    source_frame = normalise(source, "source")
    merged = built_frame.merge(
        source_frame,
        on=["code", "trade_date"],
        how="inner",
        suffixes=("_built", "_source"),
    )
    if merged.empty:
        raise SpecError("built and source have no overlapping (code, trade_date) rows")

    difference = (merged["factor_built"] - merged["factor_source"]).abs()
    denominator = merged["factor_source"].abs()
    relative = (difference / denominator).where(denominator > 0, difference)
    max_error = float(relative.max())
    if not math.isfinite(max_error):
        raise SpecError("relative error is not finite; check input factors")
    if max_error > tolerance:
        raise SpecError(
            f"factor reconciliation failed: max relative error {max_error:.10g} "
            f"> tolerance {tolerance:.10g}"
        )
    return max_error


# ---------------------------------------------------------------- 实现类


class CorporateActionsImpl:
    """事件表驱动的 :class:`qrg.api.CorporateActions` 实现（模块 F3）。

    Parameters
    ----------
    events:
        规则文件路径、``{"events": [...]}`` 字典、事件列表。省略时按
        ``$QRG_ACTIONS_RULES`` → ``<repo>/rules/actions.cn.json`` →
        ``<cwd>/rules/actions.cn.json`` 查找默认规则。
    prices:
        可选参考价格表（code/trade_date/close），用于现金分红的精确比例。
    path:
        ``events`` 的别名，便于 ``CorporateActionsImpl(path=...)`` 调用。
    """

    def __init__(
        self,
        events: Any = None,
        *,
        prices: pd.DataFrame | None = None,
        path: str | Path | None = None,
    ) -> None:
        if path is not None:
            if events is not None:
                raise SpecError("pass either events or path, not both")
            events = path
        if events is None:
            events = _default_rules_path()

        self._source = events
        self._events = _load_events(events)
        self._prices = _normalise_prices(prices) if prices is not None else None
        self._curves: dict[str, list[tuple[date, float]]] | None = None

    @classmethod
    def from_file(cls, path: str | Path, *, prices: pd.DataFrame | None = None) -> "CorporateActionsImpl":
        return cls(path=path, prices=prices)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, prices: pd.DataFrame | None = None) -> "CorporateActionsImpl":
        return cls(events=payload, prices=prices)

    def _ensure_curves(self) -> dict[str, list[tuple[date, float]]]:
        if self._curves is None:
            price_book = _PriceBook(self._prices) if self._prices is not None else None
            self._curves = _build_curves(self._events, price_book)
        return self._curves

    def factor(self, code: str, day: date) -> float:
        """返回 ``day`` 当天的累计后复权因子；无事件/未知代码返回 1.0。"""

        normalised_code = _validate_code(code, "code")
        normalised_day = _as_date(day, "day")
        curves = self._ensure_curves()
        points = curves.get(normalised_code, [])
        return _factor_at(points, normalised_day)

    def actions(
        self,
        code: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """返回 ``[start, end]`` 闭区间内的事件，按 ``ex_date`` 升序。"""

        normalised_code = _validate_code(code, "code")
        start_day = _as_date(start, "start")
        end_day = _as_date(end, "end")
        if start_day > end_day:
            raise SpecError(f"start {start_day} must be <= end {end_day}")
        return [
            _event_mapping(event)
            for event in self._events
            if event.code == normalised_code
            and start_day <= event.ex_date <= end_day
        ]

    def build_factor(
        self,
        prices: pd.DataFrame | None = None,
        *,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        """使用本实例的事件重新构建因子表（可覆盖构造时的 prices）。"""

        chosen_prices = self._prices if prices is None else prices
        return _build_factor_frame(self._events, chosen_prices, start=start, end=end)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """事件表快照（按 code/ex_date/event/payload 稳定排序）。"""

        return tuple(_event_mapping(event) for event in self._events)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({event.code for event in self._events}))

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"CorporateActionsImpl(codes={len(self.codes)}, "
            f"events={len(self._events)}, prices={self._prices is not None})"
        )


__all__ = [
    "CorporateActionsImpl",
    "DEFAULT_TOLERANCE",
    "EVENT_FIELDS",
    "FACTOR_DEFINITION",
    "KNOWN_EVENTS",
    "build_factor",
    "verify_against_source",
]
