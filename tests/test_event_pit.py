from __future__ import annotations

from datetime import date, datetime, time

import pytest

from qrg.api import GateBlocked, Severity, SpecError
from qrg.text.event_pit import Event, audit, pit_frame, visible_at

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def event(**kwargs) -> Event:
    base = dict(
        event_id="e1",
        actor="congress",
        action="letter",
        object="DUV export",
        stage="proposed",
        first_published_at="2024-01-02T10:00:00",
        source_tier=1,
    )
    base.update(kwargs)
    return Event(**base)


def test_intraday_event_visible_same_day() -> None:
    assert visible_at(event(), datetime.combine(D1, time(15, 0)))


def test_after_close_event_not_visible_same_day() -> None:
    e = event(first_published_at="2024-01-02T16:00:00")
    assert not visible_at(e, datetime.combine(D1, time(15, 0)))
    assert visible_at(e, datetime.combine(D2, time(15, 0)))


def test_retraction_only_affects_after() -> None:
    e = event(retracted_at="2024-01-03T09:00:00")
    assert visible_at(e, datetime.combine(D1, time(15, 0)))
    assert not visible_at(e, datetime.combine(D2, time(15, 0)))


def test_pit_frame_complete_grid() -> None:
    frame = pit_frame([event(), event(event_id="e2", first_published_at="2024-01-02T16:00:00")], [D1, D2])
    assert len(frame) == 4
    assert frame.loc[(frame["event_id"] == "e1"), "visible"].all()
    e2 = frame.loc[frame["event_id"] == "e2", "visible"].tolist()
    assert e2 == [False, True]


def test_effect_before_publish_is_blocked() -> None:
    report = audit([event(effective_at="2024-01-01")])
    assert report.max_severity == Severity.BLOCKED
    with pytest.raises(GateBlocked):
        report.raise_if_blocked()


def test_duplicate_event_id_blocked() -> None:
    report = audit([event(), event()])
    assert report.max_severity == Severity.BLOCKED


def test_invalid_stage_and_tier() -> None:
    with pytest.raises(SpecError):
        event(stage="rumor")
    with pytest.raises(SpecError):
        event(source_tier=9)


def test_stage_monotonic() -> None:
    from qrg.text.event_pit import assert_monotonic_stage

    hist = [event(stage="proposed"), event(stage="passed", first_published_at="2024-01-03T09:00:00")]
    assert_monotonic_stage(hist)
    with pytest.raises(SpecError):
        assert_monotonic_stage(list(reversed(hist)))
