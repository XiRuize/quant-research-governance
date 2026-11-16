from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pandas as pd

from qrg.clean.artifacts import COVERAGE, PANEL, verify_manifest, write_artifacts
from qrg.clean.pit import pit_merge
from qrg.cli import main
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "spec" / "cleaning.spec.json"
TMP = Path(__file__).resolve().parent / ".tmp" / "v06"


def _dir() -> Path:
    d = TMP / uuid.uuid4().hex
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_financial_us_dtype_merges() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "code": ["000001", "000001"],
        }
    )
    financial = pd.DataFrame(
        {
            "code": ["000001"],
            "notice_date": pd.to_datetime(["2023-12-31"]).astype("datetime64[us]"),
            "item": ["roe"],
            "value": [10.0],
        }
    )
    out, report = pit_merge(panel, financial)
    assert out["roe"].notna().all()
    assert report.meta["financial_coverage"] == 1.0


def test_artifacts_write_optional_panel_and_coverage() -> None:
    d = _dir()
    spec = load_spec(SPEC_PATH).to_panel_spec()
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-03", "2024-01-02"]),
            "code": ["000001", "000001"],
            "total_mv": [1.0, 2.0],
        }
    )
    manifest = write_artifacts(d, spec, flags=panel.loc[:, ["trade_date", "code"]], panel=panel, coverage={"window": {"start": "2024-01-02"}})
    assert PANEL in manifest and COVERAGE in manifest
    assert verify_manifest(d)
    written = pd.read_parquet(d / PANEL)
    assert list(written["trade_date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]


def test_cli_market_cap_writes_panel(capsys) -> None:
    d = _dir()
    rows = []
    for i, day in enumerate(pd.bdate_range("2024-01-02", periods=3)):
        rows.append(
            {
                "trade_date": day.strftime("%Y-%m-%d"),
                "code": "600001",
                "open": 10.0 + i,
                "high": 10.5 + i,
                "low": 9.5 + i,
                "close": 10.0 + i,
                "volume": 1000.0,
                "amount": 1e6,
            }
        )
    pd.DataFrame(rows).to_csv(d / "panel.csv", index=False)
    pd.DataFrame(
        {
            "code": ["600001"] * 3,
            "trade_date": pd.bdate_range("2024-01-02", periods=3).strftime("%Y-%m-%d"),
            "total_mv": [1e10, 1.1e10, 1.2e10],
            "circ_mv": [9e9, 9.9e9, 1.1e10],
        }
    ).to_csv(d / "mv.csv", index=False)

    code = main(
        [
            "clean",
            "--spec",
            str(SPEC_PATH),
            "--input",
            str(d / "panel.csv"),
            "--market-cap",
            str(d / "mv.csv"),
            "--out",
            str(d / "out"),
            "--replay-step",
            "1",
        ]
    )
    assert code == 0, capsys.readouterr().err
    out = pd.read_parquet(d / "out" / PANEL)
    assert out["total_mv"].notna().all()
    assert verify_manifest(d / "out")
