from __future__ import annotations

import json
from pathlib import Path

import pytest

from qrg.api import PanelSpec, SpecError
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "cleaning.spec.json"


def test_load_default_spec() -> None:
    doc = load_spec(SPEC)
    assert all(p.isdigit() for p in doc.spec_version.split("."))
    assert len(doc.spec_version.split(".")) == 3
    assert doc.price_adjustment.qfq_base_date.year == 2026


def test_spec_merged_legacy_contract() -> None:
    """0.2.0 起契约必须携带 v1.0-locked 并入的 9 块与 C1–C18 裁决。"""
    doc = load_spec(SPEC)
    extra = doc.model_extra or {}
    for block in (
        "dedup_and_units",
        "time_alignment",
        "universe_and_board",
        "limit_and_st",
        "financial_pit",
        "industry_pit",
        "outlier",
        "cost",
        "portfolio_constraints",
        "data_status_enum",
    ):
        assert block in extra, f"missing merged block: {block}"
    rulings = {r["id"] for r in extra["rulings"]["items"]}
    assert {"C13", "C14", "C15", "C16", "C17", "C18"} <= rulings
    assert doc.execution["price_basis"] == "t_plus_1_open"


def test_to_panel_spec() -> None:
    panel: PanelSpec = load_spec(SPEC).to_panel_spec()
    assert "60" in panel.code_whitelist
    assert panel.violation_rate_fatal >= panel.violation_rate_max
    assert panel.raw["timezone"] == "Asia/Shanghai"


def test_bad_version(tmp_spec_dir: Path) -> None:
    payload = json.loads(SPEC.read_text(encoding="utf-8"))
    payload["spec_version"] = "v1"
    p = tmp_spec_dir / "bad.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(p)


def test_missing_section(tmp_spec_dir: Path) -> None:
    payload = json.loads(SPEC.read_text(encoding="utf-8"))
    del payload["hard_validation"]
    p = tmp_spec_dir / "bad.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(p)


def test_fatal_lt_max(tmp_spec_dir: Path) -> None:
    payload = json.loads(SPEC.read_text(encoding="utf-8"))
    payload["hard_validation"]["violation_rate_max"] = 0.5
    payload["hard_validation"]["violation_rate_fatal"] = 0.1
    p = tmp_spec_dir / "bad.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(p)


def test_cli_stages(capsys: pytest.CaptureFixture[str]) -> None:
    from qrg.cli import main

    assert main(["stages"]) == 0
    out = capsys.readouterr().out
    assert "normalize" in out
    assert "pending" in out or "ready" in out

