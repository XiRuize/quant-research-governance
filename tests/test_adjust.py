from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qrg.api import GateBlocked, Severity, SpecError
from qrg.clean.adjust import adjust_panel, roundtrip_error
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()

DATES = pd.date_range("2024-01-02", periods=4, freq="D")


def panel(code: str = "600001") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": DATES,
            "code": code,
            "open": [10.0, 10.0, 10.0, 10.0],
            "high": [10.0, 10.0, 10.0, 10.0],
            "low": [10.0, 10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 20.0, 20.0],
            "volume": [1.0, 1.0, 1.0, 1.0],
            "amount": [10.0, 10.0, 20.0, 20.0],
        }
    )


def factors(codes: tuple[str, ...] = ("600001",)) -> pd.DataFrame:
    rows = []
    for code in codes:
        rows += [
            {"code": code, "trade_date": DATES[0], "factor": 1.0},
            {"code": code, "trade_date": DATES[2], "factor": 2.0},
        ]
    return pd.DataFrame(rows)


def test_qfq_uses_base_factor() -> None:
    out, report = adjust_panel(panel(), factors(), SPEC, base_date="2024-01-02")
    assert report.ok
    assert out.loc[out["trade_date"] == DATES[2], "close_qfq"].iloc[0] == pytest.approx(40.0)


def test_roundtrip_is_reversible() -> None:
    err = roundtrip_error(pd.Series([10.0, 20.0]), pd.Series([1.0, 2.0]), 1.0)
    assert float(err.max()) < 1e-9


def test_missing_factor_is_nan_not_filled() -> None:
    df = pd.concat([panel("600001"), panel("600002")], ignore_index=True)
    out, report = adjust_panel(df, factors(("600001",)), SPEC)
    missing = out.loc[out["code"] == "600002", "close_qfq"]
    assert missing.isna().all()
    ids = {h.rule_id for h in report.hits}
    assert "ADJ.missing_factor" in ids


def test_decreasing_factor_raises() -> None:
    bad = pd.DataFrame(
        [
            {"code": "600001", "trade_date": DATES[0], "factor": 2.0},
            {"code": "600001", "trade_date": DATES[1], "factor": 1.0},
        ]
    )
    with pytest.raises(SpecError):
        adjust_panel(panel(), bad, SPEC)


def test_base_proxy_for_late_listing() -> None:
    late = panel("600002")
    late["trade_date"] = pd.date_range("2024-02-01", periods=4, freq="D")
    f = pd.DataFrame([{"code": "600002", "trade_date": late["trade_date"].iloc[0], "factor": 1.0}])
    out, report = adjust_panel(late, f, SPEC, base_date="2024-01-02")
    assert "ADJ.base_proxy" in {h.rule_id for h in report.hits}
    assert out["close_qfq"].notna().all()


def test_empty_panel() -> None:
    out, report = adjust_panel(panel().iloc[0:0], factors(), SPEC)
    assert out.empty
    assert report.ok
