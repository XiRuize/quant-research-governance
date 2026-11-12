from __future__ import annotations

import shutil
import uuid
from datetime import date

from qrg.text.event_pit import Event, audit, pit_frame
from qrg.text.ingest import SnapshotStore
from qrg.text.sources import Evidence, SourceRegistry, arbitrate

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
BASE = __import__("pathlib").Path(__file__).resolve().parent / ".tmp" / "text_pipeline"

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)
HTML_GOV = b"<html><body>CSRC notice: DUV export review</body></html>"
HTML_WB = b"<html><body>weibo: rumor about DUV</body></html>"


def _dir():
    d = BASE / f"case-{uuid.uuid4().hex}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _evidence(record, registry: SourceRegistry) -> Evidence:
    return Evidence(
        evidence_id=record.snapshot_id,
        url=record.url,
        source_id=record.source_id,
        tier=registry.tier_for(url=record.url, source_id=record.source_id),
        published_at=record.fetched_at,
    )


def test_snapshot_to_event_pipeline() -> None:
    d = _dir()
    store = SnapshotStore(d / "evidence")
    registry = SourceRegistry.load(ROOT / "rules" / "sources.cn.json")

    gov = store.put(
        "https://www.csrc.gov.cn/notice/1",
        HTML_GOV,
        fetched_at="2024-01-02T15:40:00",
        source_id="csrc",
    )
    weibo = store.put(
        "https://weibo.com/u/1/status/2",
        HTML_WB,
        fetched_at="2024-01-02T15:45:00",
        source_id="weibo",
    )

    chosen, reason = arbitrate([_evidence(weibo, registry), _evidence(gov, registry)])
    assert chosen.source_id == "csrc"
    assert reason.startswith("tier:")

    event = Event(
        event_id="e-duv",
        actor="congress",
        action="letter",
        object="DUV export to China",
        stage="proposed",
        first_published_at="2024-01-02T16:00:00",
        source_tier=chosen.tier,
        evidence_ids=(chosen.evidence_id,),
    )
    frame = pit_frame([event], [D1, D2])
    assert frame.loc[frame["trade_date"] == frame["trade_date"].min(), "visible"].tolist() == [False]
    assert frame["visible"].tolist() == [False, True]
    assert audit([event]).ok


def test_snapshot_tamper_is_detected() -> None:
    d = _dir()
    store = SnapshotStore(d / "evidence")
    record = store.put("https://www.sse.com.cn/a", b"x", fetched_at="2024-01-02T10:00:00", source_id="sse")
    assert all(store.verify().values())
    (store.root / record.path).write_bytes(b"tampered")
    assert store.verify()[record.snapshot_id] is False


def test_arbitration_is_deterministic() -> None:
    registry = SourceRegistry.load(ROOT / "rules" / "sources.cn.json")
    a = Evidence("sha256:a", "https://www.csrc.gov.cn/x", "csrc", 1, "2024-01-02T10:00:00")
    b = Evidence("sha256:b", "https://weibo.com/x", "weibo", 4, "2024-01-02T09:00:00")
    assert arbitrate([a, b])[0] == a
    assert arbitrate([b, a])[0] == a
