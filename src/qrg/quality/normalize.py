"""F7 规范化：把外部行情长表收敛成可硬校验的规范面板。

设计约束
--------
* **纯离线、确定性**：不联网、不读时钟、不依赖随机数；只使用
  stdlib + pandas（以及 ``qrg.api`` 中的契约对象）。
* **先规范化、后硬校验**：单位、空值、百分号、代码前导零、名称编码、
  价格 tick 必须先统一，否则后续阈值/涨跌停/费用校验全部失真。
* **不猜单位**：``volume`` / ``amount`` 只有在调用方显式给出
  ``unit_hints`` 时才换算；未声明就原样保留并在 report 中不产生换算命中。
* **幂等**：换算后的规范单位写入 ``DataFrame.attrs["qrg_units"]``，
  同一张表用同一组 hints 再跑一次不会二次乘 100 / 1000。

取整规则（测试中固定）
----------------------
价格按 tick（默认 ``0.01``）使用 **Decimal ROUND_HALF_UP**（四舍五入，
远离零）取整；成交量取整同样使用 ROUND_HALF_UP。因此 ``10.005`` →
``10.01``，而不是 Python 内置 ``round`` 的银行家舍入 ``10.00``。
"""
from __future__ import annotations

import math
import unicodedata
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..api import (
    FLAG_COLUMNS,
    PANEL_COLUMNS,
    AuditReport,
    RuleHit,
    Severity,
    SpecError,
)

__all__ = ["normalize_panel", "pct_to_decimal", "NULL_TOKENS", "DEFAULT_TICK"]

#: 空值令牌（比较时先 strip + lower；``""`` 与纯空白都算空值）。
NULL_TOKENS: frozenset[str] = frozenset({"", "-", "null", "none", "nan"})

#: 价格最小变动单位，默认到分。
DEFAULT_TICK = 0.01

#: 价格列（含常见前收/复权价列）；按 tick 四舍五入到分。
_PRICE_COLUMNS: frozenset[str] = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "prev_close",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
    }
)
_NUMERIC_COLUMNS: frozenset[str] = _PRICE_COLUMNS | {"volume", "amount"}

#: 代码列：只做字符串化 + 前导零，不改变 6 位代码本身。
_CODE_COLUMNS: frozenset[str] = frozenset(
    {"code", "symbol", "stock_code", "sec_code", "证券代码"}
)

#: 名称列：去空白、全角转半角、去 BOM。
_NAME_COLUMNS: frozenset[str] = frozenset(
    {"name", "stock_name", "security_name", "证券名称", "名称", "简称"}
)

#: 显式单位提示的合法取值。注意：没有 hint 就绝不换算。
_ALLOWED_UNITS: dict[str, frozenset[str]] = {
    "volume": frozenset({"hand", "share"}),
    "amount": frozenset({"k_yuan", "yuan"}),
}

#: 换算到规范单位的目标与因子。
_UNIT_TARGET: dict[str, str] = {"volume": "share", "amount": "yuan"}
_UNIT_FACTOR: dict[tuple[str, str], float] = {
    ("volume", "hand"): 100.0,
    ("volume", "share"): 1.0,
    ("amount", "k_yuan"): 1000.0,
    ("amount", "yuan"): 1.0,
}

