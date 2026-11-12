from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from qrg.api import SpecError
from qrg.gov.provenance import collect, load_record, sha256_file, trace_artifact, write_record

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(__file__).resolve().parent / ".tmp" / "provenance"


def _dir() -> Path:
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest() -> dict[str, str]:
    return {
        "data_quality_report.json": "sha256:aa",
        "clean_report.json": "sha256:bb",
        "spec_version": "0.1.0",
    }


def test_collect_hashes_rules() -> None:
    rec = collect("run-1", "0.1.0", ROOT / "rules", _manifest(), {"k": 1})
    assert "regime.cn.json" in rec.rule_hashes
    assert rec.config_fingerprint.startswith("sha256:")


def test_write_and_load_roundtrip() -> None:
    d = _dir()
    rec = collect("run-2", "0.1.0", ROOT / "rules", _manifest())
    path = write_record(d, rec)
    assert path.exists()
    loaded = load_record(d)
    assert loaded.run_id == "run-2"
    assert dict(loaded.artifacts) == {"clean_report.json": "sha256:bb", "data_quality_report.json": "sha256:aa"}


def test_deterministic_bytes() -> None:
    rec = collect("run-3", "0.1.0", ROOT / "rules", _manifest())
    a, b = _dir(), _dir()
    write_record(a, rec)
    write_record(b, rec)
    assert sha256_file(a / "provenance.json") == sha256_file(b / "provenance.json")


def test_empty_manifest_rejected() -> None:
    with pytest.raises(SpecError):
        collect("run", "0.1.0", ROOT / "rules", {})


def test_trace_artifact() -> None:
    d = _dir()
    write_record(d, collect("run-4", "0.1.0", ROOT / "rules", _manifest()))
    info = trace_artifact(d, "clean_report.json")
    assert info["run_id"] == "run-4"
    assert info["hash"] == "sha256:bb"


def test_trace_unknown_artifact() -> None:
    d = _dir()
    write_record(d, collect("run-5", "0.1.0", ROOT / "rules", _manifest()))
    with pytest.raises(SpecError):
        trace_artifact(d, "nope.json")


def test_missing_rules_dir() -> None:
    with pytest.raises(SpecError):
        collect("run", "0.1.0", ROOT / "nope", _manifest())
