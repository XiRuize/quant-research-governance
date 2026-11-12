from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from qrg.clean.artifacts import CLEAN_REPORT, MANIFEST, QUALITY_REPORT, SENSITIVITY, TRADING_FLAGS, verify_manifest
from qrg.cli import main

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "cleaning.spec.json"
BASE = Path(__file__).resolve().parent / ".tmp" / "e2e"


def _panel(bad: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=3, freq="D")
    rows = []
    for code in ("600001", "600002"):
        for i, d in enumerate(dates):
            rows.append(
                {
                    "trade_date": d.strftime("%Y-%m-%d"),
                    "code": code,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0 + 0.1 * i,
                    "volume": 1000.0,
                    "amount": 1e6,
                }
            )
    df = pd.DataFrame(rows)
    if bad:
        df["volume"] = -1.0
    return df


def _factors() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "600001", "trade_date": "2024-01-02", "factor": 1.0},
            {"code": "600002", "trade_date": "2024-01-02", "factor": 1.0},
        ]
    )


def _setup(bad: bool = False) -> Path:
    base = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    _panel(bad).to_csv(base / "panel.csv", index=False)
    _factors().to_csv(base / "factors.csv", index=False)
    return base


def _run(base: Path) -> int:
    return main(
        [
            "clean",
            "--spec",
            str(SPEC),
            "--input",
            str(base / "panel.csv"),
            "--factors",
            str(base / "factors.csv"),
            "--out",
            str(base / "out"),
        ]
    )


def test_end_to_end_writes_artifacts(capsys) -> None:
    base = _setup()
    code = _run(base)
    out = base / "out"
    assert code == 0, capsys.readouterr()
    for name in (QUALITY_REPORT, CLEAN_REPORT, SENSITIVITY, TRADING_FLAGS, MANIFEST):
        assert (out / name).exists()
    assert verify_manifest(out)
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert "grid" in summary["steps"]


def test_blocking_data_returns_3(capsys) -> None:
    base = _setup(bad=True)
    code = _run(base)
    assert code == 3
    assert verify_manifest(base / "out")
