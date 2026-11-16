from __future__ import annotations

import pandas as pd

from qrg.quality.coverage import (
    RULE_FACTOR_START,
    RULE_SNAPSHOT,
    build_coverage,
)


def _panel(days: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2024-01-02", periods=days),
            "code": ["000001"] * days,
        }
    )


def test_factor_start_gap_warns() -> None:
    factors = pd.DataFrame({"code": ["000001"], "trade_date": ["2024-01-03"], "factor": [1.0]})
    _, report = build_coverage(_panel(), factors=factors)
    assert any(h.rule_id == RULE_FACTOR_START for h in report.hits)
    assert report.stage == "coverage"


def test_no_gap_when_factor_covers_window() -> None:
    factors = pd.DataFrame({"code": ["000001"], "trade_date": ["2023-12-01"], "factor": [1.0]})
    _, report = build_coverage(_panel(), factors=factors)
    assert not any(h.rule_id == RULE_FACTOR_START for h in report.hits)


def test_single_vintage_snapshot_warns() -> None:
    _, report = build_coverage(_panel(), vintages={"stock_basic": {"snapshot_date": "2026-10-31"}})
    hits = [h for h in report.hits if h.rule_id == RULE_SNAPSHOT]
    assert hits and hits[0].severity.name == "WARN"


def test_coverage_records_input_spans() -> None:
    meta, _ = build_coverage(
        _panel(),
        financial=pd.DataFrame({"code": ["000001"], "notice_date": ["2024-01-02"], "item": ["roe"], "value": [1.0]}),
    )
    assert meta["window"]["start"] == "2024-01-02"
    assert meta["inputs"]["financial"]["min"] == "2024-01-02"
    assert meta["inputs"]["panel"]["rows"] == 3
