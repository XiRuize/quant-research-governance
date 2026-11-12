"""E1 门禁的确定性判定与产物写出。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qrg.api import AuditReport, GateBlocked, RuleHit, Severity
from qrg.gov.gate import (
    GATE_JSON,
    REQUIRED_ARTIFACTS,
    Decision,
    decide,
    write_decision,
)

TMP = Path(__file__).resolve().parent / ".tmp" / "gate"


def _report(*hits: RuleHit, stage: str = "validate") -> AuditReport:
    report = AuditReport(stage=stage)
    for hit in hits:
        report.add(hit)
    return report


def _hit(rule_id: str, severity: Severity, message: str = "") -> RuleHit:
    return RuleHit(rule_id=rule_id, severity=severity, message=message or rule_id)


def test_all_info_is_pass() -> None:
    decision = decide([_report(_hit("I.note", Severity.INFO, "仅提示"))])

    assert isinstance(decision, Decision)
    assert decision.status == "pass"
    assert decision.max_severity == Severity.INFO
    assert decision.blocking_rules == ()
    assert decision.warnings == ()
    assert decision.manifest_ok is True


def test_l2_only_is_warn() -> None:
    decision = decide([_report(_hit("V.price", Severity.L2, "价格异常"))])

    assert decision.status == "warn"
    assert decision.max_severity == Severity.L2
    assert decision.blocking_rules == ()
    assert decision.warnings == ("V.price: 价格异常",)
    assert decision.manifest_ok is True


def test_blocked_reports_block_and_collect_rule_ids() -> None:
    first = _report(
        _hit("B.block", Severity.BLOCKED, "硬阻断"),
        _hit("V.price", Severity.WARN, "价格警告"),
    )
    second = _report(
        _hit("A.block", Severity.BLOCKED, "另一硬阻断"),
        _hit("B.block", Severity.BLOCKED, "重复硬阻断"),
    )

    decision = decide([first, second])

    assert decision.status == "blocked"
    assert decision.max_severity == Severity.BLOCKED
    assert decision.blocking_rules == ("A.block", "B.block")
    assert "V.price: 价格警告" in decision.warnings

    with pytest.raises(GateBlocked):
        first.raise_if_blocked()
    with pytest.raises(GateBlocked):
        decision.raise_if_blocked()


def test_manifest_missing_artifact_is_at_least_warn() -> None:
    partial = {name: "sha256:deadbeef" for name in REQUIRED_ARTIFACTS[:-1]}

    decision = decide([], partial)

    assert decision.manifest_ok is False
    assert decision.status == "warn"
    assert decision.max_severity == Severity.WARN
    assert decision.blocking_rules == ()
    assert any("manifest" in warning for warning in decision.warnings)


def test_manifest_complete_allows_pass() -> None:
    manifest = {name: "sha256:deadbeef" for name in REQUIRED_ARTIFACTS}
    manifest["spec_version"] = "test"

    decision = decide([_report(_hit("I.note", Severity.INFO))], manifest)

    assert decision.manifest_ok is True
    assert decision.status == "pass"


def test_write_decision_is_byte_deterministic() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    decision = decide([_report(_hit("V.price", Severity.L2, "价格异常"))])

    first_path = write_decision(TMP, decision)
    first_bytes = first_path.read_bytes()

    second_path = write_decision(TMP, decision)
    second_bytes = second_path.read_bytes()

    assert first_path == TMP / GATE_JSON
    assert second_path == first_path
    assert first_bytes == second_bytes

    payload = json.loads(first_bytes.decode("utf-8"))
    assert payload == {
        "status": "warn",
        "max_severity": "L2",
        "blocking_rules": [],
        "warnings": ["V.price: 价格异常"],
        "manifest_ok": True,
    }
    assert payload == decision.to_dict()

    shutil.rmtree(TMP, ignore_errors=True)


def test_empty_reports_is_pass() -> None:
    decision = decide([])

    assert decision.status == "pass"
    assert decision.max_severity == Severity.INFO
    assert decision.blocking_rules == ()
    assert decision.warnings == ()
    assert decision.manifest_ok is True
