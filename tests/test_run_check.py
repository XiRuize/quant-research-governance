"""qrg run check 测试：契约 run.spec.json check_rules 的行为验证。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qrg.run_check import check_all_runs, check_run

V1_BASE = {
    "schema": "qrg-run-v1",
    "run_id": "QM-20261121-999",
    "title": "测试问题",
    "family": "10_baseline_portfolio",
    "created_at": "2026-11-21T00:00:00+08:00",
    "caliber": "UNI-CALIBER-v2",
    "data": {"source": "测试", "window": "2018-01~2026-06"},
    "verdict": {"code": "CONFIRMED", "note": "测试结论"},
    "headline": "SR 0.5（train 段，UNI-CALIBER-v2 口径）",
    "artifacts": {"scripts": [], "data": [], "results": ["07_deliver/x.md"]},
    "known_limitations": ["样本仅 train 段"],
    "revisions": [],
}


def _make_run(root: Path, family: str, run_id: str, payload: dict, *, readme: bool = True) -> Path:
    d = root / family / "runs" / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if readme:
        (d / "README.md").write_text("# 结论\n", encoding="utf-8")
    return d


def _find(findings, code, target_suffix="QM-20261121-999"):
    return [f for f in findings if f.code == code and f.target.endswith(target_suffix)]


@pytest.fixture()
def research_root(tmp_path: Path) -> Path:
    root = tmp_path / "research"
    (root / "10_baseline_portfolio" / "runs").mkdir(parents=True)
    return root


def test_v1_complete_passes(research_root):
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", dict(V1_BASE))
    assert check_all_runs(research_root) == []


def test_v1_missing_field_is_fatal(research_root):
    bad = {k: v for k, v in V1_BASE.items() if k != "headline"}
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", bad)
    findings = check_all_runs(research_root)
    assert _find(findings, "RUN_V1_FIELDS_MISSING") and findings[0].level == "fatal"


def test_v1_empty_revisions_ok_but_empty_limitations_warns(research_root):
    # revisions=[] 合法（无更正），known_limitations=[] 必须 warn
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", dict(V1_BASE, known_limitations=[]))
    findings = check_all_runs(research_root)
    assert not _find(findings, "RUN_V1_FIELDS_MISSING")
    assert _find(findings, "LIMITATIONS_EMPTY")


def test_run_id_mismatch_fatal(research_root):
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", dict(V1_BASE, run_id="QM-OTHER"))
    assert _find(check_all_runs(research_root), "RUN_ID_MISMATCH")


def test_missing_run_json_fatal(research_root):
    (research_root / "10_baseline_portfolio" / "runs" / "QM-20261121-998").mkdir(parents=True)
    findings = check_all_runs(research_root)
    assert any(f.level == "fatal" and f.code == "RUN_JSON_MISSING" for f in findings)


def test_legacy_only_enforces_run_id(research_root):
    legacy = {"run_id": "QM-OLD-001", "status": "COMPLETED"}
    _make_run(research_root, "10_baseline_portfolio", "QM-OLD-001", legacy, readme=False)
    findings = check_all_runs(research_root)
    fatals = [f for f in findings if f.level == "fatal"]
    assert not fatals
    codes = {f.code for f in findings}
    assert {"CALIBER_MISSING", "VERDICT_MISSING", "HEADLINE_MISSING", "CONCLUSION_DOC_MISSING"} <= codes


def test_verdict_format(research_root):
    bad = dict(V1_BASE, verdict={"code": "pass", "note": "小写"})
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", bad)
    assert _find(check_all_runs(research_root), "VERDICT_FORMAT")


def test_verdict_unknown_root_warns(research_root):
    bad = dict(V1_BASE, verdict={"code": "PASS_BUT_ALSO_MAGIC", "note": ""})
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", bad)
    assert _find(check_all_runs(research_root), "VERDICT_UNKNOWN_ROOT")


def test_caliber_registered_with_suffix(research_root):
    # "FREQ-GRID-v1 (inherits ...)" 形态：登记键 + 分隔符后缀 → 通过
    ok = dict(V1_BASE, caliber="FREQ-GRID-v1 (inherits UNI-CALIBER-v2 cost & execution)")
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", ok)
    assert not _find(check_all_runs(research_root), "CALIBER_UNREGISTERED")


def test_caliber_unregistered_fatal_for_v1(research_root):
    bad = dict(V1_BASE, caliber="随手写的口径")
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", bad)
    f = _find(check_all_runs(research_root), "CALIBER_UNREGISTERED")
    assert f and f[0].level == "fatal"


def test_caliber_custom_prefix_ok(research_root):
    ok = dict(V1_BASE, caliber="custom_一次性口径说明")
    _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", ok)
    assert not _find(check_all_runs(research_root), "CALIBER_UNREGISTERED")


def test_caliber_id_alias_for_legacy(research_root):
    legacy = {"run_id": "QM-OLD-002", "caliber_id": "EVAL-CALIBER-v2.2", "verdict": "CONFIRMED",
              "headline": "x", "known_limitations": ["x"]}
    _make_run(research_root, "10_baseline_portfolio", "QM-OLD-002", legacy, readme=False)
    findings = check_all_runs(research_root)
    assert not [f for f in findings if f.target.endswith("QM-OLD-002") and f.code.startswith("CALIBER")]


def test_shape_legacy_and_conclusion_doc(research_root):
    d = _make_run(research_root, "10_baseline_portfolio", "QM-20261121-999", dict(V1_BASE), readme=False)
    (d / "09_随手堆料").mkdir()
    findings = check_all_runs(research_root)
    assert _find(findings, "SHAPE_LEGACY")
    assert _find(findings, "CONCLUSION_DOC_MISSING")
