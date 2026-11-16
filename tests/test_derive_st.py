from __future__ import annotations

import pandas as pd

from qrg.master.derive import ST_PATTERN, events_by_code, st_events_from_names


def _frame(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["code", "trade_date", "name"])


def test_st_transitions_mid_period() -> None:
    frame = _frame(
        [
            ("1", "2024-01-02", "平安银行"),
            ("1", "2024-01-03", "平安银行"),
            ("1", "2024-01-04", "ST平安"),
            ("1", "2024-01-05", "ST平安"),
            ("1", "2024-01-08", "平安银行"),
        ]
    )
    events = st_events_from_names(frame)
    assert [(e["effective_date"], e["event"]) for e in events] == [
        ("2024-01-02", "st_off"),
        ("2024-01-04", "st_on"),
        ("2024-01-08", "st_off"),
    ]
    assert all(e["payload"]["proxy"] is True for e in events)


def test_st_padding_and_prefix_variants() -> None:
    frame = _frame(
        [
            ("000001", "2024-02-01", "S*ST示例"),
            ("000001", "2024-02-02", "*ST示例"),
            ("000001", "2024-02-03", "示例股份"),
        ]
    )
    events = st_events_from_names(frame)
    assert [e["event"] for e in events] == ["st_on", "st_off"]


def test_st_skips_missing_and_dedupes() -> None:
    frame = _frame(
        [
            ("000002", "2024-01-02", "ST甲公司"),
            ("000002", "2024-01-02", "ST甲公司"),
            ("000002", None, "ST甲公司"),  # type: ignore[arg-type]
        ]
    )
    events = st_events_from_names(frame)
    assert len(events) == 1
    assert events_by_code(events)["000002"][0]["event"] == "st_on"


def test_st_vintage_in_payload() -> None:
    frame = _frame([("000003", "2024-01-02", "乙公司")])
    events = st_events_from_names(frame, vintage_date="2026-10-30")
    assert events[0]["payload"]["vintage_date"] == "2026-10-30"
    assert ST_PATTERN.match("ST乙").group(0) == "ST"
