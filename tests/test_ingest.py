"""G1 采集快照验收用例：往返、幂等、去重、篡改检测、损坏索引。"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from qrg.api import SpecError, content_hash
from qrg.text.ingest import INDEX_NAME, SnapshotRecord, SnapshotStore

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


@pytest.fixture
def root() -> Path:
    """仓库内临时根目录（避免依赖系统 tmp）。"""
    d = TMP_ROOT / f"ingest-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _index_lines(root: Path) -> list[str]:
    return (root / INDEX_NAME).read_text(encoding="utf-8").splitlines()


def test_put_get_list_read_roundtrip(root: Path) -> None:
    store = SnapshotStore(root)
    content = b"\xff\xfe<html>\x00raw bytes</html>"

    record = store.put(
        "https://example.com/a",
        content,
        fetched_at="2026-11-12T09:30:00+08:00",
        source_id="web",
    )

    assert isinstance(record, SnapshotRecord)
    assert record.url == "https://example.com/a"
    assert record.source_id == "web"
    assert record.fetched_at == "2026-11-12T09:30:00+08:00"
    assert record.status == 200
    assert record.content_type == "text/html"
    assert record.content_sha256.startswith("sha256:")
    assert record.path == f"raw/{record.content_sha256.removeprefix('sha256:')}.bin"
    assert (root / record.path).read_bytes() == content

    # snapshot_id 只由 url + content_sha256 + source_id 决定。
    assert record.snapshot_id == content_hash(
        {
            "url": record.url,
            "content_sha256": record.content_sha256,
            "source_id": record.source_id,
        }
    )

    assert store.get(record.snapshot_id) == record
    assert store.list() == [record]
    assert store.read(record.snapshot_id) == content
    assert store.verify() == {record.snapshot_id: True}


def test_duplicate_put_is_idempotent(root: Path) -> None:
    store = SnapshotStore(root)
    first = store.put(
        "https://example.com/a",
        b"same content",
        fetched_at="2026-11-12T09:30:00+08:00",
        source_id="web",
    )
    second = store.put(
        "https://example.com/a",
        b"same content",
        # 时间/状态不同也仍然是同一份证据，不应产生第二行。
        fetched_at="2026-11-13T10:00:00+08:00",
        source_id="web",
        status=304,
    )

    assert second == first
    assert store.list() == [first]
    assert len(_index_lines(root)) == 1
    assert json.loads(_index_lines(root)[0])["snapshot_id"] == first.snapshot_id
    assert store.read(first.snapshot_id) == b"same content"


def test_different_source_id_yields_different_snapshot_id(root: Path) -> None:
    store = SnapshotStore(root)
    content = b"same bytes, different provenance"
    a = store.put(
        "https://example.com/a", content, fetched_at="2026-11-12T09:30:00+08:00", source_id="wire-a"
    )
    b = store.put(
        "https://example.com/a", content, fetched_at="2026-11-12T09:30:00+08:00", source_id="wire-b"
    )

    assert a.snapshot_id != b.snapshot_id
    assert a.content_sha256 == b.content_sha256
    # 原文按内容哈希共享，不重复存储。
    assert a.path == b.path
    assert len(store.list()) == 2
    assert len(_index_lines(root)) == 2


def test_tampered_raw_file_fails_verify(root: Path) -> None:
    store = SnapshotStore(root)
    good = store.put(
        "https://example.com/good", b"good", fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )
    bad = store.put(
        "https://example.com/bad", b"original", fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )
    (root / bad.path).write_bytes(b"tampered")

    assert store.verify() == {good.snapshot_id: True, bad.snapshot_id: False}


def test_missing_file_read_raises_and_verify_false(root: Path) -> None:
    store = SnapshotStore(root)
    record = store.put(
        "https://example.com/a", b"payload", fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )
    (root / record.path).unlink()

    with pytest.raises(SpecError):
        store.read(record.snapshot_id)
    # 记录仍在索引中，verify 明确标 False，而不是静默丢失。
    assert store.get(record.snapshot_id) == record
    assert store.verify() == {record.snapshot_id: False}


@pytest.mark.parametrize(
    "bad_line",
    [
        b"{not valid json}\n",
        b"\n",
        b'{"snapshot_id": "sha256:only-id"}\n',
        b'{"snapshot_id": "sha256:x", "url": "u", "source_id": "s", "fetched_at": "t", "status": "200", "content_sha256": "sha256:y", "path": "raw/y.bin", "content_type": "text/html"}\n',
    ],
)
def test_corrupt_index_line_raises_with_line_number(root: Path, bad_line: bytes) -> None:
    store = SnapshotStore(root)
    store.put(
        "https://example.com/a", b"ok", fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )
    index = root / INDEX_NAME
    index.write_bytes(index.read_bytes() + bad_line)

    with pytest.raises(SpecError) as excinfo:
        store.list()
    assert "line 2" in str(excinfo.value)

    with pytest.raises(SpecError):
        store.get("sha256:whatever")
    with pytest.raises(SpecError):
        store.verify()


def test_corrupt_index_blocks_put_without_side_effect(root: Path) -> None:
    store = SnapshotStore(root)
    store.put(
        "https://example.com/a", b"first", fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )
    index = root / INDEX_NAME
    before = index.read_bytes()
    raw_before = sorted(p.name for p in (root / "raw").iterdir())
    index.write_bytes(before + b"garbage\n")

    with pytest.raises(SpecError):
        store.put(
            "https://example.com/b",
            b"second",
            fetched_at="2026-11-12T09:30:00+08:00",
            source_id="web",
        )
    # 损坏时不会产生新的原文文件，也不会改写索引。
    assert sorted(p.name for p in (root / "raw").iterdir()) == raw_before
    assert index.read_bytes() == before + b"garbage\n"


def test_two_independent_roots_are_byte_identical(root: Path) -> None:
    kwargs = dict(
        fetched_at="2026-11-12T09:30:00+08:00",
        source_id="web",
        status=200,
        content_type="text/html",
    )
    left = SnapshotStore(root / "left")
    right = SnapshotStore(root / "right")

    rec_left = left.put("https://example.com/a", b"deterministic", **kwargs)
    rec_right = right.put("https://example.com/a", b"deterministic", **kwargs)

    assert rec_left == rec_right
    assert (root / "left" / INDEX_NAME).read_bytes() == (
        root / "right" / INDEX_NAME
    ).read_bytes()
    assert (root / "left" / rec_left.path).read_bytes() == (
        root / "right" / rec_right.path
    ).read_bytes()


def test_str_content_is_utf8_and_roundtrips(root: Path) -> None:
    store = SnapshotStore(root)
    text = "中文标题 & <tag>"
    record = store.put(
        "https://example.com/zh", text, fetched_at="2026-11-12T09:30:00+08:00", source_id="web"
    )

    assert store.read(record.snapshot_id) == text.encode("utf-8")
    assert record.content_sha256.startswith("sha256:")


def test_get_unknown_snapshot_raises(root: Path) -> None:
    store = SnapshotStore(root)
    with pytest.raises(SpecError):
        store.get("sha256:" + "0" * 64)


def test_empty_store_list_is_empty(root: Path) -> None:
    store = SnapshotStore(root)
    assert store.list() == []
    assert store.verify() == {}
