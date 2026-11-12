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
    assert doc.spec_version == "0.1.0"
    assert doc.price_adjustment.qfq_base_date.year == 2026


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