#: 名称里出现这些片段时，把该列当作百分比/比率列（仍需值本身可解析）。
_PCT_NAME_HINTS = (
    "pct",
    "percent",
    "rate",
    "ratio",
    "涨跌幅",
    "换手率",
    "比例",
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _is_missing(value: Any) -> bool:
    """标量是否缺失（pd.NA / None / NaN / NaT），不把字符串当缺失。"""
    if value is pd.NA or value is pd.NaT or value is None:
        return True
    if isinstance(value, str):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _is_null_token(value: Any) -> bool:
    """是否是**需要转换**的空值令牌。

    已经规范化的 ``pd.NA`` 不算，保证第二次运行的空值行数为 0。
    """
    if value is pd.NA or value is pd.NaT:
        return False
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in NULL_TOKENS
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _add_hit(
    report: AuditReport,
    rule_id: str,
    severity: Severity,
    message: str,
    *,
    scope: Mapping[str, Any] | None = None,
    row_count: int = 0,
    sample: Sequence[Any] = (),
) -> None:
    report.add(
        RuleHit(
            rule_id=rule_id,
            severity=severity,
            message=message,
            scope=dict(scope or {}),
            row_count=int(row_count),
            sample=tuple(sample),
        )
    )


def _float_series(values: Sequence[Any], index: pd.Index) -> pd.Series:
    """构造价格/金额列：有缺失用 ``Float64``，无缺失用 ``float64``。"""
    arr = pd.array(values, dtype="Float64")
    series = pd.Series(arr, index=index)
    if len(series) and not bool(series.isna().any()):
        return series.astype("float64")
    return series


def _int_series(values: Sequence[Any], index: pd.Index) -> pd.Series:
    """构造成交量列：有缺失用 ``Int64``，无缺失用 ``int64``。"""
    arr = pd.array(values, dtype="Int64")
    series = pd.Series(arr, index=index)
    if len(series) and not bool(series.isna().any()):
        return series.astype("int64")
    return series


def _parse_numeric(series: pd.Series) -> tuple[pd.Series, int, int]:
    """把列解析为 Float64。

    Returns
    -------
    parsed:
        解析后的 ``Float64`` Series。
    text_converted:
        由字符串（含千分位逗号）成功转成数值的行数。
    invalid:
        非空但无法解析 / 为 inf 的行数。
    """
    values = series.tolist()
    if pd.api.types.is_numeric_dtype(series):
        parsed = series.astype("Float64")
        arr = series.to_numpy(dtype="float64", na_value=np.nan)
        present = ~np.isnan(arr)
        nonfinite = present & ~np.isfinite(arr)
        if nonfinite.any():
            parsed = parsed.mask(nonfinite, pd.NA)
        return parsed, 0, int(nonfinite.sum())

    present = [not _is_missing(v) for v in values]
    cleaned: list[Any] = []
    for value, is_present in zip(values, present):
        if not is_present:
            cleaned.append(pd.NA)
        elif isinstance(value, str):
            text = unicodedata.normalize("NFKC", value)
            text = text.replace("\ufeff", "").strip().replace(",", "").replace(" ", "")
            cleaned.append(text if text else pd.NA)
        else:
            cleaned.append(value)

    try:
        parsed = pd.to_numeric(
            pd.Series(cleaned, index=series.index, dtype=object), errors="coerce"
        ).astype("Float64")
    except (TypeError, ValueError):  # pragma: no cover - 防御性兜底
        parsed = pd.Series(pd.array([pd.NA] * len(values), dtype="Float64"), index=series.index)

    finite = parsed.map(
        lambda x: True if pd.isna(x) else math.isfinite(float(x))
    ).astype(bool)
    present_series = pd.Series(present, index=series.index, dtype=bool)
    nonfinite = present_series & ~finite
    if bool(nonfinite.any()):
        parsed = parsed.mask(nonfinite, pd.NA)

    invalid_mask = (present_series & parsed.isna()) | nonfinite
    invalid = int(invalid_mask.sum())

    text_converted = 0
    for value, is_present, parsed_value in zip(values, present, parsed.tolist()):
        if is_present and isinstance(value, str) and not pd.isna(parsed_value):
            text_converted += 1

    return parsed, text_converted, invalid


def _round_half_up(value: float, quantum: Any) -> float:
    """Decimal ROUND_HALF_UP（四舍五入，远离零）。"""
    try:
        quant = Decimal(str(quantum))
        return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return math.nan


# --------------------------------------------------------------------------- #
# 百分比 / 名称 / 代码
# --------------------------------------------------------------------------- #


def _pct_scalar(value: Any) -> Any:
    if value is pd.NA or value is pd.NaT or value is None:
        return pd.NA
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value)
        text = text.replace("\ufeff", "").strip()
        if not text:
            return pd.NA
        if text.endswith("%"):
            number = float(text[:-1].strip().replace(",", ""))
            return number / 100.0
        return float(text.replace(",", ""))
    if isinstance(value, bool):
        raise ValueError(f"not a percentage value: {value!r}")
    try:
        if pd.isna(value):
            return pd.NA
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a percentage value: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"not a finite percentage value: {value!r}")
    return number


