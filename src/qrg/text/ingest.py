"""G1 采集快照：把已经抓到的原文钉成可校验的证据。

设计原则：**agent 负责发现，库负责证据**。本模块不联网、不抓取，
只接收调用方已经拿到的内容，按内容哈希原样落盘，并维护 append-only 索引。

存储布局::

    <root>/raw/<content_sha256>.bin   # 原始字节（不转码、不压缩、不改写）
    <root>/index.jsonl                # 每行一条证据记录（sort_keys、UTF-8）

关键约束
--------
* ``snapshot_id`` 只由 ``(url, content_sha256, source_id)`` 决定，不含时间，
  因此同样内容可以跨次去重；同一 ``snapshot_id`` 重复 ``put`` 幂等，
  不会重复追加索引行。
* 索引损坏（空行、非 JSON、非对象、缺字段、字段类型错误）一律抛
  :class:`qrg.api.SpecError` 并带行号，绝不静默跳过。
* 所有文件写入都走临时文件 + :func:`os.replace` 原子替换。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api import SpecError, content_hash

__all__ = ["SnapshotRecord", "SnapshotStore", "INDEX_NAME", "RAW_DIRNAME"]

#: 索引文件名（相对 root）。
INDEX_NAME = "index.jsonl"

#: 原文目录名（相对 root）。
RAW_DIRNAME = "raw"

_REQUIRED_FIELDS: tuple[str, ...] = (
    "snapshot_id",
    "url",
    "source_id",
    "fetched_at",
    "status",
    "content_sha256",
    "path",
    "content_type",
)

_STRING_FIELDS: tuple[str, ...] = (
    "snapshot_id",
    "url",
    "source_id",
    "fetched_at",
    "content_sha256",
    "path",
    "content_type",
)


@dataclass(frozen=True)
class SnapshotRecord:
    """一条采集证据；由 :meth:`SnapshotStore.put` 产生并可回读。"""

    snapshot_id: str
    url: str
    source_id: str
    fetched_at: str
    status: int
    content_sha256: str
    path: str
    content_type: str = "text/html"

    def to_dict(self) -> dict[str, Any]:
        """投影为索引行字段；键集合固定，便于确定性序列化。"""
        return {
            "snapshot_id": self.snapshot_id,
            "url": self.url,
            "source_id": self.source_id,
            "fetched_at": self.fetched_at,
            "status": self.status,
            "content_sha256": self.content_sha256,
            "path": self.path,
            "content_type": self.content_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SnapshotRecord":
        """从索引行恢复记录；缺键/类型错误抛 :class:`SpecError`。"""
        missing = [key for key in _REQUIRED_FIELDS if key not in payload]
        if missing:
            raise SpecError(f"missing fields {', '.join(missing)}")
        for key in _STRING_FIELDS:
            if not isinstance(payload[key], str):
                raise SpecError(f"field {key!r} must be a string")
        status = payload["status"]
        if not isinstance(status, int) or isinstance(status, bool):
            raise SpecError("field 'status' must be an int")
        return cls(
            snapshot_id=payload["snapshot_id"],
            url=payload["url"],
            source_id=payload["source_id"],
            fetched_at=payload["fetched_at"],
            status=status,
            content_sha256=payload["content_sha256"],
            path=payload["path"],
            content_type=payload["content_type"],
        )


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{name} must be a non-empty string")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件写完后 :func:`os.replace`，避免读者看到半成品。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class SnapshotStore:
    """基于内容哈希的采集快照仓库；纯离线、确定性、可校验。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------ 只读属性

    @property
    def root(self) -> Path:
        """仓库根目录。"""
        return self._root

    @property
    def index_path(self) -> Path:
        """索引文件路径（尚未创建时也存在此路径）。"""
        return self._root / INDEX_NAME

    @property
    def raw_dir(self) -> Path:
        """原文目录路径（尚未创建时也存在此路径）。"""
        return self._root / RAW_DIRNAME

    # ------------------------------------------------------------ 写入

    def put(
        self,
        url: str,
        content: bytes | str,
        *,
        fetched_at: str,
        source_id: str,
        status: int = 200,
        content_type: str = "text/html",
    ) -> SnapshotRecord:
        """把一份原文钉成证据；同一 ``snapshot_id`` 重复调用幂等。

        ``str`` 内容按 UTF-8 编码为字节后原样保存；``bytes`` 不做任何转码。
        返回索引中已存在的记录（重复 ``put`` 时以首次入库记录为准）。
        """
        url = _require_text(url, "url")
        source_id = _require_text(source_id, "source_id")
        fetched_at = _require_text(fetched_at, "fetched_at")
        content_type = _require_text(content_type, "content_type")
        if not isinstance(status, int) or isinstance(status, bool):
            raise SpecError("status must be an int")

        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            payload = bytes(content)
        else:
            raise SpecError(f"content must be bytes or str, got {type(content).__name__}")

        content_sha256 = _sha256_bytes(payload)
        snapshot_id = content_hash(
            {
                "url": url,
                "content_sha256": content_sha256,
                "source_id": source_id,
            }
        )
        rel_path = f"{RAW_DIRNAME}/{content_sha256.removeprefix('sha256:')}.bin"
        record = SnapshotRecord(
            snapshot_id=snapshot_id,
            url=url,
            source_id=source_id,
            fetched_at=fetched_at,
            status=status,
            content_sha256=content_sha256,
            path=rel_path,
            content_type=content_type,
        )

        # 先读索引：索引已损坏时不应产生任何写入副作用。
        existing_records = self._read_records()
        for existing in existing_records:
            if existing.snapshot_id == snapshot_id:
                # 幂等：不追加索引；若原文缺失/被篡改，用同内容修复。
                self._ensure_raw(rel_path, payload)
                return existing

        self._ensure_raw(rel_path, payload)
        self._append(record)
        return record

    # ------------------------------------------------------------ 读取

    def get(self, snapshot_id: str) -> SnapshotRecord:
        """按 ``snapshot_id`` 取记录；索引中没有则抛 :class:`SpecError`。"""
        _require_text(snapshot_id, "snapshot_id")
        for record in self._read_records():
            if record.snapshot_id == snapshot_id:
                return record
        raise SpecError(f"snapshot not found: {snapshot_id}")

    def read(self, snapshot_id: str) -> bytes:
        """读取原文原始字节；文件缺失抛 :class:`SpecError`。"""
        record = self.get(snapshot_id)
        target = self._resolve(record.path)
        if not target.is_file():
            raise SpecError(
                f"snapshot raw file missing for {snapshot_id}: {record.path}"
            )
        try:
            return target.read_bytes()
        except OSError as exc:
            raise SpecError(f"cannot read snapshot raw file {record.path}: {exc}") from exc

    def list(self) -> list[SnapshotRecord]:
        """按写入顺序返回全部记录；索引损坏抛 :class:`SpecError`。"""
        return self._read_records()

    def verify(self) -> dict[str, bool]:
        """校验每条证据的原文是否存在且内容哈希匹配。"""
        result: dict[str, bool] = {}
        for record in self._read_records():
            try:
                target = self._resolve(record.path)
                ok = target.is_file() and _sha256_file(target) == record.content_sha256
            except (OSError, SpecError):
                ok = False
            # 理论上 snapshot_id 不应重复；若被篡改出重复行，任一失败即 False。
            result[record.snapshot_id] = result.get(record.snapshot_id, True) and ok
        return result

    # ------------------------------------------------------------ 内部实现

    def _ensure_raw(self, rel_path: str, payload: bytes) -> None:
        """确保原文文件存在且内容一致；一致则跳过，避免无谓重写。"""
        target = self._resolve(rel_path)
        if target.is_file():
            try:
                if _sha256_file(target) == _sha256_bytes(payload):
                    return
            except OSError:
                pass
        _atomic_write(target, payload)

    def _append(self, record: SnapshotRecord) -> None:
        """逻辑追加一行；物理上用临时文件 + os.replace 原子替换整个索引。"""
        line = (
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        index = self._root / INDEX_NAME
        existing = b""
        if index.exists():
            try:
                existing = index.read_bytes()
            except OSError as exc:
                raise SpecError(f"cannot read snapshot index {index}: {exc}") from exc
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        _atomic_write(index, existing + line)

    def _read_records(self) -> list[SnapshotRecord]:
        index = self._root / INDEX_NAME
        if not index.exists():
            return []
        try:
            data = index.read_bytes()
        except OSError as exc:
            raise SpecError(f"cannot read snapshot index {index}: {exc}") from exc

        records: list[SnapshotRecord] = []
        for lineno, raw in enumerate(data.splitlines(), 1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: not valid UTF-8 ({exc})"
                ) from exc
            if not text.strip():
                raise SpecError(f"corrupt snapshot index line {lineno}: empty line")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: expected a JSON object"
                )
            missing = [key for key in _REQUIRED_FIELDS if key not in payload]
            if missing:
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: "
                    f"missing fields {', '.join(missing)}"
                )
            for key in _STRING_FIELDS:
                if not isinstance(payload[key], str):
                    raise SpecError(
                        f"corrupt snapshot index line {lineno}: "
                        f"field {key!r} must be a string"
                    )
            status = payload["status"]
            if not isinstance(status, int) or isinstance(status, bool):
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: field 'status' must be an int"
                )
            if not payload["path"]:
                raise SpecError(
                    f"corrupt snapshot index line {lineno}: field 'path' is empty"
                )
            records.append(
                SnapshotRecord(
                    snapshot_id=payload["snapshot_id"],
                    url=payload["url"],
                    source_id=payload["source_id"],
                    fetched_at=payload["fetched_at"],
                    status=status,
                    content_sha256=payload["content_sha256"],
                    path=payload["path"],
                    content_type=payload["content_type"],
                )
            )
        return records

    def _resolve(self, rel_path: str) -> Path:
        """把相对路径安全地解析到 root 内，拒绝绝对路径与 ``..``。"""
        if not isinstance(rel_path, str) or not rel_path:
            raise SpecError(f"invalid snapshot path: {rel_path!r}")
        relative = Path(rel_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SpecError(f"invalid snapshot path: {rel_path!r}")
        root = self._root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SpecError(f"snapshot path escapes root: {rel_path!r}") from exc
        return target
