from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qrg.api import GateBlocked, Severity, SpecError
from qrg.clean.validate import validate_panel
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()


def make(n: int = 100) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=n, freq="D")
    return pd.DataFrame(
        {
            "trade_date": dates,
            "code": ["600001"] * n,
            "open": [10.0] * n,
            "high": [10.5] * n,
            "low": [9.5] * n,
            "close": [10.0] * n,
            "volume": [1000.0] * n,
            "amount": [1e6] * n,
        }
    )


def test_clean_panel_is_ok() -> None:
    report = validate_panel(make(), SPEC)
    assert report.ok
    assert report.hits == []


def test_missing_column_raises() -> None:
    with pytest.raises(SpecError):
        validate_panel(make().drop(columns=["amount"]), SPEC)


def test_high_lt_low() -> None:
    df = make()
    df.loc[0, "high"] = 1.0
    report = validate_panel(df, SPEC)
    # 1/100 = 1%：等于 fatal 阈值，按“严格大于才 BLOCKED”的规则落 L2
    assert report.max_severity == Severity.L2
    assert report.hits[0].rule_id == "V.high_lt_low"
    assert report.hits[0].row_count == 1


def test_high_low_above_l2_threshold() -> None:
    df = make(100)
    df.loc[:5, "high"] = 1.0  # 6% > 0.1% max, < 1% fatal? 6% > 1% fatal -> BLOCKED
    report = validate_panel(df, SPEC)
    assert report.max_severity == Severity.BLOCKED
    with pytest.raises(GateBlocked):
        report.raise_if_blocked()


def test_code_whitelist() -> None:
    bad = make()
    bad["code"] = "A00001"  # 全部非法 -> 100% 违规 -> BLOCKED
    report = validate_panel(bad, SPEC)
    assert report.max_severity == Severity.BLOCKED
    assert not report.ok


def test_negative_volume_and_nonpositive_price() -> None:
    df = make()
    df.loc[1, "volume"] = -1
    df.loc[2, "close"] = 0
    ids = {h.rule_id for h in validate_panel(df, SPEC).hits}
    assert "V.negative_volume" in ids
    assert "V.price_le_zero" in ids


def test_empty_panel() -> None:
    df = make().iloc[0:0]
    report = validate_panel(df, SPEC)
    assert report.ok
    assert report.meta["rows"] == 0
