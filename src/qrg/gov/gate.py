"""门禁（E1）：把审计报告汇总成唯一的发布判定。

设计约束：
- 纯确定性：不读时间、不用随机数、不调用模型，输入相同则输出相同；
- 判定优先级：blocked > warn > pass；
- 四件套清单缺失时，最少也要给出 warn，并在 warnings 中留痕；
- gate.json 以稳定 JSON（sort_keys、固定缩进、无时间戳）原子写出。

注意：``decide`` 的签名不包含 ``out_dir``，因此这里只能校验 manifest 的键覆盖；
hash 校验由持有产物目录的调用方用 ``qrg.clean.artifacts.verify_manifest`` 完成。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..api import AuditReport, GateBlocked, RuleHit, Severity

GATE_JSON = "gate.json"

#: 发布四件套；manifest 必须覆盖这些键。
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "data_quality_report.json",
    "clean_report.json",
    "cleaning_sensitivity.json",
    "trading_flags.parquet",
)

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_BLOCKED = "blocked"


@dataclass(frozen=True)
class Decision:
    """一次门禁判定；可直接序列化为 gate.json。"""

    status: str
    max_severity: Severity
    blocking_rules: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest_ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "max_severity": self.max_severity.name,
            "blocking_rules": list(self.blocking_rules),
            "warnings": list(self.warnings),
            "manifest_ok": self.manifest_ok,
        }

    def raise_if_blocked(self) -> None:
        """方便调用方在发布前统一拦截；语义与 ``AuditReport.raise_if_blocked`` 一致。"""
        if self.status == STATUS_BLOCKED:
            raise GateBlocked(f"blocked by: {', '.join(self.blocking_rules)}")


def _iter_hits(reports: Sequence[AuditReport]):
    for report in reports:
        yield from report.hits


def _collect_warnings(hits: Sequence[RuleHit]) -> list[str]:
    """WARN/L2 留痕；BLOCKED 由 blocking_rules 单独承载。"""
    warnings = {
        f"{hit.rule_id}: {hit.message}"
        for hit in hits
        if Severity.WARN <= hit.severity < Severity.BLOCKED
    }
    return sorted(warnings)


def _check_manifest(manifest: Mapping[str, str] | None) -> tuple[bool, tuple[str, ...]]:
    """只校验键覆盖；没有 manifest 时视为未要求校验。"""
    if manifest is None:
        return True, ()
    missing = tuple(sorted(set(REQUIRED_ARTIFACTS) - set(manifest)))
    return not missing, missing


def decide(
    reports: Sequence[AuditReport],
    manifest: Mapping[str, str] | None = None,
) -> Decision:
    """汇总全部审计报告，返回唯一、确定性的门禁判定。"""
    reports = tuple(reports)
    hits = tuple(_iter_hits(reports))

    max_severity = max((report.max_severity for report in reports), default=Severity.INFO)

    blocked_hits = tuple(hit for hit in hits if hit.severity >= Severity.BLOCKED)
    blocking_rules = tuple(sorted({hit.rule_id for hit in blocked_hits}))

    warnings = _collect_warnings(hits)
    manifest_ok, missing = _check_manifest(manifest)
    if not manifest_ok:
        warnings.append(f"manifest: missing required artifacts: {', '.join(missing)}")

    if blocked_hits:
        status = STATUS_BLOCKED
    elif max_severity >= Severity.WARN or not manifest_ok:
        status = STATUS_WARN
    else:
        status = STATUS_PASS

    # 清单缺件本身没有对应 RuleHit，但判定已至少是 warn；让 max_severity 与 status 对齐。
    if not manifest_ok and max_severity < Severity.WARN:
        max_severity = Severity.WARN

    return Decision(
        status=status,
        max_severity=max_severity,
        blocking_rules=blocking_rules,
        warnings=tuple(sorted(set(warnings))),
        manifest_ok=manifest_ok,
    )


def write_decision(out_dir: str | Path, decision: Decision) -> Path:
    """确定性写出 ``gate.json`` 并返回其路径；同一 Decision 的字节内容完全一致。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / GATE_JSON
    payload = json.dumps(
        decision.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    return path
