"""对外冻结的接口层。

规则：所有模块只依赖本文件中的类型与 Protocol；实现细节不得泄漏到调用方。
改动本文件需要升 spec 版本（见 spec/CHANGELOG.md）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum, IntEnum
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

import pandas as pd

# ---------------------------------------------------------------- 规范面板

#: 规范面板的最小列集合（长表）；其余列由各模块追加，不得改名。
PANEL_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

#: 网格化后允许出现的标志列。
FLAG_COLUMNS: tuple[str, ...] = (
    "suspended",
    "limit_up",
    "limit_down",
    "limit_locked",
    "is_st",
    "is_new",
    "tradable",
    "listed",
)

CODE_LENGTH = 6


class Severity(IntEnum):
    """违规分级：数值越大越严重，可直接比较。"""

    INFO = 0
    WARN = 1
    L2 = 2
    BLOCKED = 3


class SpecError(ValueError):
    """契约/配置不合法（结构、缺键、口径冲突）。"""


class GateBlocked(RuntimeError):
    """门禁拦截：产物不得发布。"""


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    severity: Severity
    message: str
    scope: Mapping[str, Any] = field(default_factory=dict)
    row_count: int = 0
    sample: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.name,
            "message": self.message,
            "scope": dict(self.scope),
            "row_count": self.row_count,
            "sample": list(self.sample),
        }


@dataclass
class AuditReport:
    """一次校验/审计的结果；可序列化为 data_quality_report.json。"""

    stage: str
    hits: list[RuleHit] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, hit: RuleHit) -> None:
        self.hits.append(hit)

    @property
    def max_severity(self) -> Severity:
        return max((h.severity for h in self.hits), default=Severity.INFO)

    @property
    def ok(self) -> bool:
        return self.max_severity < Severity.BLOCKED

    @property
    def blocked(self) -> bool:
        return self.max_severity >= Severity.BLOCKED

    def raise_if_blocked(self) -> None:
        if self.blocked:
            ids = sorted({h.rule_id for h in self.hits if h.severity >= Severity.BLOCKED})
            raise GateBlocked(f"blocked by: {', '.join(ids)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "max_severity": self.max_severity.name,
            "hits": [h.to_dict() for h in self.hits],
            "meta": self.meta,
        }


def content_hash(payload: Any) -> str:
    """稳定内容哈希：用于产物 hash 与 AI 输入/输出指纹。"""
    if isinstance(payload, pd.DataFrame):
        # 排序后序列化，保证行序无关
        text = payload.sort_index(axis=1).to_csv(index=False)
    else:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 契约对象


@dataclass(frozen=True)
class LimitRule:
    """涨跌停幅度规则；区间为 [effective_from, effective_to)。"""

    board: str
    pct: float
    effective_from: date
    effective_to: date | None
    source: str

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)


@dataclass(frozen=True)
class FeeRule:
    """费用科目规则；同一科目可有多条时序规则。"""

    item: str  # commission / stamp_duty / transfer_fee / exchange_fee / regulatory_fee
    rate: float
    side: str  # buy / sell / both
    effective_from: date
    effective_to: date | None
    min_amount: float = 0.0
    source: str = ""

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)


@dataclass(frozen=True)
class SecurityState:
    """某一标的在某一时点的生命周期状态。"""

    code: str
    name: str
    board: str  # main / star / chinext / bse
    listed_on: date | None
    delisted_on: date | None
    is_st: bool
    lot_size: int
    tick_size: float

    def exists_on(self, day: date) -> bool:
        if self.listed_on and day < self.listed_on:
            return False
        if self.delisted_on and day >= self.delisted_on:
            return False
        return True


@dataclass(frozen=True)
class Session:
    """单个交易日的会话时间（本地时区）。"""

    day: date
    call_auction_open: time
    continuous_open: time
    morning_close: time
    afternoon_open: time
    continuous_close: time
    call_auction_close: time

    @property
    def is_half_day(self) -> bool:
        return self.afternoon_open >= self.continuous_close


@dataclass(frozen=True)
class IndustryNode:
    code: str
    name: str
    level: int
    version: str  # e.g. SW2021
    effective_from: date
    effective_to: date | None = None


# ---------------------------------------------------------------- 提供者协议


@runtime_checkable
class CalendarProvider(Protocol):
    def sessions(self, start: date, end: date) -> list[Session]: ...

    def is_session(self, day: date) -> bool: ...

    def previous_session(self, day: date) -> date: ...


@runtime_checkable
class SecurityMaster(Protocol):
    def state(self, code: str, as_of: date) -> SecurityState | None: ...

    def listable(self, as_of: date) -> set[str]: ...


@runtime_checkable
class RegimeRules(Protocol):
    def limit_pct(self, state: SecurityState, day: date) -> float: ...

    def fees(self, day: date) -> Sequence[FeeRule]: ...


@runtime_checkable
class IndustryCalendar(Protocol):
    def classify(self, code: str, as_of: date) -> IndustryNode | None: ...


@runtime_checkable
class CorporateActions(Protocol):
    def factor(self, code: str, day: date) -> float: ...

    def actions(self, code: str, start: date, end: date) -> Iterable[Mapping[str, Any]]: ...


# ---------------------------------------------------------------- 契约加载


@dataclass(frozen=True)
class PanelSpec:
    """清洗契约的运行时视图（由 spec/*.json 加载）。"""

    spec_version: str
    timezone: str
    code_whitelist: tuple[str, ...]
    violation_rate_max: float
    violation_rate_fatal: float
    base_date: date
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PanelSpec":
        try:
            hv = payload["hard_validation"]
            return cls(
                spec_version=str(payload["spec_version"]),
                timezone=str(payload.get("timezone", "Asia/Shanghai")),
                code_whitelist=tuple(hv["code_whitelist"]),
                violation_rate_max=float(hv["violation_rate_max"]),
                violation_rate_fatal=float(hv["violation_rate_fatal"]),
                base_date=date.fromisoformat(payload["price_adjustment"]["qfq_base_date"]),
                raw=payload,
            )
        except (KeyError, TypeError, ValueError) as exc:  # noqa: BLE001
            raise SpecError(f"invalid spec: {exc}") from exc

    @classmethod
    def load(cls, path: str) -> "PanelSpec":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------- 执行与成本


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


#: 未成交/部分成交原因码；任何 fill 必须带其一，禁止静默丢单。
UNFILLED_REASONS: frozenset[str] = frozenset(
    {
        "filled",
        "partial",
        "suspended",
        "limit_locked",
        "t_plus_1",
        "participation_cap",
        "no_price",
        "no_liquidity",
    }
)


@dataclass(frozen=True)
class Order:
    """一个下单意图；ref_price 是到达价（arrival price）基准。"""

    trade_date: date
    code: str
    side: Side
    qty: float
    ref_price: float
    order_id: str = ""

    def with_qty(self, qty: float, order_id: str | None = None) -> "Order":
        return Order(self.trade_date, self.code, self.side, qty, self.ref_price, order_id or self.order_id)


@dataclass(frozen=True)
class Fill:
    order_id: str
    trade_date: date
    code: str
    side: Side
    qty: float
    price: float
    fee: float = 0.0
    reason: str = "filled"

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(frozen=True)
class SlippageBreakdown:
    """成交价相对 arrival price 的偏离分解；residual 为无法解释部分。"""

    impact: float = 0.0
    delay: float = 0.0
    spread: float = 0.0
    overnight: float = 0.0
    residual: float = 0.0

    @property
    def total(self) -> float:
        return self.impact + self.delay + self.spread + self.overnight + self.residual
