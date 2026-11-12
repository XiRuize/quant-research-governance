"""F7 规范化模块的验收测试。

取整规则固定为 **Decimal ROUND_HALF_UP**（四舍五入、远离零）：
``10.005`` 按 tick=0.01 取整为 ``10.01``。这与 Python 内置 ``round`` 的
银行家舍入（会得到 10.00）不同，是本模块明确选择的口径。
"""
from __future__ import annotations

import pandas as pd
import pytest

from qrg.api import AuditReport, SpecError
from qrg.quality import DEFAULT_TICK, normalize_panel, pct_to_decimal


def _unit_hits(report: AuditReport) -> list:
    return [h for h in report.hits if h.rule_id.startswith("F7.unit_")]


def test_hand_to_share_and_idempotent() -> None:
    raw = pd.DataFrame(
        {
            "trade_date": ["2024-01-02"],
            "code": ["000001"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.0],
            "volume": [100],
            "amount": [1000.0],
        }
    )

    first, first_report = normalize_panel(raw, {"volume": "hand"})
    assert first["volume"].iloc[0] == 10000
    assert int(first["volume"].dtype == "int64")  # 无缺失时用原生 int64
    hits = _unit_hits(first_report)
    assert len(hits) == 1
    assert hits[0].rule_id == "F7.unit_volume"
    assert hits[0].row_count == 1
    assert hits[0].scope["from"] == "hand"
    assert hits[0].scope["to"] == "share"

    # 同一张已规范化表 + 同一组 hints 再跑：数值不变，换算命中行数为 0。
    second, second_report = normalize_panel(first, {"volume": "hand"})
    pd.testing.assert_frame_equal(first, second)
    assert second["volume"].iloc[0] == 10000
    assert sum(h.row_count for h in _unit_hits(second_report)) == 0


def test_k_yuan_to_yuan() -> None:
    raw = pd.DataFrame({"amount": [1.5, 2.0]})
    out, report = normalize_panel(raw, {"amount": "k_yuan"})

    assert out["amount"].tolist() == [1500.0, 2000.0]
    hits = [h for h in report.hits if h.rule_id == "F7.unit_amount"]
    assert len(hits) == 1
    assert hits[0].row_count == 2
    assert hits[0].scope["factor"] == 1000.0

    # 幂等：第二次不会再乘 1000。
    out2, report2 = normalize_panel(out, {"amount": "k_yuan"})
    pd.testing.assert_frame_equal(out, out2)
    assert out2["amount"].tolist() == [1500.0, 2000.0]
    assert sum(h.row_count for h in _unit_hits(report2)) == 0


def test_all_null_tokens_become_na() -> None:
    tokens = ["", " ", "-", "NULL", "None", "nan", "NaN", None]
    raw = pd.DataFrame(
        {
            "note": tokens,
            "close": tokens,
            "volume": tokens,
        }
    )

    out, report = normalize_panel(raw)

    for column in ("note", "close", "volume"):
        assert out[column].isna().all(), column
    null_hits = [h for h in report.hits if h.rule_id == "F7.null_tokens"]
    assert {h.scope["column"] for h in null_hits} == {"note", "close", "volume"}
    assert all(h.row_count == len(tokens) for h in null_hits)

    # 第二次运行不应再“转换”任何空值令牌。
    _, report2 = normalize_panel(out)
    assert not [h for h in report2.hits if h.rule_id == "F7.null_tokens" and h.row_count]


def test_pct_to_decimal() -> None:
    assert pct_to_decimal("5%") == pytest.approx(0.05)
    assert pct_to_decimal("0.05") == pytest.approx(0.05)  # 不重复除 100
    assert pct_to_decimal(" 5 % ") == pytest.approx(0.05)
    assert pct_to_decimal("５％") == pytest.approx(0.05)  # 全角
    assert pct_to_decimal(None) is pd.NA
    assert pct_to_decimal(pd.NA) is pd.NA

    raw = pd.DataFrame({"pct_chg": ["5%", "0.05", None]})
    out, report = normalize_panel(raw)
    assert out["pct_chg"].iloc[0] == pytest.approx(0.05)
    assert out["pct_chg"].iloc[1] == pytest.approx(0.05)
    assert pd.isna(out["pct_chg"].iloc[2])
    hits = [h for h in report.hits if h.rule_id == "F7.pct_decimal"]
    assert len(hits) == 1
    assert hits[0].row_count == 2


