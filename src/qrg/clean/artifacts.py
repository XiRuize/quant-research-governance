"""四件套产物（B5）：确定性写出 + 内容哈希清单。

要求：
- 同一输入两次运行的产物 hash 必须一致（不写时间戳、不依赖行序）；
- 任何一个产物都必须能通过 manifest 校验；
- 文件先写临时名再原子替换，避免半成品。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..api import AuditReport, PanelSpec, SpecError

QUALITY_REPORT = "data_quality_report.json"
CLEAN_REPORT = "clean_report.json"
TRADING_FLAGS = "trading_flags.parquet"
SENSITIVITY = "cleaning_sensitivity.json"
MANIFEST = "manifest.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _dump_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def write_artifacts(
    out_dir: str | Path,
    spec: PanelSpec,
    reports: Sequence[AuditReport] = (),
    flags: pd.DataFrame | None = None,
    sensitivity: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """写出四件套 + manifest，返回 manifest。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    quality = {
        "spec_version": spec.spec_version,
        "timezone": spec.timezone,
        "reports": [r.to_dict() for r in reports],
        "ok": all(r.ok for r in reports),
    }
    clean = {
        "spec_version": spec.spec_version,
        "base_date": spec.base_date.isoformat(),
        "stages": [r.stage for r in reports],
        "rows": {r.stage: int(r.meta.get("rows", 0)) for r in reports},
    }
    sens = dict(sensitivity or {})
    sens.setdefault("spec_version", spec.spec_version)
    sens.setdefault(
        "thresholds",
        {
            "violation_rate_max": spec.violation_rate_max,
            "violation_rate_fatal": spec.violation_rate_fatal,
        },
    )

    _atomic_write(out / QUALITY_REPORT, _dump_json(quality))
    _atomic_write(out / CLEAN_REPORT, _dump_json(clean))
    _atomic_write(out / SENSITIVITY, _dump_json(sens))

    if flags is None:
        flags = pd.DataFrame(columns=["trade_date", "code"])
    flags_out = flags.copy()
    if "trade_date" in flags_out.columns:
        flags_out["trade_date"] = pd.to_datetime(flags_out["trade_date"])
        flags_out = flags_out.sort_values(["trade_date", "code"]).reset_index(drop=True)
    flags_path = out / TRADING_FLAGS
    tmp_flags = flags_path.with_suffix(".parquet.tmp")
    flags_out.to_parquet(tmp_flags, index=False)
    os.replace(tmp_flags, flags_path)

    manifest = {
        QUALITY_REPORT: _sha256_file(out / QUALITY_REPORT),
        CLEAN_REPORT: _sha256_file(out / CLEAN_REPORT),
        SENSITIVITY: _sha256_file(out / SENSITIVITY),
        TRADING_FLAGS: _sha256_file(flags_path),
        "spec_version": spec.spec_version,
    }
    _atomic_write(out / MANIFEST, _dump_json(manifest))
    return manifest


def verify_manifest(out_dir: str | Path) -> bool:
    """校验产物是否与 manifest 一致；缺失或不一致返回 False。"""
    out = Path(out_dir)
    manifest_path = out / MANIFEST
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        if name == "spec_version":
            continue
        path = out / name
        if not path.exists():
            return False
        if _sha256_file(path) != digest:
            return False
    return True


def load_quality(out_dir: str | Path) -> dict[str, Any]:
    path = Path(out_dir) / QUALITY_REPORT
    if not path.exists():
        raise SpecError(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))
