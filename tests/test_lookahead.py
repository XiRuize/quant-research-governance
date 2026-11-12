from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qrg.api import GateBlocked, Severity
from qrg.clean.lookahead import replay_check, scan_source

TMP = Path(__file__).resolve().parent / ".tmp" / "lookahead"


def panel() -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=5, freq="D")
    return pd.DataFrame(
        {
            "trade_date": dates,
            "code": ["600001"] * 5,
            "close": [10.0, 10.5, 11.0, 10.8, 11.2],
        }
    )


def safe_compute(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out.groupby("code")["close"].pct_change()
    return out


def leaking_compute(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["leak"] = out.groupby("code")["close"].shift(-1)
    return out


def test_safe_feature_passes() -> None:
    report = replay_check(panel(), safe_compute, feature_cols=["ret"])
    assert report.ok
    assert report.meta["diff_rows"] == 0


def test_leaking_feature_is_blocked() -> None:
    report = replay_check(panel(), leaking_compute, feature_cols=["leak"])
    assert report.max_severity == Severity.BLOCKED
    assert report.meta["diff_rows"] > 0
    with pytest.raises(GateBlocked):
        report.raise_if_blocked()


def test_scan_detects_future_functions() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    f = TMP / "leak.py"
    f.write_text(
        "x = df.groupby('code')['close'].shift(-1)\n"
        "y = df['close'].rolling(5, center=True).mean()\n",
        encoding="utf-8",
    )
    report = scan_source(f)
    assert report.max_severity == Severity.BLOCKED
    assert len(report.hits) == 2


def test_scan_clean_file_passes() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    f = TMP / "clean.py"
    f.write_text("x = df['close'].rolling(5).mean()\n", encoding="utf-8")
    assert scan_source(f).ok
