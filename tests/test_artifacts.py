from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from qrg.api import AuditReport, RuleHit, Severity
from qrg.clean.artifacts import (
    CLEAN_REPORT,
    MANIFEST,
    QUALITY_REPORT,
    SENSITIVITY,
    TRADING_FLAGS,
    verify_manifest,
    write_artifacts,
)
from qrg.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()
TMP = Path(__file__).resolve().parent / ".tmp" / "artifacts"


def _report() -> AuditReport:
    r = AuditReport(stage="validate", meta={"rows": 10})
    r.add(RuleHit("V.high_lt_low", Severity.L2, "high < low", {"rate": 0.1}, 1, ("2024-01-02:600001",)))
    return r


def _flags() -> pd.DataFrame:
    return pd.DataFrame(
        {"trade_date": ["2024-01-03", "2024-01-02"], "code": ["600001", "600001"], "suspended": [False, False]}
    )


def test_writes_four_artifacts_and_manifest() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    manifest = write_artifacts(TMP, SPEC, [_report()], _flags())
    for name in (QUALITY_REPORT, CLEAN_REPORT, SENSITIVITY, TRADING_FLAGS, MANIFEST):
        assert (TMP / name).exists()
    assert all(v.startswith("sha256:") for k, v in manifest.items() if k != "spec_version")
    assert verify_manifest(TMP)


def test_artifact_hashes_are_deterministic() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    a = write_artifacts(TMP / "a", SPEC, [_report()], _flags())
    b = write_artifacts(TMP / "b", SPEC, [_report()], _flags())
    assert a == b


def test_manifest_detects_tampering() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    write_artifacts(TMP, SPEC, [_report()], _flags())
    path = TMP / QUALITY_REPORT
    path.write_text("{}", encoding="utf-8")
    assert not verify_manifest(TMP)
