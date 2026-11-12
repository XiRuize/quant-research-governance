from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from qrg.cli import main

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "cleaning.spec.json"
BASE = Path(__file__).resolve().parent / ".tmp" / "e2e_v04"


def _dir() -> Path:
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean(d: Path, capsys, extra: list[str] | None = None) -> int:
    panel = pd.DataFrame(
        [
            {"trade_date": "2024-01-02", "code": "600001", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000.0, "amount": 1e6},
            {"trade_date": "2024-01-03", "code": "600001", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.1, "volume": 1000.0, "amount": 1e6},
        ]
    )
    panel.to_csv(d / "panel.csv", index=False)
    pd.DataFrame([{"code": "600001", "trade_date": "2024-01-02", "factor": 1.0}]).to_csv(d / "factors.csv", index=False)
    args = [
        "clean",
        "--spec",
        str(SPEC),
        "--input",
        str(d / "panel.csv"),
        "--factors",
        str(d / "factors.csv"),
        "--out",
        str(d / "out"),
        "--run-id",
        "run-test-1",
    ]
    return main(args + (extra or []))


def test_clean_registers_trial_and_gate_verifies(capsys) -> None:
    d = _dir()
    code = _clean(d, capsys, ["--registry", str(d / "registry")])
    clean_out = capsys.readouterr()
    assert code == 0, clean_out

    ledger = (d / "registry" / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1
    rec = json.loads(ledger[0])
    assert rec["run_id"] == "run-test-1"
    assert rec["spec_version"] == "0.1.0"

    code = main(["gate", "--dir", str(d / "out")])
    gate_out = capsys.readouterr()
    assert code == 0, gate_out
    summary = json.loads(gate_out.out)
    assert summary["manifest_ok"] is True
    assert summary["run_id"] == "run-test-1"


def test_gate_detects_tampering(capsys) -> None:
    d = _dir()
    assert _clean(d, capsys) == 0
    target = d / "out" / "data_quality_report.json"
    target.write_text("{}", encoding="utf-8")
    code = main(["gate", "--dir", str(d / "out")])
    assert code == 3


def test_gate_missing_dir(capsys) -> None:
    assert main(["gate", "--dir", str(BASE / "nope")]) == 4
