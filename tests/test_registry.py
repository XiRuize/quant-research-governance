"""E2 试验登记测试：确定性、可发现重复、损坏行与阈值冻结。"""
from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from qrg.api import SpecError, content_hash
from qrg.gov.registry import LEDGER_NAME, TrialRegistry

FIXED_CLOCK_TEXT = "2024-01-02T03:04:05+00:00"
TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """仓库内临时目录（沙箱不允许写系统 tmp，同 conftest 的约定）。"""
    d = TMP_ROOT / "registry"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def fixed_clock() -> str:
    return FIXED_CLOCK_TEXT


def make_registry(root: Path) -> TrialRegistry:
    return TrialRegistry(root, clock=fixed_clock)


def test_register_list_preserves_write_order(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)

    first = registry.register(
        spec_version="spec-1",
        config={"window": 20, "threshold": 0.05},
        note="first",
        run_id="run-a",
    )
    second = registry.register(
        spec_version="spec-1",
        config={"window": 60, "threshold": 0.05},
        note="second",
        run_id="run-b",
    )

    records = registry.list()
    assert [item["run_id"] for item in records] == ["run-a", "run-b"]
    assert records == [first, second]
    assert records[0]["created_at"] == FIXED_CLOCK_TEXT
    assert records[0]["spec_version"] == "spec-1"
    assert records[0]["note"] == "first"

    # 已存在台账时是追加，而不是覆盖。
    assert registry.ledger_path == tmp_path / LEDGER_NAME
    lines = registry.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "run-a"


def test_repeated_config_register_then_find_duplicate_returns_first(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    original = registry.register(
        spec_version="spec-1",
        config={"window": 20, "threshold": 0.05},
        run_id="run-original",
    )
    registry.register(
        spec_version="spec-1",
        config={"window": 60, "threshold": 0.05},
        run_id="run-other",
    )

    # 同一 config 重复登记不报错。
    repeated = registry.register(
        spec_version="spec-2",
        config={"threshold": 0.05, "window": 20},  # 键顺序不同，指纹相同
        run_id="run-repeated",
    )

    hit = registry.find_duplicate({"threshold": 0.05, "window": 20})
    assert hit is not None
    assert hit["run_id"] == "run-original"
    assert hit == original
    assert repeated["fingerprint"] == original["fingerprint"]
    assert registry.find_duplicate({"window": 21}) is None


def test_fixed_clock_is_byte_deterministic_across_directories(tmp_path: Path) -> None:
    left = make_registry(tmp_path / "left")
    right = make_registry(tmp_path / "right")

    for registry in (left, right):
        registry.register(
            spec_version="spec-1",
            config={"window": 20, "threshold": 0.05},
            note="same",
        )
        registry.register(
            spec_version="spec-1",
            config={"window": 60, "threshold": 0.10},
            note="same",
        )
        registry.freeze("thresholds", {"close": 0.10, "open": 0.05})

    left_ledger = left.ledger_path.read_bytes()
    right_ledger = right.ledger_path.read_bytes()
    assert left_ledger == right_ledger

    # run_id 也必须由输入决定，而不是 uuid/时间戳。
    records = left.list()
    assert records[0]["run_id"] == right.list()[0]["run_id"]

    left_snapshot = (left.frozen_dir / "thresholds.json").read_bytes()
    right_snapshot = (right.frozen_dir / "thresholds.json").read_bytes()
    assert left_snapshot == right_snapshot
    assert b"2024" not in left_snapshot  # 快照无时间戳


def test_corrupt_jsonl_line_raises_spec_error(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.register(
        spec_version="spec-1",
        config={"window": 20},
        run_id="run-ok",
    )
    with registry.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")

    with pytest.raises(SpecError, match="line 2"):
        registry.list()
    with pytest.raises(SpecError):
        registry.find_duplicate({"window": 20})
    with pytest.raises(SpecError):
        registry.register(spec_version="spec-1", config={"window": 60})


def test_corrupt_jsonl_non_object_line_raises_spec_error(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.ledger_path.write_text("[1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(SpecError, match="JSON object"):
        registry.list()


def test_freeze_same_content_is_idempotent_and_diff_raises(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    payload = {"price_limit": {"main": 0.10, "star": 0.20}, "fee_bps": 3}
    first = registry.freeze("thresholds", payload)
    snapshot_path = tmp_path / "frozen" / "thresholds.json"
    first_bytes = snapshot_path.read_bytes()

    second = registry.freeze("thresholds", {"fee_bps": 3, "price_limit": {"star": 0.20, "main": 0.10}})
    assert second == first
    assert snapshot_path.read_bytes() == first_bytes

    with pytest.raises(SpecError, match="different content"):
        registry.freeze("thresholds", {"fee_bps": 5})
    assert snapshot_path.read_bytes() == first_bytes  # 失败时不得覆盖旧口径


def test_freeze_rejects_path_traversal_name(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    with pytest.raises(SpecError):
        registry.freeze("../escape", {"a": 1})


def test_fingerprint_matches_content_hash(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    config = {
        "window": 20,
        "nested": {"b": [1, 2, 3], "a": None},
        "threshold": 0.05,
        "label": "试验",
    }

    record = registry.register(spec_version="spec-1", config=config)

    assert record["fingerprint"] == content_hash(config)
    assert record["fingerprint"].startswith("sha256:")
    # 存进台账的 config 本身也能复算出同一指纹。
    assert content_hash(record["config"]) == record["fingerprint"]
    assert json.loads(registry.ledger_path.read_text(encoding="utf-8").splitlines()[0]) == record
