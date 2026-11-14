from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pandas as pd

from qrg.clean.artifacts import verify_manifest
from qrg.cli import main

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "cleaning.spec.json"
BASE = Path(__file__).resolve().parent / ".tmp" / "e2e_partition"


def _dir() -> Path:
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _panel() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", "2025-12-31")
    rows = []
    for code in ("600001", "600002"):
        for i, d in enumerate(dates):
            close = 10 + (i % 50) * 0.01
            rows.append(
                {
                    "trade_date": d.strftime("%Y-%m-%d"),
                    "code": code,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000.0,
                    "amount": 1e6,
                }
            )
    return pd.DataFrame(rows)


def _run(out: Path, extra: list[str], capsys) -> int:
    common = [
        "clean",
        "--spec",
        str(SPEC),
        "--input",
        str(out.parent / "panel.csv"),
        "--out",
        str(out),
        "--replay-step",
        "1",
    ]
    code = main(common + extra)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return code


def test_cli_partition_matches_single(capsys) -> None:
    d = _dir()
    _panel().to_csv(d / "panel.csv", index=False)

    _run(d / "single", [], capsys)
    _run(d / "part", ["--partition-year"], capsys)

    single = pd.read_parquet(d / "single" / "trading_flags.parquet")
    part = pd.read_parquet(d / "part" / "trading_flags.parquet")
    columns = ["trade_date", "code", "limit_pct", "suspended", "tradable"]
    pd.testing.assert_frame_equal(
        single.loc[:, columns].sort_values(["trade_date", "code"]).reset_index(drop=True),
        part.loc[:, columns].sort_values(["trade_date", "code"]).reset_index(drop=True),
    )
    assert verify_manifest(d / "part")
