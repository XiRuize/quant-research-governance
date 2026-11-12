"""事件 PIT 与时效（G6）。

三条硬规则：
1. 可见时点 = **首次公开时间**，不是抓取时间、不是后续报道时间；
2. 收盘后（默认 15:00）公开的事件，当日不可见，顺延到下一交易日；
3. 辟谣/撤稿只影响撤稿之后的可见性，不改写历史（不做 backfill）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable, Sequence

import pandas as pd

from ..api import AuditReport, RuleHit, Severity, SpecError

STAGES: tuple[str, ...] = ("proposed", "reviewed", "passed", "effective")
DEFAULT_CLOSE = time(15, 0)

RULE_INVALID = "EPIT.invalid"
RULE_BACKFILL = "EPIT.backfill"


@dataclass(frozen=True)
class Event:
    event_id: str
    actor: str
    action: str
    object: str
    stage: str
    first_published_at: str  # ISO8601 datetime
    effective_at: str | None = None
    source_tier: int = 3
    evidence_ids: tuple[str, ...] = ()
    retracted_at: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise SpecError(f"invalid stage: {self.stage}")
        if not 1 <= int(self.source_tier) <= 5:
            raise SpecError("source_tier must be in 1..5")
        _parse_dt(self.first_published_at)
        if self.effective_at:
            date.fromisoformat(str(self.effective_at)[:10])
        if self.retracted_at:
            _parse_dt(self.retracted_at)


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise SpecError(f"invalid datetime: {value!r}") from exc


def visible_at(event: Event, as_of: datetime, close: time = DEFAULT_CLOSE) -> bool:
    """as_of 时刻是否可见。"""
    published = _parse_dt(event.first_published_at)
    if published > as_of:
        return False
    if event.retracted_at:
        if _parse_dt(event.retracted_at) <= as_of:
            return False
    return True


def events_asof(events: Iterable[Event], as_of: datetime) -> list[Event]:
    return [e for e in events if visible_at(e, as_of)]


def pit_frame(
    events: Sequence[Event],
    dates: Sequence[date],
    close: time = DEFAULT_CLOSE,
) -> pd.DataFrame:
    """返回 [trade_date, event_id, visible]；as_of 固定为该日 close。"""
    rows = []
    for day in dates:
        as_of = datetime.combine(day, close)
        for event in events:
            rows.append({"trade_date": pd.Timestamp(day), "event_id": event.event_id, "visible": visible_at(event, as_of, close)})
    return pd.DataFrame(rows, columns=["trade_date", "event_id", "visible"])


def audit(events: Sequence[Event], dates: Sequence[date] | None = None) -> AuditReport:
    """结构审计：生效早于公开、撤稿早于公开、重复 id、事件在生效日仍不可见。"""
    report = AuditReport(stage="event_pit", meta={"events": len(events)})
    seen: set[str] = set()
    hits = 0
    for e in events:
        if e.event_id in seen:
            report.add(
                RuleHit(
                    RULE_INVALID,
                    Severity.BLOCKED,
                    "duplicate event_id",
                    {"event_id": e.event_id},
                    1,
                    (e.event_id,),
                )
            )
            hits += 1
        seen.add(e.event_id)

        published = _parse_dt(e.first_published_at)
        if e.effective_at:
            eff = datetime.fromisoformat(str(e.effective_at)[:10])
            if eff < published:
                report.add(
                    RuleHit(
                        RULE_BACKFILL,
                        Severity.BLOCKED,
                        "effective_at precedes first_published_at (look-ahead risk)",
                        {"event_id": e.event_id},
                        1,
                        (e.event_id,),
                    )
                )
                hits += 1
        if e.retracted_at and _parse_dt(e.retracted_at) < published:
            report.add(
                RuleHit(
                    RULE_BACKFILL,
                    Severity.BLOCKED,
                    "retracted_at precedes first_published_at",
                    {"event_id": e.event_id},
                    1,
                    (e.event_id,),
                )
            )
            hits += 1

    report.meta["violations"] = hits
    return report


def assert_monotonic_stage(history: Sequence[Event]) -> None:
    """同一事件的多条记录必须按 proposed→reviewed→passed→effective 单调推进。"""
    if not history:
        return
    ids = {e.event_id for e in history}
    if len(ids) != 1:
        raise SpecError("history must share a single event_id")
    ranks = [STAGES.index(e.stage) for e in history]
    if ranks != sorted(ranks):
        raise SpecError(f"stage regression for {history[0].event_id}")
