"""F1 证券主数据的契约测试。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from qrg.api import SecurityMaster, SecurityState, SpecError
from qrg.master import SecurityMasterImpl


# ---------------------------------------------------------------- 测试工具


def _rules(events: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """构造内存规则表，避免依赖系统临时目录（沙箱可能拒绝）。"""

    payload: dict[str, Any] = {
        "spec_version": "test",
        "defaults": {"lot_size": 100, "tick_size": 0.01},
        "board_overrides": {"star": {"lot_size": 200}},
        "events": events,
    }
    payload.update(extra)
    return payload


def _master(events: list[dict[str, Any]], **extra: Any) -> SecurityMasterImpl:
    return SecurityMasterImpl(_rules(events, **extra))


def _listed(code: str, day: str, name: str, board: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name}
    if board is not None:
        payload["board"] = board
    return {"code": code, "event": "listed", "effective_date": day, "payload": payload}


def _event(code: str, event: str, day: str, **payload: Any) -> dict[str, Any]:
    return {"code": code, "event": event, "effective_date": day, "payload": payload}


# ---------------------------------------------------------------- 代码复用


def test_code_reuse_returns_none_in_gap_and_distinct_listing_ids() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "旧主体"),
            _event("600001", "delisted", "2015-06-30"),
            _listed("600001", "2020-07-01", "新主体"),
        ]
    )
    assert isinstance(master, SecurityMaster)  # 满足 qrg.api 运行时协议

    first = master.state("600001", date(2010, 1, 4))
    assert isinstance(first, SecurityState)
    assert first.listed_on == date(2010, 1, 4)
    assert first.delisted_on == date(2015, 6, 30)
    assert first.name == "旧主体"

    # 退市当天即不可见，空档内 state 必须为 None。
    assert master.state("600001", date(2015, 6, 29)) is not None
    assert master.state("600001", date(2015, 6, 30)) is None
    assert master.state("600001", date(2017, 1, 1)) is None
    assert master.state("600001", date(2019, 12, 31)) is None

    second = master.state("600001", date(2020, 7, 1))
    assert isinstance(second, SecurityState)
    assert second.name == "新主体"
    assert second.listed_on == date(2020, 7, 1)
    assert second.delisted_on is None

    # 两段生命周期必须用不同 listing_id 区分，且空档没有 listing_id。
    first_id = master.listing_id("600001", date(2010, 1, 4))
    second_id = master.listing_id("600001", date(2020, 7, 1))
    assert first_id is not None and second_id is not None
    assert first_id != second_id
    assert master.listing_id("600001", date(2017, 1, 1)) is None

    assert master.detect_reuse() == ["600001"]


def test_detect_reuse_empty_when_no_reuse() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "甲"),
            _listed("300001", "2012-03-01", "乙"),
        ]
    )
    assert master.detect_reuse() == []


# ---------------------------------------------------------------- ST 事件


def test_st_events_take_effect_on_effective_date() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "ST 标的"),
            _event("600001", "st_on", "2013-01-04"),
            _event("600001", "st_off", "2015-03-02"),
            _event("600001", "delisted", "2016-12-30"),
        ]
    )

    # 不得用当前快照回填历史：st_on 之前一律 False。
    assert master.state("600001", date(2012, 12, 31)).is_st is False
    # st_on 生效日当天即为 True。
    assert master.state("600001", date(2013, 1, 4)).is_st is True
    assert master.state("600001", date(2014, 6, 30)).is_st is True
    # st_off 生效日当天即恢复 False。
    assert master.state("600001", date(2015, 3, 1)).is_st is True
    assert master.state("600001", date(2015, 3, 2)).is_st is False
    assert master.state("600001", date(2016, 1, 1)).is_st is False


# ---------------------------------------------------------------- listable


def test_listable_contains_only_codes_alive_on_that_day() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "甲"),
            _event("600001", "delisted", "2015-06-30"),
            _listed("300001", "2012-03-01", "乙"),
            _listed("688001", "2019-07-22", "丙"),
        ]
    )

    assert master.listable(date(2009, 12, 31)) == set()
    assert master.listable(date(2010, 1, 4)) == {"600001"}
    assert master.listable(date(2013, 1, 2)) == {"600001", "300001"}
    assert master.listable(date(2015, 6, 30)) == {"300001"}  # 退市当天已不可交易
    assert master.listable(date(2020, 1, 2)) == {"300001", "688001"}


# ---------------------------------------------------------------- 非法事件


def test_missing_effective_date_raises_spec_error() -> None:
    bad_event = {
        "code": "600001",
        "event": "listed",
        "payload": {"name": "缺日期"},
    }
    with pytest.raises(SpecError):
        SecurityMasterImpl(_rules([bad_event]))


def test_unknown_event_raises_spec_error() -> None:
    bad_event = _event("600001", "suspended", "2020-01-02")
    with pytest.raises(SpecError):
        SecurityMasterImpl(_rules([bad_event]))


def test_lifecycle_ordering_violation_raises_spec_error() -> None:
    # 未退市就再次上市（且没有 listing_id 区分）属于事件表冲突。
    events = [
        _listed("600001", "2010-01-04", "甲"),
        _listed("600001", "2012-01-04", "乙"),
    ]
    with pytest.raises(SpecError):
        SecurityMasterImpl(_rules(events))


# ---------------------------------------------------------------- 板别与交易参数


def test_board_inference_and_lot_tick_overrides() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "主板"),
            _listed("688001", "2019-07-22", "科创板"),
            _listed("300001", "2012-03-01", "创业板"),
            _listed("830001", "2020-07-27", "北交所 8"),
            _listed("430001", "2020-07-27", "北交所 4"),
            _listed("920001", "2023-01-03", "北交所 920"),
        ],
        code_overrides={"600001": {"tick_size": 0.005}},
    )

    main = master.state("600001", date(2020, 1, 2))
    assert main.board == "main"
    assert main.lot_size == 100
    assert main.tick_size == pytest.approx(0.005)  # code_overrides 最高优先级

    star = master.state("688001", date(2020, 1, 2))
    assert star.board == "star"
    assert star.lot_size == 200  # board_overrides 覆盖默认值
    assert star.tick_size == pytest.approx(0.01)

    chinext = master.state("300001", date(2020, 1, 2))
    assert chinext.board == "chinext"
    assert chinext.lot_size == 100

    for code in ("830001", "430001", "920001"):
        state = master.state(code, date(2023, 6, 1))
        assert state.board == "bse"
        assert state.lot_size == 100


def test_listed_payload_overrides_board_lot_size() -> None:
    master = _master(
        [
            {
                "code": "688001",
                "event": "listed",
                "effective_date": "2019-07-22",
                "payload": {"name": "科创板", "lot_size": 300, "tick_size": 0.02},
            }
        ]
    )
    state = master.state("688001", date(2020, 1, 2))
    assert state.lot_size == 300
    assert state.tick_size == pytest.approx(0.02)


# ---------------------------------------------------------------- 历史不被覆盖


def test_renamed_and_board_change_are_not_retroactive() -> None:
    master = _master(
        [
            _listed("600001", "2010-01-04", "旧名"),
            _event("600001", "renamed", "2012-06-01", name="新名"),
            _event("600001", "board_change", "2014-01-02", board="star"),
        ]
    )

    before = master.state("600001", date(2011, 1, 1))
    assert before.name == "旧名"
    assert before.board == "main"

    renamed = master.state("600001", date(2013, 1, 1))
    assert renamed.name == "新名"
    assert renamed.board == "main"

    changed = master.state("600001", date(2015, 1, 1))
    assert changed.name == "新名"
    assert changed.board == "star"
    assert changed.lot_size == 200


# ---------------------------------------------------------------- 随库规则文件


def test_shipped_rules_file_loads_and_supports_reuse() -> None:
    master = SecurityMasterImpl()  # 自动定位 rules/security.cn.json
    assert master.state("600001", date(2012, 1, 4)) is None  # 已退市、未重新上市
    assert master.state("600001", date(2021, 1, 4)).name == "示例股份"
    assert "600001" in master.detect_reuse()