def pct_to_decimal(value: Any) -> Any:
    """百分比转小数；已是小数的值不重复换算。

    * ``"5%"`` → ``0.05``
    * ``"0.05"`` → ``0.05``（没有 ``%`` 就按小数处理，不猜业务口径）
    * ``5`` → ``5.0``（不猜它是 5% 还是 5 倍）
    * ``pd.NA`` / ``None`` → ``pd.NA``

    传入 ``pd.Series`` 时逐元素转换并返回 Float64 Series；无法解析的字符串
    抛出 ``ValueError``（面板路径会捕获并记为 ``F7.pct_invalid``）。
    """
    if isinstance(value, pd.Series):
        return _float_series([_pct_scalar(v) for v in value.tolist()], value.index).rename(
            value.name
        )
    return _pct_scalar(value)


def _normalize_code(value: Any) -> Any:
    if _is_missing(value):
        return pd.NA
    if isinstance(value, bool):
        text = str(int(value))
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return pd.NA
        text = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).replace("\ufeff", "").strip()
        # CSV 有时把 1 读成 "1.0"：仅在确实是整数形态时按整数处理。
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
    else:
        text = str(value).strip()
    if not text:
        return pd.NA
    if len(text) < 6 and text.isdigit():
        text = text.zfill(6)
    return text


def _normalize_name(value: Any) -> Any:
    if _is_missing(value):
        return pd.NA
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\ufeff", "")
    # 去空白：包含内部空白（证券名称里的空格通常无意义）。
    text = "".join(text.split())
    return text


# --------------------------------------------------------------------------- #
# 各阶段
# --------------------------------------------------------------------------- #


def _normalize_columns(df: pd.DataFrame, report: AuditReport) -> tuple[pd.DataFrame, list[str]]:
    original = [str(c) for c in df.columns]
    normalized = [str(c).strip().lower() for c in df.columns]

    duplicates = sorted(name for name, count in Counter(normalized).items() if count > 1)
    if duplicates:
        raise SpecError(
            "duplicate columns after lowercase/trim: " + ", ".join(repr(n) for n in duplicates)
        )

    mapping = {
        old: new
        for old, new in zip(df.columns, normalized)
        if not isinstance(old, str) or old != new
    }
    if mapping:
        df = df.rename(columns=mapping)
        _add_hit(
            report,
            "F7.columns_lowercased",
            Severity.INFO,
            "column names lowercased and stripped",
            scope={
                "renamed": {str(k): str(v) for k, v in mapping.items()},
                "columns": normalized,
            },
        )

    unknown = [
        col for col in normalized if col not in PANEL_COLUMNS and col not in FLAG_COLUMNS
    ]
    if unknown:
        _add_hit(
            report,
            "F7.unknown_columns",
            Severity.INFO,
            "unrecognized columns preserved",
            scope={"columns": unknown},
        )
    return df, original


