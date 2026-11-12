"""制度日历（B6）：涨跌停幅度、新股无限制、费用科目。

与交易日历分离：交易日历回答“今天开不开市”，制度日历回答“今天规则是什么”。
所有规则带 [effective_from, effective_to) 与 source；缺 source 的规则在载入时拒绝。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..api import FeeRule, LimitRule, SecurityState, SpecError

DEFAULT_RULES = Path(__file__).resolve().parents[3] / "rules" / "regime.cn.json"

#: 表示“当日无涨跌幅限制”。
NO_LIMIT = float("inf")


class LimitRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    board: str
    pct: float = Field(gt=0, le=1)
    effective_from: date
    effective_to: date | None = None
    source: str = ""

    @model_validator(mode="after")
    def _check(self) -> "LimitRow":
        if not self.source:
            raise ValueError("limit rule requires source")
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be > effective_from")
        return self

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)

    def to_rule(self) -> LimitRule:
        return LimitRule(self.board, self.pct, self.effective_from, self.effective_to, self.source)


class FeeRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    item: str
    rate: float = Field(ge=0)
    side: str = "both"
    effective_from: date
    effective_to: date | None = None
    min_amount: float = 0.0
    source: str = ""

    @model_validator(mode="after")
    def _check(self) -> "FeeRow":
        if self.side not in {"buy", "sell", "both"}:
            raise ValueError(f"invalid side: {self.side}")
        if not self.source:
            raise ValueError("fee rule requires source")
        return self

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)

    def to_rule(self) -> FeeRule:
        return FeeRule(
            self.item, self.rate, self.side, self.effective_from, self.effective_to, self.min_amount, self.source
        )


class NewListingRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    no_limit_days: int = Field(ge=0)
    boards: list[str]
    effective_from: date
    note: str = ""


class StRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    pct: float = Field(gt=0, le=1)
    source: str = ""


class RegimeDoc(BaseModel):
    model_config = ConfigDict(extra="allow")

    timezone: str = "Asia/Shanghai"
    limit: list[LimitRow] = Field(min_length=1)
    st: dict[str, StRow] = Field(default_factory=dict)
    new_listing: NewListingRow | None = None
    fees: list[FeeRow] = Field(default_factory=list)


def load_regime(path: str | Path = DEFAULT_RULES) -> RegimeDoc:
    p = Path(path)
    if not p.exists():
        raise SpecError(f"regime rules not found: {p}")
    try:
        payload: Any = json.loads(p.read_text(encoding="utf-8"))
        return RegimeDoc.model_validate(payload)
    except ValidationError as exc:
        raise SpecError(f"invalid regime rules: {exc.errors()}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"regime rules not json: {exc}") from exc


class RegimeCalendar:
    """RegimeRules 协议的默认实现。"""

    def __init__(self, doc: RegimeDoc | None = None, path: str | Path | None = None) -> None:
        self.doc = doc or load_regime(path or DEFAULT_RULES)
        for row in self.doc.limit:
            if row.board not in {r.board for r in self.doc.limit}:
                raise SpecError("unreachable")
        self._limits = sorted(self.doc.limit, key=lambda r: (r.board, r.effective_from))

    # ------------------------------------------------------------ limits

    def _limit_row(self, board: str, day: date) -> LimitRow:
        candidates = [r for r in self._limits if r.board == board and r.covers(day)]
        if not candidates:
            raise SpecError(f"no limit rule for board={board} on {day}")
        return max(candidates, key=lambda r: r.effective_from)

    def is_no_limit(self, state: SecurityState, day: date) -> bool:
        rule = self.doc.new_listing
        if not rule or not state.listed_on:
            return False
        if state.board not in rule.boards:
            return False
        if not (day >= rule.effective_from and day >= state.listed_on):
            return False
        # 近似：按自然日计 5 天；精确交易日应由 grid 用交易日历重算
        return (day - state.listed_on).days < rule.no_limit_days

    def limit_pct(self, state: SecurityState, day: date) -> float:
        if self.is_no_limit(state, day):
            return NO_LIMIT
        if state.is_st:
            st = self.doc.st.get(state.board)
            if st:
                return st.pct
        return self._limit_row(state.board, day).pct

    # ------------------------------------------------------------ fees

    def fees(self, day: date) -> Sequence[FeeRule]:
        return [r.to_rule() for r in self.doc.fees if r.covers(day)]

    def fee_items(self, day: date) -> dict[str, list[FeeRule]]:
        out: dict[str, list[FeeRule]] = {}
        for rule in self.fees(day):
            out.setdefault(rule.item, []).append(rule)
        return out


def applicable(items: Iterable[FeeRule], day: date, side: str) -> list[FeeRule]:
    """筛选某日某方向适用的费率规则。"""
    return [r for r in items if r.covers(day) and r.side in (side, "both")]