def test_code_padding_and_string_validation() -> None:
    raw = pd.DataFrame({"code": [1, "000001", " 600000 ", 2.0]})
    out, report = normalize_panel(raw)

    assert out["code"].tolist() == ["000001", "000001", "600000", "000002"]
    assert all(isinstance(v, str) for v in out["code"].tolist())
    hits = [h for h in report.hits if h.rule_id == "F7.code_pad"]
    assert len(hits) == 1
    # 只有 1、" 600000 "、2.0 被改写；已经是 6 位字符串的 "000001" 不动。
    assert hits[0].row_count == 3
    assert hits[0].scope["target_length"] == 6


def test_price_rounding_half_up() -> None:
    raw = pd.DataFrame({"close": [10.005, 10.004, 10.006]})
    out, report = normalize_panel(raw)

    # 固定口径：Decimal ROUND_HALF_UP；10.005 -> 10.01（不是 10.00）。
    assert out["close"].tolist() == pytest.approx([10.01, 10.00, 10.01])
    assert DEFAULT_TICK == 0.01
    hits = [h for h in report.hits if h.rule_id == "F7.price_round"]
    assert len(hits) == 1
    assert hits[0].row_count == 3
    assert hits[0].scope["rounding"] == "ROUND_HALF_UP"


def test_column_names_lowercased_and_unknown_preserved() -> None:
    raw = pd.DataFrame(
        {
            " Trade_Date ": ["2024-01-02"],
            "CLOSE": [10.0],
            "Mystery": [1],
        }
    )
    out, report = normalize_panel(raw)

    assert list(out.columns) == ["trade_date", "close", "mystery"]
    assert out["mystery"].iloc[0] == 1  # 不认识的列保留
    lower_hits = [h for h in report.hits if h.rule_id == "F7.columns_lowercased"]
    assert len(lower_hits) == 1
    unknown_hits = [h for h in report.hits if h.rule_id == "F7.unknown_columns"]
    assert len(unknown_hits) == 1
    assert unknown_hits[0].scope["columns"] == ["mystery"]
    assert unknown_hits[0].row_count == 0  # schema 级记录，不是行转换


def test_unit_hints_are_explicit_and_validated() -> None:
    with pytest.raises(SpecError):
        normalize_panel(pd.DataFrame({"volume": [1]}), {"volume": "lot"})
    with pytest.raises(SpecError):
        normalize_panel(pd.DataFrame({"close": [1.0]}), {"price": "yuan"})
    with pytest.raises(SpecError):
        normalize_panel(pd.DataFrame({"volume": [1]}), "hand")  # type: ignore[arg-type]
    with pytest.raises(SpecError):
        normalize_panel(pd.DataFrame({"close": [1.0]}), tick=0)


def test_full_panel_idempotent_and_second_run_has_zero_conversion_rows() -> None:
    raw = pd.DataFrame(
        {
            " Trade_Date ": ["2024-01-02", "2024-01-03"],
            "Code": [1, "000002"],
            "Name": [" 贵州　茅台 ", "平安银行\ufeff"],
            "Open": ["10.00", "11.00"],
            "High": ["10.50", "11.50"],
            "Low": ["9.90", "10.90"],
            "Close": ["10.005", "11.00"],
            "Volume": ["100", "200"],
            "Amount": ["1.5", "2.25"],
            "pct_chg": ["5%", "0.05"],
            "mystery": ["x", "y"],
        }
    )
    hints = {"volume": "hand", "amount": "k_yuan"}

    first, first_report = normalize_panel(raw, hints)
    second, second_report = normalize_panel(first, hints)

    pd.testing.assert_frame_equal(first, second)
    assert first["code"].tolist() == ["000001", "000002"]
    assert first["name"].tolist() == ["贵州茅台", "平安银行"]
    assert first["volume"].tolist() == [10000, 20000]
    assert first["amount"].tolist() == pytest.approx([1500.0, 2250.0])
    assert first["close"].tolist() == pytest.approx([10.01, 11.00])
    assert first["pct_chg"].tolist() == pytest.approx([0.05, 0.05])
    assert first["mystery"].tolist() == ["x", "y"]

    # 第二次运行：所有 hit 的转换行数都是 0（只剩未知列的 schema 记录）。
    assert all(h.row_count == 0 for h in second_report.hits)
    assert first_report.hits  # 第一次确实发生了转换
    assert isinstance(first_report, AuditReport)
    assert first_report.stage == "normalize"
