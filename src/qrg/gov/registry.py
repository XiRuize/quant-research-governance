"""E2 试验登记：append-only 试验台账与阈值快照。

设计约束
--------
* **只追加**：每次 ``register`` 向 ``<path>/trials.jsonl`` 追加一行；不重写历史。
* **指纹**：``fingerprint`` 只由 ``config`` 决定，复用 :func:`qrg.api.content_hash`，
  因此键顺序无关；同一 config 重复登记不报错，用 :meth:`TrialRegistry.find_duplicate`
  找出首条记录，让「静默重复试验」可被发现。
* **时间**：``created_at`` 只来自注入的 ``clock``；测试必须注入固定 clock，
  使两次独立运行逐字节一致。
* **阈值快照**：``freeze`` 写 ``<path>/frozen/<name>.json``，确定性 JSON
  （``sort_keys``、无时间戳）；同名不同内容抛 :class:`~qrg.api.SpecError`，
  同名同内容幂等，禁止静默改口径。
* **不静默跳过**：台账中的空行、非 JSON 行、非对象行、缺字段行一律抛
  :class:`~qrg.api.SpecError`，并带行号。

存储布局::

    <path>/trials.jsonl          # 每行一条登记记录
    <path>/frozen/<name>.json    # 阈值快照

台账记录字段：``run_id``、``spec_version``、``fingerprint``、``config``、
``note``、``created_at``。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..api import SpecError, content_hash

__all__ = ["TrialRegistry", "LEDGER_NAME", "FROZEN_DIRNAME"]

#: 台账文件名（相对 registry 根目录）。
LEDGER_NAME = "trials.jsonl"

#: 阈值快照目录名（相对 registry 根目录）。
FROZEN_DIRNAME = "frozen"

_REQUIRED_FIELDS: tuple[str, ...] = (
    "run_id",
    "spec_version",
    "fingerprint",
    "config",
    "note",
    "created_at",
)


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_jsonable(value: Any) -> Any:
    """把值投影成 JSON 原生结构（键排序、非原生值按 ``str``）。

    这样写入台账的 ``config`` 与计算指纹时使用的结构完全一致，
    且跨进程/跨平台可重复。
    """
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return json.loads(text)


class TrialRegistry:
    """基于 JSONL 的试验登记簿；纯离线、无随机数、无系统时钟依赖。"""

    def __init__(self, path: str | Path, *, clock: Callable[[], str] | None = None) -> None:
        self._root = Path(path)
        self._ledger = self._root / LEDGER_NAME
        self._frozen_dir = self._root / FROZEN_DIRNAME
        self._clock = _default_clock if clock is None else clock
        if not callable(self._clock):
            raise TypeError("clock must be callable returning a str")

    # ------------------------------------------------------------ 只读属性

    @property
    def ledger_path(self) -> Path:
        """台账文件路径（尚未创建时也存在此路径）。"""
        return self._ledger

    @property
    def frozen_dir(self) -> Path:
        """阈值快照目录。"""
        return self._frozen_dir

    # ------------------------------------------------------------ 登记

    def register(
        self,
        *,
        spec_version: str,
        config: Mapping[str, Any],
        note: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """追加一条试验登记记录并返回它。

        ``run_id`` 省略时生成确定性 id（序号 + 指纹前缀），保证固定 clock 下
        两次独立目录的台账逐字节一致。
        """
        if not isinstance(spec_version, str) or not spec_version:
            raise SpecError("spec_version must be a non-empty string")
        if not isinstance(config, Mapping):
            raise SpecError("config must be a mapping")

        canonical = self._canonical_config(config)

        fingerprint = content_hash(canonical)

        created_at = self._clock()
        if not isinstance(created_at, str) or not created_at:
            raise SpecError("clock must return a non-empty ISO-8601 string")

        existing = self._read_records()
        if run_id is None:
            seq = len(existing) + 1
            digest = fingerprint.removeprefix("sha256:")
            run_id = f"run-{seq:06d}-{digest[:10]}"
        elif not isinstance(run_id, str) or not run_id:
            raise SpecError("run_id must be a non-empty string")

        record: dict[str, Any] = {
            "run_id": run_id,
            "spec_version": spec_version,
            "fingerprint": fingerprint,
            "config": canonical,
            "note": str(note),
            "created_at": created_at,
        }
        self._append(record)
        return record

    def list(self) -> list[dict[str, Any]]:
        """按写入顺序返回全部记录；损坏行抛 SpecError。"""
        return self._read_records()

    def find_duplicate(self, config: Mapping[str, Any]) -> dict[str, Any] | None:
        """返回与 ``config`` 指纹相同的第一条记录；没有则返回 ``None``。"""
        if not isinstance(config, Mapping):
            raise SpecError("config must be a mapping")
        fingerprint = content_hash(self._canonical_config(config))
        for record in self._read_records():
            if record["fingerprint"] == fingerprint:
                return record
        return None

    # ------------------------------------------------------------ 阈值快照

    def freeze(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """写入确定性阈值快照；同名同内容幂等，同名不同内容抛 SpecError。"""
        self._validate_snapshot_name(name)
        if not isinstance(payload, Mapping):
            raise SpecError("payload must be a mapping")

        snapshot = {"name": name, "payload": self._canonical_json(payload)}
        text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        target = self._frozen_dir / f"{name}.json"

        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SpecError(f"invalid frozen snapshot {target}: {exc}") from exc
            if existing != snapshot:
                raise SpecError(
                    f"frozen snapshot {name!r} already exists with different content; "
                    "refusing to overwrite silently"
                )
            return self._snapshot_result(snapshot, target)

        self._frozen_dir.mkdir(parents=True, exist_ok=True)
        # 以 UTF-8 字节写入，避免 Windows 换行转换破坏确定性。
        target.write_bytes(text.encode("utf-8"))
        return self._snapshot_result(snapshot, target)

    # ------------------------------------------------------------ 内部实现

    @staticmethod
    def _canonical_config(config: Mapping[str, Any]) -> Any:
        try:
            return _to_jsonable(dict(config))
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            raise SpecError(f"config is not JSON-serializable: {exc}") from exc

    @staticmethod
    def _canonical_json(payload: Mapping[str, Any]) -> Any:
        try:
            return _to_jsonable(dict(payload))
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            raise SpecError(f"payload is not JSON-serializable: {exc}") from exc

    @staticmethod
    def _snapshot_result(snapshot: Mapping[str, Any], target: Path) -> dict[str, Any]:
        return {
            "name": snapshot["name"],
            "payload": snapshot["payload"],
            "fingerprint": content_hash(snapshot["payload"]),
            "path": str(target),
        }

    @staticmethod
    def _validate_snapshot_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise SpecError("snapshot name must be a non-empty string")
        if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise SpecError(f"invalid snapshot name: {name!r}")

    def _append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(
            dict(record), ensure_ascii=False, sort_keys=True, default=str
        ) + "\n"
        self._root.mkdir(parents=True, exist_ok=True)
        with self._ledger.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._ledger.exists():
            return []
        try:
            text = self._ledger.read_text(encoding="utf-8")
        except OSError as exc:
            raise SpecError(f"cannot read registry ledger {self._ledger}: {exc}") from exc

        records: list[dict[str, Any]] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            if not raw.strip():
                raise SpecError(f"corrupt registry line {lineno}: empty line")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SpecError(f"corrupt registry line {lineno}: {exc}") from exc
            if not isinstance(record, dict):
                raise SpecError(f"corrupt registry line {lineno}: expected a JSON object")
            missing = [key for key in _REQUIRED_FIELDS if key not in record]
            if missing:
                raise SpecError(
                    f"corrupt registry line {lineno}: missing fields {', '.join(missing)}"
                )
            records.append(record)
        return records
