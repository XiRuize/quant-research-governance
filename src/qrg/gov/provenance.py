"""provenance（E3）：把一次运行钉到 spec 版本、规则哈希、配置指纹与产物清单上。

原则：没有时间戳的确定性记录 —— 同样的输入必须得到逐字节相同的 provenance.json。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..api import SpecError, content_hash


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def hash_tree(root: str | Path, pattern: str = "*.json") -> dict[str, str]:
    base = Path(root)
    if not base.exists():
        raise SpecError(f"rules dir not found: {base}")
    return {str(p.relative_to(base)).replace("\\", "/"): sha256_file(p) for p in sorted(base.rglob(pattern))}


@dataclass(frozen=True)
class ProvenanceRecord:
    run_id: str
    spec_version: str
    rule_hashes: Mapping[str, str] = field(default_factory=dict)
    config_fingerprint: str = ""
    artifacts: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "spec_version": self.spec_version,
            "rule_hashes": dict(sorted(self.rule_hashes.items())),
            "config_fingerprint": self.config_fingerprint,
            "artifacts": dict(sorted(self.artifacts.items())),
        }


def collect(
    run_id: str,
    spec_version: str,
    rules_dir: str | Path,
    manifest: Mapping[str, str],
    config: Mapping[str, Any] | None = None,
) -> ProvenanceRecord:
    if not run_id:
        raise SpecError("run_id must be non-empty")
    artifacts = {k: v for k, v in manifest.items() if k != "spec_version"}
    if not artifacts:
        raise SpecError("manifest is empty; refusing to write provenance")
    return ProvenanceRecord(
        run_id=run_id,
        spec_version=spec_version,
        rule_hashes=hash_tree(rules_dir),
        config_fingerprint=content_hash(dict(config or {})),
        artifacts=artifacts,
    )


def write_record(out_dir: str | Path, record: ProvenanceRecord) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "provenance.json"
    payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def load_record(out_dir: str | Path) -> ProvenanceRecord:
    path = Path(out_dir) / "provenance.json"
    if not path.exists():
        raise SpecError(f"missing provenance: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ProvenanceRecord(
        run_id=payload["run_id"],
        spec_version=payload["spec_version"],
        rule_hashes=payload.get("rule_hashes", {}),
        config_fingerprint=payload.get("config_fingerprint", ""),
        artifacts=payload.get("artifacts", {}),
    )


def trace_artifact(out_dir: str | Path, artifact: str) -> dict[str, Any]:
    """产物 -> 记录 -> 规则哈希，回答“这份报告凭什么来的”。"""
    record = load_record(out_dir)
    if artifact not in record.artifacts:
        raise SpecError(f"artifact {artifact!r} not in provenance")
    return {
        "artifact": artifact,
        "hash": record.artifacts[artifact],
        "run_id": record.run_id,
        "spec_version": record.spec_version,
        "rule_hashes": dict(record.rule_hashes),
    }
