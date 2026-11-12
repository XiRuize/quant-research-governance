"""G5 来源分级与仲裁测试：domain/source 映射、最长后缀、确定性仲裁。"""
from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from qrg.api import SpecError
from qrg.text.sources import Evidence, SourceRegistry, arbitrate

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


@pytest.fixture(scope="module")
def registry() -> SourceRegistry:
    return SourceRegistry.load()


@pytest.fixture
def tmp_rules_dir() -> Iterator[Path]:
    """仓库内临时目录（沙箱不允许写系统 tmp，同 conftest 约定）。"""
    root = TMP_ROOT / "sources"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def evidence(
    evidence_id: str,
    *,
    tier: int,
    published_at: str = "2024-01-02T09:00:00+08:00",
    url: str = "https://example.invalid/item",
    source_id: str = "test",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        url=url,
        source_id=source_id,
        tier=tier,
        published_at=published_at,
    )


# ---------------------------------------------------------------- 域名 / source 映射


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.csrc.gov.cn/csrc/c100028/common_list.shtml", 1),
        ("https://www.sse.com.cn/disclosure/listedinfo/announcement/", 1),
        ("https://www.reuters.com/markets/asia/", 2),
        ("https://finance.eastmoney.com/a/202401021234567890.html", 3),
        ("https://weibo.com/1234567890/abcdef", 4),
        ("https://example.invalid/news/1", 5),
    ],
)
def test_tier_for_url_maps_tiers(
    registry: SourceRegistry, url: str, expected: int
) -> None:
    assert registry.tier_for(url=url) == expected


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [
        ("gov", 1),
        ("csrc", 1),
        ("reuters", 2),
        ("xinhua", 2),
        ("eastmoney", 3),
        ("weibo", 4),
        ("rumor", 5),
        ("completely-unknown", 5),
    ],
)
def test_tier_for_source_id_maps_tiers(
    registry: SourceRegistry, source_id: str, expected: int
) -> None:
    assert registry.tier_for(source_id=source_id) == expected


def test_source_id_mapping_takes_precedence_over_domain(
    registry: SourceRegistry,
) -> None:
    assert (
        registry.tier_for(
            source_id="reuters", url="https://finance.eastmoney.com/a/1.html"
        )
        == 2
    )


def test_longest_domain_suffix_wins(registry: SourceRegistry) -> None:
    # sse.com.cn（官方）优先于宽泛的 com.cn（财经媒体）。
    assert registry.tier_for(url="https://notice.sse.com.cn/disclosure/1.pdf") == 1
    assert registry.tier_for(url="https://news.example.com.cn/a/1.html") == 3


def test_domain_suffix_requires_label_boundary(registry: SourceRegistry) -> None:
    # fakegov.cn 不是 gov.cn 的子域，不能误命中。
    assert registry.tier_for(url="https://fakegov.cn/news") == 5


def test_unknown_source_and_url_falls_back_to_tier_five(
    registry: SourceRegistry,
) -> None:
    assert registry.tier_for() == 5
    assert registry.tier_for(url="https://unknown.example/path") == 5


def test_label_and_invalid_label_tier(registry: SourceRegistry) -> None:
    assert registry.label(1) == "官方"
    assert registry.label(2) == "通讯社"
    assert registry.label(3) == "财经媒体"
    assert registry.label(4) == "社媒"
    assert "UNKNOWN" in registry.label(5)
    with pytest.raises(SpecError):
        registry.label(0)
    with pytest.raises(SpecError):
        registry.label(6)


# ---------------------------------------------------------------- 仲裁


def test_arbitrate_prefers_official_over_self_media() -> None:
    official = evidence(
        "snap-official",
        tier=1,
        published_at="2024-01-02T10:00:00+08:00",
        url="https://www.sse.com.cn/",
        source_id="sse",
    )
    self_media = evidence(
        "snap-weibo",
        tier=4,
        published_at="2024-01-02T08:00:00+08:00",
        url="https://weibo.com/1/2",
        source_id="weibo",
    )

    winner, reason = arbitrate([self_media, official])

    assert winner == official
    assert reason.startswith("tier:")
    assert reason == "tier:1"


