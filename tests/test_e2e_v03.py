from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from qrg.cli import main

BASE = Path(__file__).resolve().parent / ".tmp" / "e2e_v03"


def _dir() -> Path:
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _panel() -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=40, freq="D")
    rows = []
    for code, amount in (("600001", 1e7), ("600002", 5e6)):
        for d in dates:
            rows.append({"trade_date": d, "code": code, "volume": 1e6, "amount": amount})
    return pd.DataFrame(rows)


def _run_capacity(d: Path, capsys) -> pd.DataFrame:
    _panel().to_parquet(d / "panel.parquet", index=False)
    pd.DataFrame([{"code": "600001", "weight": 0.6}, {"code": "600002", "weight": 0.4}]).to_csv(
        d / "weights.csv", index=False
    )
    code = main(
        [
            "capacity",
            "--weights",
            str(d / "weights.csv"),
            "--panel",
            str(d / "panel.parquet"),
            "--out",
            str(d / "cap"),
            "--gross-alpha-bps",
            "100",
        ]
    )
    assert code == 0, capsys.readouterr()
    summary = json.loads((d / "cap" / "capacity.json").read_text(encoding="utf-8"))
    assert summary["uncalibrated"] is True
    assert summary["capacity_aum"] is not None
    return pd.read_parquet(d / "cap" / "capacity_curve.parquet")


def test_capacity_curve_monotonic(capsys) -> None:
    d = _dir()
    curve = _run_capacity(d, capsys)
    assert curve["net_bps"].is_monotonic_decreasing
    assert curve["feasible"].iloc[0]
    assert not curve["feasible"].iloc[-1]


def test_tail_scenarios_reproducible(capsys) -> None:
    d = _dir()
    _run_capacity(d, capsys)
    outs = []
    for i in range(2):
        out = d / f"tail{i}"
        code = main(["tail", "--curve", str(d / "cap" / "capacity_curve.parquet"), "--out", str(out)])
        assert code == 0, capsys.readouterr()
        outs.append(pd.read_parquet(out / "tail_curves.parquet"))
    pd.testing.assert_frame_equal(outs[0], outs[1])
    assert set(outs[0]["scenario"]) == {"normal", "stress", "illiquid"}
    report = json.loads((d / "tail0" / "tail.json").read_text(encoding="utf-8"))["report"]
    assert report["stage"] == "tail"
