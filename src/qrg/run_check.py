"""qrg run check —— spec/run.spec.json check_rules 的可执行版。

发现逻辑：扫 research/<family>/runs/<run_id>/（有 run.json 与无 run.json 都算），
按 schema 分派：
- schema=qrg-run-v1 → 12 必填字段缺一即 fatal；
- 无 schema 或 schema 不符 → legacy 门：只强制 run_id，其余缺失一律 warn。

口径登记表与 verdict 词根直接取自契约文件，不在此硬编码第二份——
契约改了这里自动跟随（避免"契约说 A、代码查 B"的第三种口径）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .registry import Finding

_REQUIRED_V1 = (
    "run_id", "schema", "title", "family", "created_at", "caliber",
    "data", "verdict", "headline", "artifacts", "known_limitations", "revisions",
)

# 顶层子目录语义槽位（spec/run.spec.json → segment_vocabulary）
_SEGMENTS = {
    "00_anchor", "01_spec", "02_data", "03_diagnose", "03_out",
    "04_adjudicate", "05_portfolio", "06_backtest", "07_deliver",
}

_VERDICT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# 允许出现在 run 根目录的文件（不触发 shape_legacy）
_KNOWN_FILES = {"run.json", "README.md"}


def _load_run_spec() -> dict[str, Any]:
    spec_path = Path(__file__).resolve().parents[2] / "spec" / "run.spec.json"
    return json.loads(spec_path.read_text(encoding="utf-8"))


def _run_spec_cached() -> dict[str, Any]:
    global _SPEC_CACHE  # noqa: PLW0603
    if _SPEC_CACHE is None:
        _SPEC_CACHE = _load_run_spec()
    return _SPEC_CACHE


_SPEC_CACHE: dict[str, Any] | None = None

_CALIBER_SEP = (" ", "(", "（", ":", "：", "；", ";", "｜", "｜", "\t", "/", "-(")


def _caliber_registered(caliber: str, calibers: set[str]) -> bool:
    """精确命中，或以某登记键开头、后跟分隔符（允许带说明后缀）。"""
    if caliber in calibers or caliber.startswith("custom_"):
        return True
    return any(caliber.startswith(k) and caliber[len(k):len(k) + 1] in _CALIBER_SEP for k in calibers)


def discover_runs(research_root: Path) -> list[Path]:
    """research/<family>/runs/<run_id>/ 全量发现；无 run.json 的也返回（本身就是 fatal）。"""
    out: list[Path] = []
    if not research_root.is_dir():
        return out
    for family in sorted(p for p in research_root.iterdir() if p.is_dir()):
        runs_dir = family / "runs"
        if not runs_dir.is_dir():
            continue
        out += sorted(p for p in runs_dir.iterdir() if p.is_dir())
    return out


def check_run(run_dir: Path, research_root: Path) -> list[Finding]:
    spec = _run_spec_cached()
    run_id_hint = run_dir.name
    rel = str(run_dir.relative_to(research_root))
    findings: list[Finding] = []

    rj_path = run_dir / "run.json"
    if not rj_path.is_file():
        findings.append(Finding("fatal", "RUN_JSON_MISSING", rel, "run.json 不存在"))
        return findings

    try:
        rj = json.loads(rj_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding("fatal", "RUN_JSON_UNPARSEABLE", rel, f"run.json 解析失败: {exc}"))
        return findings

    is_v1 = rj.get("schema") == spec["run_json"]["schema_value_v1"]
    lvl = "fatal" if is_v1 else "warn"

    # F2：run_id 与目录名一致（对 legacy 同样强制）
    rid = rj.get("run_id")
    if not rid:
        findings.append(Finding("fatal", "RUN_ID_MISSING", rel, "run.json 缺 run_id"))
    elif str(rid) != run_id_hint:
        findings.append(Finding("fatal", "RUN_ID_MISMATCH", rel, f"run_id={rid} 与目录名 {run_id_hint} 不一致"))

    # F3：v1 必填字段。revisions=[]（尚无更正）合法，不视为缺失
    if is_v1:
        missing = [k for k in _REQUIRED_V1 if rj.get(k) in (None, "", {})]
        if missing:
            findings.append(Finding("fatal", "RUN_V1_FIELDS_MISSING", rel, "缺必填字段: " + ", ".join(missing)))
        fam = rj.get("family")
        if fam and not (research_root / str(fam)).is_dir():
            findings.append(Finding("warn", "FAMILY_UNKNOWN", rel, f"family={fam} 在 research/ 下不存在"))

    # F4：caliber 登记。迁移工作流用 caliber_id + caliber_doc 指向口径文档，
    # legacy 门下等价认可；登记命中允许「登记键 + 分隔符后缀」形式
    # （如 "FREQ-GRID-v1 (inherits UNI-CALIBER-v2 ...)"）。
    caliber = rj.get("caliber") or rj.get("caliber_id")
    calibers = set(spec["caliber_registry"]["entries"])
    if not caliber or (isinstance(caliber, str) and not caliber.strip()):
        findings.append(Finding(lvl, "CALIBER_MISSING", rel, "caliber 缺失"))
    elif isinstance(caliber, str) and not _caliber_registered(caliber, calibers):
        findings.append(Finding(lvl, "CALIBER_UNREGISTERED", rel,
                                f"caliber={caliber} 未在 caliber_registry 登记且无 custom_ 前缀"))

    # F5：verdict 形态。verdict 可为 {code, note} 对象或纯字符串（legacy 常见）
    verdict = rj.get("verdict")
    code = verdict.get("code") if isinstance(verdict, dict) else verdict
    if code is None:
        findings.append(Finding(lvl, "VERDICT_MISSING", rel, "缺 verdict（legacy run 无结论字段只报 warn）"))
    elif not isinstance(code, str) or not _VERDICT_PATTERN.match(code):
        findings.append(Finding("fatal" if is_v1 else "warn", "VERDICT_FORMAT", rel,
                                f"verdict.code={code!r} 不匹配 UPPER_SNAKE 模式"))
    else:
        roots = set(spec["verdict_vocabulary"]["known_roots"])
        unknown = [t for t in code.split("_") if t and t not in roots]
        if unknown:
            findings.append(Finding("warn", "VERDICT_UNKNOWN_ROOT", rel,
                                    f"词根 {unknown} 不在 verdict_vocabulary，需扩充契约或改名"))

    # W1：legacy run 缺 verdict / headline（verdict 已在上面覆盖）
    if not is_v1 and not rj.get("headline"):
        findings.append(Finding("warn", "HEADLINE_MISSING", rel, "legacy run 缺 headline"))

    # W2：README.md 与 07_deliver 同时缺失
    has_readme = (run_dir / "README.md").is_file()
    deliver = run_dir / "07_deliver"
    has_deliver_md = deliver.is_dir() and any(deliver.glob("*.md"))
    if not has_readme and not has_deliver_md:
        findings.append(Finding("warn", "CONCLUSION_DOC_MISSING", rel, "README.md 与 07_deliver/*.md 同时缺失"))

    # W3：known_limitations 为空
    kl = rj.get("known_limitations")
    if is_v1 and (kl is None or kl == []):
        findings.append(Finding("warn", "LIMITATIONS_EMPTY", rel, "known_limitations 为空，写不出局限=没做稳健性"))

    # W4：顶层子目录不在语义槽位内
    shape_legacy = [p.name for p in run_dir.iterdir() if p.is_dir() and p.name not in _SEGMENTS]
    if shape_legacy:
        findings.append(Finding("warn", "SHAPE_LEGACY", rel, "目录不在 segment_vocabulary: " + ", ".join(sorted(shape_legacy))))

    return findings


def check_all_runs(research_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for run_dir in discover_runs(research_root):
        findings += check_run(run_dir, research_root)
    return findings


def render_run_check(findings: list[Finding], n_runs: int) -> str:
    from .registry import render_findings

    if not findings:
        return f"run check: {n_runs} 个 run 全部通过，无 fatal / warn。"
    body = render_findings(findings)
    head, _, rest = body.partition("\n")
    return f"{head}（{n_runs} 个 run）\n{rest}"


def default_research_root() -> Path:
    import os

    env = os.environ.get("QRG_RESEARCH_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / "research").resolve()