def _replace_null_tokens(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    for column in df.columns:
        s = df[column]
        # 数值/时间/布尔列不可能含字符串空值令牌，直接跳过（真实行情表大多走这里）
        if (
            pd.api.types.is_numeric_dtype(s)
            or pd.api.types.is_datetime64_any_dtype(s)
            or pd.api.types.is_bool_dtype(s)
        ):
            continue
        as_str = s.astype("string")
        mask = as_str.str.strip().str.lower().isin(NULL_TOKENS).fillna(False)
        # 字面量 None 也算空值令牌；pd.NA/NaN 已经是缺失，不重复计数（保证幂等）
        na_mask = s.isna()
        if bool(na_mask.any()):
            # pandas 会把 None 规整成 NaN；pd.NA/NaT 视为已规范化，避免二次计数
            raw_missing = na_mask & s.map(lambda v: not (v is pd.NA or v is pd.NaT))
            mask = mask | raw_missing.fillna(False).astype(bool)
        count = int(mask.sum())
        if not count:
            continue
        df[column] = s.astype(object).mask(mask, pd.NA)
        _add_hit(
            report,
            "F7.null_tokens",
            Severity.INFO,
            f"null tokens normalized in column {column!r}",
            scope={"column": column},
            row_count=count,
        )
    return df


def _normalize_codes(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    for column in [c for c in df.columns if c in _CODE_COLUMNS]:
        values = df[column].tolist()
        normalized = [_normalize_code(v) for v in values]
        changed = 0
        changed_examples: list[Any] = []
        invalid_values: list[Any] = []
        for old, new in zip(values, normalized):
            if new is pd.NA:
                continue
            if not (isinstance(old, str) and old == new):
                changed += 1
                changed_examples.append(new)
            if not (new.isdigit() and len(new) == 6):
                invalid_values.append(new)
        df[column] = pd.Series(normalized, index=df.index, dtype=object)
        if changed:
            _add_hit(
                report,
                "F7.code_pad",
                Severity.INFO,
                f"code column {column!r} stringified/padded to 6 digits",
                scope={"column": column, "target_length": 6},
                row_count=changed,
                sample=tuple(changed_examples[:3]),
            )
        if invalid_values:
            _add_hit(
                report,
                "F7.code_invalid",
                Severity.WARN,
                f"code column {column!r} contains non-6-digit codes (left unchanged)",
                scope={"column": column},
                row_count=len(invalid_values),
                sample=tuple(invalid_values[:3]),
            )
    return df


def _normalize_names(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    for column in [c for c in df.columns if c in _NAME_COLUMNS]:
        values = df[column].tolist()
        normalized = [_normalize_name(v) for v in values]
        changed = sum(
            1
            for old, new in zip(values, normalized)
            if new is not pd.NA and not (isinstance(old, str) and old == new)
        )
        df[column] = pd.Series(normalized, index=df.index, dtype=object)
        if changed:
            _add_hit(
                report,
                "F7.name_normalized",
                Severity.INFO,
                f"security name column {column!r} normalized (whitespace/NFKC/BOM)",
                scope={"column": column},
                row_count=changed,
            )
    return df


def _coerce_numeric_columns(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    for column in [c for c in df.columns if c in _NUMERIC_COLUMNS]:
        parsed, text_converted, invalid = _parse_numeric(df[column])
        # 无缺失时收敛为原生 float64，有缺失时保留可空 Float64；
        # volume 随后会单独收敛为 int（见 _round_volume）。
        df[column] = _float_series(parsed.tolist(), df.index)
        if text_converted:
            _add_hit(
                report,
                "F7.numeric_coerce",
                Severity.INFO,
                f"numeric strings parsed in column {column!r}",
                scope={"column": column},
                row_count=text_converted,
            )
        if invalid:
            _add_hit(
                report,
                "F7.numeric_invalid",
                Severity.WARN,
                f"non-numeric values coerced to NA in column {column!r}",
                scope={"column": column},
                row_count=invalid,
            )
    return df


def _looks_like_pct_column(column: str, series: pd.Series) -> bool:
    lowered = column.lower()
    if any(token in lowered for token in _PCT_NAME_HINTS):
        return True
    return any(
        isinstance(v, str) and "%" in unicodedata.normalize("NFKC", v)
        for v in series.tolist()
    )


def _convert_pct_columns(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    skip = _CODE_COLUMNS | _NAME_COLUMNS
    for column in [c for c in df.columns if c not in skip]:
        series = df[column]
        if not _looks_like_pct_column(column, series):
            continue
        values = series.tolist()
        converted: list[Any] = []
        text_converted = 0
        invalid: list[Any] = []
        for value in values:
            if _is_missing(value):
                converted.append(pd.NA)
                continue
            if isinstance(value, str):
                try:
                    converted.append(_pct_scalar(value))
                    text_converted += 1
                except ValueError:
                    converted.append(pd.NA)
                    invalid.append(value)
            elif isinstance(value, bool):
                converted.append(pd.NA)
                invalid.append(value)
            else:
                try:
                    converted.append(_pct_scalar(value))
                except ValueError:
                    converted.append(pd.NA)
                    invalid.append(value)
        df[column] = _float_series(converted, df.index)
        if text_converted:
            _add_hit(
                report,
                "F7.pct_decimal",
                Severity.INFO,
                f"percent strings converted to decimals in column {column!r}",
                scope={"column": column},
                row_count=text_converted,
            )
        if invalid:
            _add_hit(
                report,
                "F7.pct_invalid",
                Severity.WARN,
                f"invalid percent values coerced to NA in column {column!r}",
                scope={"column": column},
                row_count=len(invalid),
                sample=tuple(str(v) for v in invalid[:3]),
            )
    return df


def _normalize_unit_hints(unit_hints: Mapping[str, Any] | None) -> dict[str, str]:
    if unit_hints is None:
        return {}
    if not isinstance(unit_hints, Mapping):
        raise SpecError("unit_hints must be a mapping like {'volume': 'hand'}")
    hints: dict[str, str] = {}
    for raw_key, raw_value in unit_hints.items():
        key = str(raw_key).strip().lower()
        if key not in _ALLOWED_UNITS:
            raise SpecError(
                f"unknown unit hint column: {raw_key!r}; expected one of {sorted(_ALLOWED_UNITS)}"
            )
        if not isinstance(raw_value, str):
            raise SpecError(f"unit hint for {key!r} must be a string")
        value = raw_value.strip().lower()
        if value not in _ALLOWED_UNITS[key]:
            raise SpecError(
                f"invalid unit hint {value!r} for {key!r}; "
                f"expected one of {sorted(_ALLOWED_UNITS[key])}"
            )
        hints[key] = value
    return hints


def _canonical_units_from_attrs(df: pd.DataFrame) -> dict[str, str]:
    raw = df.attrs.get("qrg_units")
    if not isinstance(raw, Mapping):
        return {}
    units: dict[str, str] = {}
    for column in ("volume", "amount"):
        value = raw.get(column)
        if isinstance(value, str) and value.strip().lower() in _ALLOWED_UNITS[column]:
            units[column] = value.strip().lower()
    return units


def _apply_units(
    df: pd.DataFrame,
    hints: Mapping[str, str],
    known_units: dict[str, str],
    report: AuditReport,
) -> tuple[pd.DataFrame, dict[str, str], list[dict[str, Any]]]:
    units = dict(known_units)
    skips: list[dict[str, Any]] = []
    for column in ("volume", "amount"):
        hint = hints.get(column)
        if hint is None or column not in df.columns:
            continue
        target = _UNIT_TARGET[column]
        if units.get(column) == target:
            # 已经处于规范单位：不再乘一次，保证幂等。
            skips.append({"column": column, "hint": hint, "reason": f"already {target}"})
            continue
        factor = _UNIT_FACTOR[(column, hint)]
        series = df[column]
        converted_values: list[Any] = []
        for value in series.tolist():
            if _is_missing(value):
                converted_values.append(pd.NA)
            else:
                converted_values.append(float(value) * factor)
        row_count = int(series.notna().sum())
        df[column] = _float_series(converted_values, df.index)
        units[column] = target
        if row_count:
            _add_hit(
                report,
                f"F7.unit_{column}",
                Severity.INFO,
                f"{column} converted from {hint} to {target}",
                scope={"column": column, "from": hint, "to": target, "factor": factor},
                row_count=row_count,
            )
    return df, units, skips


def _round_prices(
    df: pd.DataFrame, tick: float, report: AuditReport
) -> pd.DataFrame:
    quantum = 1.0 / float(tick)
    for column in [c for c in df.columns if c in _PRICE_COLUMNS]:
        values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64")
        missing = np.isnan(values)
        scaled = values * quantum
        floor = np.floor(scaled)
        frac = scaled - floor
        # 绝大多数值直接向量化 half-up；只有恰好落在 .5 附近的才走 Decimal（二进制浮点的坑）
        near_half = np.abs(frac - 0.5) < 1e-9
        rounded = (floor + (frac >= 0.5)) / quantum
        for i in np.flatnonzero(near_half & ~missing):
            rounded[i] = _round_half_up(float(values[i]), tick)
        rounded[missing] = np.nan
        changed = int(np.sum(~missing & (rounded != values)))
        df[column] = _float_series(rounded.tolist(), df.index)
        if changed:
            _add_hit(
                report,
                "F7.price_round",
                Severity.INFO,
                f"prices in column {column!r} rounded to tick {tick:g}",
                scope={"column": column, "tick": tick, "rounding": "ROUND_HALF_UP"},
                row_count=changed,
            )
    return df


def _round_volume(df: pd.DataFrame, report: AuditReport) -> pd.DataFrame:
    if "volume" not in df.columns:
        return df
    values = pd.to_numeric(df["volume"], errors="coerce").to_numpy(dtype="float64")
    missing = np.isnan(values)
    rounded = np.rint(values)
    changed = int((~missing & (values != rounded)).sum())
    result = rounded.copy()
    result[missing] = np.nan
    df["volume"] = _int_series([int(v) if not math.isnan(v) else pd.NA for v in result], df.index)
    if changed:
        _add_hit(
            report,
            "F7.volume_round",
            Severity.INFO,
            "volume rounded to integer shares",
            scope={"column": "volume", "rounding": "ROUND_HALF_UP"},
            row_count=changed,
        )
    return df


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def normalize_panel(
    df: pd.DataFrame,
    unit_hints: Mapping[str, Any] | None = None,
    *,
    tick: float = DEFAULT_TICK,
) -> tuple[pd.DataFrame, AuditReport]:
    """规范化行情面板，返回 ``(规范化后的 DataFrame, AuditReport)``。

    参数
    ----
    df:
        输入长表；不要求包含全部 ``PANEL_COLUMNS``，缺失列按原样跳过。
        函数不原地修改输入（返回深拷贝）。
    unit_hints:
        显式单位声明，例如 ``{"volume": "hand", "amount": "k_yuan"}``。
        只接受 ``volume: hand|share`` 与 ``amount: k_yuan|yuan``；
        缺省或未声明的列**不换算**。非法键/值抛 :class:`SpecError`。
    tick:
        价格最小变动单位，默认 ``0.01``（分）。

    幂等
    ----
    输入输出都带 ``df.attrs["qrg_units"]``。同一张已规范化表再传同一组
    hints 时，单位换算命中行数为 0，数值不会被二次乘 100/1000。

    返回
    ----
    report.stage == ``"normalize"``；每次实际发生的转换按列记录
    ``RuleHit``（``F7.null_tokens``、``F7.unit_volume`` 等），
    schema 级记录（未知列等）的 ``row_count`` 为 0。
    """
    if not isinstance(df, pd.DataFrame):
        raise SpecError("normalize_panel expects a pandas DataFrame")
    if isinstance(tick, bool) or not isinstance(tick, (int, float)):
        raise SpecError("tick must be a positive number")
    tick = float(tick)
    if not math.isfinite(tick) or tick <= 0:
        raise SpecError("tick must be a positive finite number")

    hints = _normalize_unit_hints(unit_hints)
    report = AuditReport(stage="normalize")
    known_units = _canonical_units_from_attrs(df)

    out = df.copy(deep=True)
    out, original_columns = _normalize_columns(out, report)
    out = _replace_null_tokens(out, report)
    out = _normalize_codes(out, report)
    out = _normalize_names(out, report)
    out = _coerce_numeric_columns(out, report)
    out = _convert_pct_columns(out, report)
    out, units, unit_skips = _apply_units(out, hints, known_units, report)
    out = _round_prices(out, tick, report)
    out = _round_volume(out, report)

    # 只把规范单位写入 attrs，供下一次调用做幂等判断。
    attrs = dict(out.attrs)
    canonical = _canonical_units_from_attrs(out)
    canonical.update({k: v for k, v in units.items() if k in _UNIT_TARGET and v == _UNIT_TARGET[k]})
    if canonical:
        attrs["qrg_units"] = canonical
    attrs["qrg_tick"] = tick
    out.attrs = attrs

    report.meta.update(
        {
            "row_count": int(len(out)),
            "columns_in": original_columns,
            "columns_out": [str(c) for c in out.columns],
            "unit_hints": dict(hints),
            "units": dict(canonical),
            "unit_skips": unit_skips,
            "tick": tick,
            "rounding": "ROUND_HALF_UP",
        }
    )
    return out, report
