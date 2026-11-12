from __future__ import annotations

import json
import shutil
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

from qrg.calendar.regime import RegimeCalendar
from qrg.calendar.session import SessionCalendar
from qrg.clean.artifacts import verify_manifest
from qrg.clean.grid import build_grid
from qrg.cli import main
from qrg.master.security import SecurityMasterImpl

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "cleaning.spec.json"
BASE = Path(__file__).resolve().parent / ".tmp" / "e2e_v02"

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def _dir() -> Path:
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _panel() -> pd.DataFrame:
    rows = [
        {"trade_date": d, "code": "600001", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1000.0, "amount": 10000.0}
        for d in (D1, D2)
    ]
    return pd.DataFrame(rows)


def _master() -> SecurityMasterImpl:
    return SecurityMasterImpl(
        events=[{"code": "600001", "event": "listed", "effective_date": "2010-01-04", "payload": {"name": "A", "board": "main"}}]
    )


def _flags(panel: pd.DataFrame) -> pd.DataFrame:
    flags, _ = build_grid(panel, SessionCalendar([D1, D2, D3]), _master(), RegimeCalendar(), D1, D3)
    return flags


def test_simulate_partial_and_suspended(capsys) -> None:
    d = _dir()
    panel = _panel()
    flags = _flags(panel)
    flags.to_parquet(d / "flags.parquet", index=False)
    panel.to_parquet(d / "panel.parquet", index=False)
    pd.DataFrame(
        [
            {"order_id": "o1", "trade_date": str(D2), "code": "600001", "side": "buy", "qty": 1000.0, "ref_price": 10.0},
            {"order_id": "o2", "trade_date": str(D3), "code": "600001", "side": "buy", "qty": 100.0, "ref_price": 10.0},
        ]
    ).to_csv(d / "orders.csv", index=False)

    code = main(
        [
            "simulate",
            "--orders",
            str(d / "orders.csv"),
            "--flags",
            str(d / "flags.parquet"),
            "--panel",
            str(d / "panel.parquet"),
            "--out",
            str(d / "sim"),
        ]
    )
    assert code == 0, capsys.readouterr()
    fills = pd.read_parquet(d / "sim" / "fills.parquet")
    assert len(fills) == 2
    o1 = fills.loc[fills["order_id"] == "o1"].iloc[0]
    o2 = fills.loc[fills["order_id"] == "o2"].iloc[0]
    assert o1["qty"] == 100.0 and o1["reason"] == "partial"  # 1000 * 0.1
    assert o2["qty"] == 0.0 and o2["reason"] == "suspended"


def test_cost_attribution_and_reconcile(capsys) -> None:
    d = _dir()
    pd.DataFrame(
        [
            {"order_id": "o1", "trade_date": str(D2), "code": "600001", "side": "buy", "qty": 100.0, "price": 10.0},
            {"order_id": "o2", "trade_date": str(D2), "code": "600001", "side": "sell", "qty": 100.0, "price": 11.0},
        ]
    ).to_csv(d / "fills.csv", index=False)
    pd.DataFrame(
        [
            {"order_id": "o1", "impact_bps": 5.0, "delay_bps": 1.0, "spread_bps": 0.0, "overnight_bps": 0.0, "residual_bps": 0.0},
            {"order_id": "o2", "impact_bps": 4.0, "delay_bps": 0.0, "spread_bps": 2.0, "overnight_bps": 0.0, "residual_bps": 0.0},
        ]
    ).to_csv(d / "slip.csv", index=False)

    code = main(
        [
            "cost",
            "--fills",
            str(d / "fills.csv"),
            "--slippage",
            str(d / "slip.csv"),
            "--out",
            str(d / "cost"),
        ]
    )
    assert code == 0, capsys.readouterr()
    frame = pd.read_parquet(d / "cost" / "attribution.parquet")
    assert frame["total_cost"].sum() > 0
    item_cols = [c for c in frame.columns if c.startswith("fee:")]
    slip_cols = [
        c
        for c in frame.columns
        if c.endswith("_cost") and c not in {"fee_total", "total_cost", "slippage_total"}
    ]
    rebuilt = frame[item_cols].sum(axis=1) + frame[slip_cols].sum(axis=1)
    assert ((rebuilt - frame["total_cost"]).abs() < 1e-6).all()


def test_clean_writes_gate_and_provenance(capsys) -> None:
    d = _dir()
    panel = pd.DataFrame(
        [
            {"trade_date": str(day), "code": "600001", "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000.0, "amount": 1e6}
            for day in (D1, D2)
        ]
    )
    panel.to_csv(d / "panel.csv", index=False)
    pd.DataFrame([{"code": "600001", "trade_date": str(D1), "factor": 1.0}]).to_csv(d / "factors.csv", index=False)

    code = main(
        [
            "clean",
            "--spec",
            str(SPEC),
            "--input",
            str(d / "panel.csv"),
            "--factors",
            str(d / "factors.csv"),
            "--out",
            str(d / "out"),
        ]
    )
    assert code == 0, capsys.readouterr()
    out = d / "out"
    gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    assert gate["status"] in {"pass", "warn"}
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert prov["spec_version"] == "0.1.0"
    assert "regime.cn.json" in prov["rule_hashes"]
    assert verify_manifest(out)