def test_arbitrate_same_tier_uses_earliest_published_at() -> None:
    later = evidence("snap-a", tier=3, published_at="2024-01-02T10:00:00+08:00")
    earlier = evidence("snap-z", tier=3, published_at="2024-01-02T09:00:00+08:00")

    winner, reason = arbitrate([later, earlier])

    assert winner == earlier
    assert reason == "tier_tie_earliest"


def test_arbitrate_same_tier_and_time_uses_evidence_id() -> None:
    first_by_id = evidence("snap-a", tier=3, published_at="2024-01-02T09:00:00+08:00")
    second_by_id = evidence("snap-b", tier=3, published_at="2024-01-02T09:00:00+08:00")

    winner, reason = arbitrate([second_by_id, first_by_id])

    assert winner == first_by_id
    assert reason == "tier_tie_by_id"


def test_arbitrate_normalises_iso8601_timezones() -> None:
    # 两个字符串写法不同，但表示同一时刻；应继续按 evidence_id 决出。
    utc = evidence("snap-b", tier=2, published_at="2024-01-02T01:00:00Z")
    shanghai = evidence("snap-a", tier=2, published_at="2024-01-02T09:00:00+08:00")

    winner, reason = arbitrate([utc, shanghai])

    assert winner == shanghai
    assert reason == "tier_tie_by_id"


def test_arbitrate_is_deterministic() -> None:
    candidates = [
        evidence("snap-c", tier=3, published_at="2024-01-02T09:00:00+08:00"),
        evidence("snap-a", tier=3, published_at="2024-01-02T09:00:00+08:00"),
        evidence("snap-b", tier=2, published_at="2024-01-03T09:00:00+08:00"),
    ]

    first = arbitrate(candidates)
    second = arbitrate(candidates)

    assert first == second
    assert first[0].evidence_id == "snap-b"


def test_arbitrate_empty_candidates_raises_spec_error() -> None:
    with pytest.raises(SpecError):
        arbitrate([])


@pytest.mark.parametrize("tier", [0, 6, -1])
def test_arbitrate_rejects_out_of_range_tier(tier: int) -> None:
    with pytest.raises(SpecError):
        arbitrate([evidence("snap-a", tier=tier)])


def test_arbitrate_rejects_non_iso_published_at() -> None:
    with pytest.raises(SpecError):
        arbitrate([evidence("snap-a", tier=1, published_at="yesterday")])


# ---------------------------------------------------------------- 规则文件校验


def _write_rules(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, object]:
    return {
        "tiers": {"1": "官方", "2": "通讯社", "3": "财经媒体", "4": "社媒", "5": "传闻/未知 (UNKNOWN)"},
        "source_rules": {"sse": 1},
        "domain_rules": [
            {"suffix": "com.cn", "tier": 3, "source_id": "cn_media"},
            {"suffix": "sse.com.cn", "tier": 1, "source_id": "sse"},
        ],
    }


def test_load_custom_rules_applies_longest_suffix_regardless_of_order(
    tmp_rules_dir: Path,
) -> None:
    path = _write_rules(tmp_rules_dir / "sources.json", _valid_payload())
    custom = SourceRegistry.load(path)

    assert custom.tier_for(url="https://notice.sse.com.cn/a") == 1
    assert custom.tier_for(url="https://other.com.cn/a") == 3


def test_load_rejects_invalid_tier_in_domain_rules(tmp_rules_dir: Path) -> None:
    payload = _valid_payload()
    payload["domain_rules"] = [{"suffix": "gov.cn", "tier": 9}]
    path = _write_rules(tmp_rules_dir / "sources.json", payload)

    with pytest.raises(SpecError):
        SourceRegistry.load(path)


def test_load_rejects_missing_tiers(tmp_rules_dir: Path) -> None:
    payload = _valid_payload()
    del payload["tiers"]
    path = _write_rules(tmp_rules_dir / "sources.json", payload)

    with pytest.raises(SpecError):
        SourceRegistry.load(path)


def test_load_missing_file_raises_spec_error(tmp_rules_dir: Path) -> None:
    with pytest.raises(SpecError):
        SourceRegistry.load(tmp_rules_dir / "does-not-exist.json")
