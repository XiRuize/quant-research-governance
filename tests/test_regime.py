from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from qrg.api import SecurityState, SpecError
from qrg.calendar.regime import NO_LIMIT, RegimeCalendar, applicable, load_regime

ROOT = Path(__file__).resolve().parents[1]
DOC = load_regime(ROOT / "rules" / "regime.cn.json")


def state(
    board: str = "main",
    is_st: bool = False,
    listed_on: date | None = date(2010, 1, 4),
) -> SecurityState:
    return SecurityState(
        code="600001",
        name="demo",
        board=board,
        listed_on=listed_on,
        delisted_on=None,
        is_st=is_st,
        lot_size=100,
        tick_size=0.01,
    )


def test_main_board_10pct() -> None:
    cal = RegimeCalendar(DOC)
    assert cal.limit_pct(state(), date(2024, 1, 2)) == pytest.approx(0.10)


def test_chinext_switch() -> None:
    cal = RegimeCalendar(DOC)
    assert cal.limit_pct(state("chinext"), date(2020, 8, 21)) == pytest.approx(0.10)
    assert cal.limit_pct(state("chinext"), date(2020, 8, 24)) == pytest.approx(0.20)


def test_st_overrides_by_board() -> None:
    cal = RegimeCalendar(DOC)
    assert cal.limit_pct(state("main", is_st=True), date(2024, 1, 2)) == pytest.approx(0.05)
    assert cal.limit_pct(state("chinext", is_st=True), date(2024, 1, 2)) == pytest.approx(0.20)


def test_new_listing_has_no_limit_for_five_days() -> None:
    cal = RegimeCalendar(DOC)
    st = state("star", listed_on=date(2024, 6, 3))
    assert cal.limit_pct(st, date(2024, 6, 4)) == NO_LIMIT
    assert cal.limit_pct(st, date(2024, 6, 10)) == pytest.approx(0.20)


def test_new_listing_rule_not_applied_to_main_board() -> None:
    cal = RegimeCalendar(DOC)
    st = state("main", listed_on=date(2024, 6, 3))
    assert cal.limit_pct(st, date(2024, 6, 4)) == pytest.approx(0.10)


def test_stamp_duty_switch_and_side() -> None:
    cal = RegimeCalendar(DOC)
    before = {r.item: r for r in cal.fees(date(2023, 8, 1))}
    after = {r.item: r for r in cal.fees(date(2023, 8, 28))}
    assert before["stamp_duty"].rate == pytest.approx(0.001)
    assert after["stamp_duty"].rate == pytest.approx(0.0005)
    assert after["stamp_duty"].side == "sell"
    assert applicable([after["stamp_duty"]], date(2023, 8, 28), "buy") == []


def test_commission_min_amount() -> None:
    cal = RegimeCalendar(DOC)
    fees = {r.item: r for r in cal.fees(date(2024, 1, 2))}
    assert fees["commission"].min_amount == 5.0


def test_unknown_board_raises() -> None:
    cal = RegimeCalendar(DOC)
    with pytest.raises(SpecError):
        cal.limit_pct(state("nope"), date(2024, 1, 2))


def test_missing_source_rejected() -> None:
    payload = '{"limit":[{"board":"main","pct":0.1,"effective_from":"2000-01-01"}],"fees":[]}'
    p = ROOT / "tests" / ".tmp" / "regime_bad.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload, encoding="utf-8")
    with pytest.raises(SpecError):
        load_regime(p)
