"""来源分级与仲裁（模块 G5）。

设计要点
--------
1. **分级口径可配置**：``rules/sources.cn.json`` 的 ``tiers`` 定义 1..5 级
   的可读标签；数值越小越可信。1 官方（交易所/证监会/政府/公司公告），
   2 通讯社（新华社/路透/彭博），3 财经媒体（财新/东财/雪球），
   4 社媒（微博/推特），5 传闻/未知。
2. **先精确、后后缀**：``tier_for`` 先按 ``source_id`` 精确映射（忽略大小写
   与首尾空白），未命中再按 URL 主机名的域名后缀匹配。后缀匹配要求主机名
   等于后缀或以 ``.<suffix>`` 结尾，并按最长后缀优先，避免
   ``notice.sse.com.cn`` 被宽泛的 ``com.cn`` 抢先命中。
3. **未知即最低可信**：source_id 与 URL 都无法识别时返回 5；``label(5)``
   带 ``UNKNOWN`` 标记，便于审计时区分「传闻」与「未知来源」。
4. **仲裁确定且可解释**：``arbitrate`` 先取 tier 最小者；同级取
   ``published_at`` 最早者；仍相同取 ``evidence_id`` 字典序最小者。理由码
   为 ``tier:<n>`` / ``tier_tie_earliest`` / ``tier_tie_by_id``，同一输入
   永远得到同一结果。
5. **失败显式**：规则文件缺键/类型错误/非法 tier，空候选列表，候选 tier
   越界或不合法 ``published_at``，一律抛 :class:`qrg.api.SpecError`。
6. **纯离线、stdlib-only**：只读本地 JSON，不联网、不调用 LLM。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..api import SpecError

#: 默认规则文件：仓库根/rules/sources.cn.json
#: （``src/qrg/text/sources.py`` 上溯 3 层即仓库根）。
_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "sources.cn.json"

#: 环境变量覆盖默认规则文件路径。
_RULES_ENV = "QRG_SOURCES_RULES"

#: 可信度等级范围；5 表示传闻/未知。
MIN_TIER = 1
MAX_TIER = 5
UNKNOWN_TIER = MAX_TIER

#: 允许的等级标签字段别名。
_TIER_LABEL_KEYS = ("label", "name", "title")


# ---------------------------------------------------------------- 通用校验


def _tier(value: Any, label: str) -> int:
    """校验并归一化可信度等级；非法值抛 ``SpecError``。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{label} must be an integer in 1..5, got {value!r}")
    if not MIN_TIER <= value <= MAX_TIER:
        raise SpecError(f"{label} must be in {MIN_TIER}..{MAX_TIER}, got {value}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{label} must be a non-empty string, got {value!r}")
    return value.strip()


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise SpecError(f"cannot read source rules {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"invalid JSON in source rules {path}: {exc}") from exc


def _parse_tiers(raw: Any) -> dict[int, str]:
    """解析 ``tiers``：支持 ``{"1": "官方"}`` 或 ``[{"tier": 1, ...}]``。"""

    if raw is None:
        raise SpecError("source rules must define 'tiers'")
    labels: dict[int, str] = {}
    if isinstance(raw, Mapping):
        entries = raw.items()
        for key, value in entries:
            try:
                tier = int(key)
            except (TypeError, ValueError) as exc:
                raise SpecError(f"invalid tier key in 'tiers': {key!r}") from exc
            tier = _tier(tier, f"tiers[{key!r}]")
            if isinstance(value, Mapping):
                value = next(
                    (value[k] for k in _TIER_LABEL_KEYS if k in value),
                    None,
                )
            if tier in labels:
                raise SpecError(f"duplicate tier in 'tiers': {tier}")
            labels[tier] = _text(value, f"tiers[{tier}] label")
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise SpecError(f"tiers #{index} must be an object")
            tier = _tier(item.get("tier"), f"tiers #{index}.tier")
            if tier in labels:
                raise SpecError(f"duplicate tier in 'tiers': {tier}")
            label = next((item[k] for k in _TIER_LABEL_KEYS if k in item), None)
            labels[tier] = _text(label, f"tiers #{index} label")
    else:
        raise SpecError("'tiers' must be an object or a list")

    missing = [tier for tier in range(MIN_TIER, MAX_TIER + 1) if tier not in labels]
    if missing:
        raise SpecError(f"'tiers' missing levels: {missing}")
    return labels


def _parse_source_rules(raw: Mapping[str, Any]) -> dict[str, int]:
    """解析 source_id 精确映射；兼容 ``source_rules`` / ``sources``。"""

    value = raw.get("source_rules", raw.get("sources", {}))
    mapping: dict[str, int] = {}
    if isinstance(value, Mapping):
        entries = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        entries = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise SpecError(f"source_rules #{index} must be an object")
            entries.append((item.get("source_id"), item.get("tier")))
    else:
        raise SpecError("'source_rules' must be an object or a list")

    for key, tier_value in entries:
        source_id = _text(key, "source_rules source_id").lower()
        tier = _tier(tier_value, f"source_rules[{source_id!r}].tier")
        if source_id in mapping:
            raise SpecError(f"duplicate source_id in 'source_rules': {source_id!r}")
        mapping[source_id] = tier
    return mapping


def _normalise_suffix(value: Any, label: str) -> str:
    suffix = _text(value, label).lower().lstrip(".").rstrip(".")
    if not suffix or "." not in suffix:
        raise SpecError(f"{label} must be a dotted domain suffix, got {value!r}")
    return suffix


@dataclass(frozen=True)
class _DomainRule:
    suffix: str
    tier: int
    source_id: str


def _parse_domain_rules(raw: Any) -> tuple[_DomainRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SpecError("'domain_rules' must be a list")

    rules: list[_DomainRule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise SpecError(f"domain_rules #{index} must be an object")
        suffix = _normalise_suffix(item.get("suffix"), f"domain_rules #{index}.suffix")
        tier = _tier(item.get("tier"), f"domain_rules[{suffix!r}].tier")
        source_id = item.get("source_id")
        source_id = "" if source_id is None else _text(source_id, f"domain_rules[{suffix!r}].source_id")
        if suffix in seen:
            raise SpecError(f"duplicate suffix in 'domain_rules': {suffix!r}")
        seen.add(suffix)
        rules.append(_DomainRule(suffix=suffix, tier=tier, source_id=source_id))

    # 最长后缀优先；等长时按字典序，保证匹配顺序确定。
    return tuple(sorted(rules, key=lambda rule: (-len(rule.suffix), rule.suffix)))


def _host_of(url: str | None) -> str | None:
    """提取 URL 主机名；无协议前缀也可解析，非法 URL 返回 ``None``。"""

    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    if "://" not in text:
        # 允许 ``www.sse.com.cn/...`` 这类无协议输入。
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.strip().lower().rstrip(".")


def _published_instant(value: Any, label: str) -> datetime:
    """把 ISO8601 ``published_at`` 归一化为 UTC；非法值抛 ``SpecError``。"""

    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{label}.published_at must be an ISO8601 string, got {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SpecError(f"{label}.published_at is not ISO8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- 注册表


class SourceRegistry:
    """来源分级注册表：``source_id`` 精确映射 + 域名后缀最长匹配。"""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise SpecError("source rules root must be a JSON object")
        self._tiers: dict[int, str] = _parse_tiers(payload.get("tiers"))
        self._source_rules: dict[str, int] = _parse_source_rules(payload)
        self._domain_rules: tuple[_DomainRule, ...] = _parse_domain_rules(
            payload.get("domain_rules")
        )

    # ------------------------------------------------------------ 构造

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SourceRegistry":
        """载入规则；省略路径时按环境变量、仓库默认路径、当前目录顺序查找。"""

        return cls(_read_json(cls._resolve_path(path)))

    @staticmethod
    def _resolve_path(path: str | Path | None) -> Path:
        if path is not None:
            resolved = Path(path)
            if not resolved.is_file():
                raise SpecError(f"source rules file not found: {resolved}")
            return resolved

        candidates: list[Path] = []
        env_path = os.environ.get(_RULES_ENV)
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(_DEFAULT_RULES_PATH)
        candidates.append(Path.cwd() / "rules" / "sources.cn.json")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise SpecError(f"source rules file not found; searched: {searched}")

    # ------------------------------------------------------------ 查询

    def tier_for(self, *, url: str | None = None, source_id: str | None = None) -> int:
        """返回来源可信度等级 1..5；无法识别返回 5（未知）。

        优先级：``source_id`` 精确映射 > URL 域名后缀最长匹配 > 未知。
        """

        if source_id is not None:
            key = _text(source_id, "source_id").lower()
            mapped = self._source_rules.get(key)
            if mapped is not None:
                return mapped

        host = _host_of(url)
        if host is not None:
            for rule in self._domain_rules:
                if host == rule.suffix or host.endswith("." + rule.suffix):
                    return rule.tier
        return UNKNOWN_TIER

    def label(self, tier: int) -> str:
        """返回等级的可读标签；非法等级抛 ``SpecError``。"""

        normalised = _tier(tier, "tier")
        return self._tiers[normalised]

    @property
    def source_rules(self) -> Mapping[str, int]:
        """source_id -> tier 的只读快照。"""

        return dict(self._source_rules)

    @property
    def domain_rules(self) -> tuple[tuple[str, int, str], ...]:
        """按匹配优先级排序的 ``(suffix, tier, source_id)`` 快照。"""

        return tuple((rule.suffix, rule.tier, rule.source_id) for rule in self._domain_rules)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"SourceRegistry(sources={len(self._source_rules)}, "
            f"domains={len(self._domain_rules)}, tiers={len(self._tiers)})"
        )


# ---------------------------------------------------------------- 证据与仲裁


@dataclass(frozen=True)
class Evidence:
    """一条待仲裁的消息证据；``evidence_id`` 对应 snapshot_id。"""

    evidence_id: str  # snapshot_id
    url: str
    source_id: str
    tier: int
    published_at: str  # ISO8601


def arbitrate(candidates: Sequence[Evidence]) -> tuple[Evidence, str]:
    """按可信度仲裁冲突消息，返回 ``(选中证据, 理由码)``。

    排序键依次为：``tier`` 升序、``published_at`` 升序、``evidence_id``
    字典序升序。理由码：

    - ``tier:<n>``：最高可信等级只有一条候选；
    - ``tier_tie_earliest``：最高等级并列，按最早发布时间分出唯一一条；
    - ``tier_tie_by_id``：最高等级与发布时间都并列，按 ``evidence_id`` 确定。
    """

    items = list(candidates)
    if not items:
        raise SpecError("cannot arbitrate an empty candidate list")

    parsed: list[tuple[datetime, int, Evidence]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Evidence):
            raise SpecError(
                f"candidates[{index}] must be Evidence, got {type(item).__name__}"
            )
        tier = _tier(item.tier, f"candidates[{index}].tier")
        if not isinstance(item.evidence_id, str) or not item.evidence_id.strip():
            raise SpecError(
                f"candidates[{index}].evidence_id must be a non-empty string, "
                f"got {item.evidence_id!r}"
            )
        published = _published_instant(
            item.published_at, f"candidates[{index}]"
        )
        parsed.append((published, tier, item))

    top_tier = min(tier for _, tier, _ in parsed)
    top = [(published, item) for published, tier, item in parsed if tier == top_tier]

    if len(top) == 1:
        return top[0][1], f"tier:{top_tier}"

    earliest_at = min(published for published, _ in top)
    earliest = [(published, item) for published, item in top if published == earliest_at]
    if len(earliest) == 1:
        return earliest[0][1], "tier_tie_earliest"

    winner = min(earliest, key=lambda pair: pair[1].evidence_id)[1]
    return winner, "tier_tie_by_id"
