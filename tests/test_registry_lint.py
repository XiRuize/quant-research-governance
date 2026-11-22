"""数据集登记与体检的验收用例。

这些用例锁住四件容易回退的事：

1. csv 不能被 parquet 读器探测（旧 catalog 的 D1 缺陷，5 个正常 CSV 被标 error）；
2. 主键重复 / 路径缺失 / 枚举越界必须 fatal，且退出码可判；
3. (code,date) 重复在价格表是 fatal、在事件表是 info —— 混为一谈会造出
   42 条假阳性（CSMAR 董事名单同一天天然多行）；
4. industry_name 跨期不变必须被识别为前视指纹（实际在 panel 上命中）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from qrg.lint import lint_dataset, summarize
from qrg.registry import (
    STATUS_ENUM,
    RegistryError,
    build_from_legacy,
    check,
    load_registry,
    probe,
)


# ---------------------------------------------------------------- 夹具


def _write_registry(root: Path, datasets: list[dict], sources: dict | None = None) -> Path:
    p = root / "data" / "datasets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "schema_version": 2,
        "generated_at": "2026-11-21T00:00:00",
        "sources": sources or {"csmar": {"id": "csmar", "name": "CSMAR", "type": "parquet", "tier": "DQ2", "status": "ACTIVE"}},
        "datasets": datasets,
    }
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _rec(did: str, path: str, **kw) -> dict:
    base = {
        "id": did,
        "path": path,
        "kind": "dataset",
        "status": "AVAILABLE",
        "caliber": "测试用",
        "lineage": [{"source_id": "csmar", "evidence": ["test"], "certainty": "exact"}],
        "gaps": [],
        "consumers": [],
    }
    base.update(kw)
    return base


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------- probe


def test_probe_csv_does_not_use_parquet_reader(tmp_root: Path) -> None:
    """D1 回归：csv 必须走文本行计数，不得报 parquet magic bytes 错误。"""
    f = tmp_root / "x.csv"
    f.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    pr = probe(f)
    assert pr.exists and pr.format == "csv"
    assert pr.rows == 3
    assert pr.n_fields == 2
    assert pr.error is None


def test_probe_parquet_roundtrip(tmp_root: Path) -> None:
    f = tmp_root / "y.parquet"
    pd.DataFrame({"code": ["000001", "000002"], "trade_date": ["2024-01-02", "2024-01-02"]}).to_parquet(f)
    pr = probe(f)
    assert pr.rows == 2
    assert pr.n_fields == 2
    assert "code" in pr.field_list


def test_probe_missing_file(tmp_root: Path) -> None:
    pr = probe(tmp_root / "nope.parquet")
    assert pr.exists is False and pr.error


# ---------------------------------------------------------------- check


def test_check_duplicate_primary_key_is_fatal(tmp_root: Path) -> None:
    (tmp_root / "a.parquet").write_bytes(b"x")
    _write_registry(tmp_root, [_rec("a", "a.parquet"), _rec("a", "a.parquet")])
    reg = load_registry(data_root=tmp_root)
    codes = [f.code for f in check(reg) if f.level == "fatal"]
    assert "R-PK-DUP" in codes


def test_check_missing_path_is_fatal_for_available(tmp_root: Path) -> None:
    _write_registry(tmp_root, [_rec("gone", "gone.parquet")])
    reg = load_registry(data_root=tmp_root)
    codes = [f.code for f in check(reg) if f.level == "fatal"]
    assert "R-PATH-MISSING" in codes


def test_check_missing_path_ok_when_unavailable(tmp_root: Path) -> None:
    _write_registry(tmp_root, [_rec("gone", "gone.parquet", status="UNAVAILABLE")])
    reg = load_registry(data_root=tmp_root)
    assert not [f for f in check(reg) if f.code == "R-PATH-MISSING"]


def test_check_bad_status_and_kind(tmp_root: Path) -> None:
    (tmp_root / "a.parquet").write_bytes(b"x")
    _write_registry(
        tmp_root,
        [_rec("a", "a.parquet", status="PROBABLY_FINE", kind="mystery")],
    )
    reg = load_registry(data_root=tmp_root)
    codes = {f.code for f in check(reg) if f.level == "fatal"}
    assert "R-STATUS-ENUM" in codes and "R-KIND-ENUM" in codes


def test_check_lineage_orphan_source(tmp_root: Path) -> None:
    (tmp_root / "a.parquet").write_bytes(b"x")
    rec = _rec("a", "a.parquet")
    rec["lineage"] = [{"source_id": "nonexistent", "evidence": [], "certainty": "exact"}]
    _write_registry(tmp_root, [rec])
    reg = load_registry(data_root=tmp_root)
    assert any(f.code == "R-LINEAGE-ORPHAN" for f in check(reg))


def test_check_null_source_ok_when_unknown(tmp_root: Path) -> None:
    """certainty=unknown 时 source_id 为 null 是合法的，不是孤儿。"""
    (tmp_root / "a.parquet").write_bytes(b"x")
    rec = _rec("a", "a.parquet")
    rec["lineage"] = [{"source_id": None, "evidence": ["无路径命中"], "certainty": "unknown"}]
    _write_registry(tmp_root, [rec])
    reg = load_registry(data_root=tmp_root)
    assert not [f for f in check(reg) if f.code == "R-LINEAGE-ORPHAN"]


def test_check_caliber_accepts_dict(tmp_root: Path) -> None:
    """views.json 的 caliber 是结构化对象，不能因为不是字符串就报空。"""
    (tmp_root / "a.parquet").write_bytes(b"x")
    _write_registry(tmp_root, [_rec("a", "a.parquet", caliber={"generation": "v2"})])
    reg = load_registry(data_root=tmp_root)
    assert not [f for f in check(reg) if f.code == "R-CALIBER-EMPTY"]


def test_check_regression_detects_lost_id(tmp_root: Path) -> None:
    (tmp_root / "a.parquet").write_bytes(b"x")
    _write_registry(tmp_root, [_rec("a", "a.parquet")])
    reg = load_registry(data_root=tmp_root)
    findings = check(reg, previous_ids={"a", "b"})
    assert any(f.code == "R-REGRESS-LOST" and f.target == "b" for f in findings)


def test_load_registry_missing_file_raises(tmp_root: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(data_root=tmp_root)


# ---------------------------------------------------------------- lint


def _price_frame(n_dup: int = 5) -> pd.DataFrame:
    rows = [
        {"Stkcd": "000001", "Trddt": "2024-01-02", "Opnprc": 10.0, "Clsprc": 10.1, "LimitUp": 1.0, "LimitDown": 0.0},
        {"Stkcd": "000001", "Trddt": "2024-01-03", "Opnprc": 10.1, "Clsprc": 10.3, "LimitUp": 0.0, "LimitDown": 0.0},
        {"Stkcd": "000002", "Trddt": "2024-01-02", "Opnprc": 20.0, "Clsprc": 19.9, "LimitUp": 0.0, "LimitDown": 0.0},
        {"Stkcd": "000002", "Trddt": "2024-01-03", "Opnprc": 19.9, "Clsprc": 20.4, "LimitUp": 0.0, "LimitDown": 0.0},
    ]
    df = pd.DataFrame(rows)
    if n_dup:
        df = pd.concat([df, df.head(n_dup)], ignore_index=True)
    return df


def test_lint_price_duplicate_is_fatal(tmp_root: Path) -> None:
    pd.DataFrame(_price_frame()).to_parquet(tmp_root / "px.parquet")
    _write_registry(tmp_root, [_rec("px", "px.parquet")])
    reg = load_registry(data_root=tmp_root)
    res = lint_dataset(reg, "px")
    dup = [f for f in res.findings if f.code == "L-P07-DUP"]
    assert dup and dup[0].level == "fatal"


def test_lint_event_duplicate_is_info_not_fatal(tmp_root: Path) -> None:
    """没有价格列的表，(code,date) 重复属正常，不能报 fatal。

    这是 CSMAR 董事/财务表的真实形态：同一天同一公司多条记录。
    """
    df = pd.DataFrame(
        {
            "Stkcd": ["000001"] * 4,
            "PartyDate": ["2001-05-01"] * 4,
            "PersonID": ["p1", "p2", "p3", "p4"],
        }
    )
    df.to_parquet(tmp_root / "ev.parquet")
    _write_registry(tmp_root, [_rec("ev", "ev.parquet")])
    reg = load_registry(data_root=tmp_root)
    res = lint_dataset(reg, "ev")
    assert not [f for f in res.findings if f.code == "L-P07-DUP"]
    comp = [f for f in res.findings if f.code == "L-P07-COMPOSITE"]
    assert comp and comp[0].level == "info"


def test_lint_industry_name_static_is_lookahead(tmp_root: Path) -> None:
    """industry_name 跨期不变 = 前视指纹，必须 fatal。"""
    rows = []
    for code in ("000001", "000002", "000003"):
        for m in ("2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"):
            rows.append({"code": code, "month_end": m, "industry_name": "银行", "next_ret": 0.01})
    pd.DataFrame(rows).to_parquet(tmp_root / "panel.parquet")
    _write_registry(tmp_root, [_rec("panel", "panel.parquet", kind="panel")])
    reg = load_registry(data_root=tmp_root)
    res = lint_dataset(reg, "panel")
    hit = [f for f in res.findings if f.code == "L-P11-INDUSTRY"]
    assert hit and hit[0].level == "fatal"


def test_lint_forbidden_field_and_lookahead_column(tmp_root: Path) -> None:
    df = pd.DataFrame({"code": ["000001"], "trade_date": ["2024-01-02"], "UPDATE_DATE": ["2024-01-03"], "D5": [0.01]})
    df.to_parquet(tmp_root / "fin.parquet")
    _write_registry(tmp_root, [_rec("fin", "fin.parquet")])
    reg = load_registry(data_root=tmp_root)
    codes = {f.code for f in lint_dataset(reg, "fin").findings}
    assert "L-P11-FIELD" in codes      # UPDATE_DATE
    assert "L-P11-LOOKAHEAD" in codes  # 龙虎榜 D5 前视收益列


def test_lint_limitstatus_all_zero_is_fatal(tmp_root: Path) -> None:
    df = pd.DataFrame(
        {
            "Stkcd": ["000001", "000002"],
            "Trddt": ["2024-01-02", "2024-01-02"],
            "Opnprc": [1.0, 2.0],
            "Clsprc": [1.1, 2.1],
            "LimitStatus": [0.0, 0.0],
        }
    )
    df.to_parquet(tmp_root / "lim.parquet")
    _write_registry(tmp_root, [_rec("lim", "lim.parquet")])
    reg = load_registry(data_root=tmp_root)
    hit = [f for f in lint_dataset(reg, "lim").findings if f.code == "L-P03-LIMIT"]
    assert hit and any(f.level == "fatal" for f in hit)


def test_lint_non_ashare_share_high_rate_is_warn(tmp_root: Path) -> None:
    df = pd.DataFrame(
        {
            "Stkcd": ["400001", "400002", "400003", "000001"],
            "Trddt": ["2024-01-02"] * 4,
            "Opnprc": [1.0] * 4,
            "Clsprc": [1.1] * 4,
        }
    )
    df.to_parquet(tmp_root / "uni.parquet")
    _write_registry(tmp_root, [_rec("uni", "uni.parquet")])
    reg = load_registry(data_root=tmp_root)
    hit = [f for f in lint_dataset(reg, "uni").findings if f.code == "L-P06-UNIVERSE"]
    # 75% 非 A 股 → warn；少量则是 info
    assert hit and hit[0].level == "warn"


def test_lint_missing_file_is_fatal(tmp_root: Path) -> None:
    _write_registry(tmp_root, [_rec("gone", "gone.parquet", status="UNAVAILABLE")])
    reg = load_registry(data_root=tmp_root)
    assert any(f.code == "L-PATH" and f.level == "fatal" for f in lint_dataset(reg, "gone").findings)


def test_lint_summary_counts(tmp_root: Path) -> None:
    pd.DataFrame(_price_frame()).to_parquet(tmp_root / "px.parquet")
    _write_registry(tmp_root, [_rec("px", "px.parquet")])
    reg = load_registry(data_root=tmp_root)
    s = summarize([lint_dataset(reg, "px")])
    assert s["datasets"] == 1
    assert s["findings"]["fatal"] >= 1


# ---------------------------------------------------------------- 合并


def test_build_from_legacy_merges_four_tables(tmp_root: Path) -> None:
    """四表合并：views 基准 + catalog 追加 + 物理事实不继承 + 血缘按路径推断。"""
    (tmp_root / "data" / "ref" / "csmar").mkdir(parents=True)
    (tmp_root / "data" / "ref" / "csmar" / "TRD_Dalyr.parquet").write_bytes(b"x")
    (tmp_root / "data" / "clean").mkdir(parents=True)
    (tmp_root / "data" / "clean" / "ic.csv").write_text("a\n1\n", encoding="utf-8")

    (tmp_root / "data" / "views.json").write_text(
        json.dumps(
            {
                "views": [
                    {"name": "ic_monthly", "path": "data/clean/ic.csv", "format": "csv",
                     "kind": "dataset", "caliber": {"g": "v2"},
                     "gaps": ["财务 PIT 不可用（announce_date 87% 滞后 >12 月）"]},
                    {"name": "trd", "path": "data/ref/csmar/TRD_Dalyr.parquet",
                     "format": "parquet", "kind": "dataset", "caliber": "日行情", "gaps": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_root / "data" / "catalog.json").write_text(
        json.dumps(
            {
                "datasets": [
                    {"name": "ic_monthly", "path": "data/clean/ic.csv", "format": "csv",
                     "rows": None, "error": "Parquet magic bytes not found in footer."},
                    {"name": "panel_x", "path": "data/ref/panel/panel_monthly.parquet",
                     "format": "parquet", "rows": 84000, "pit_anchor": "trade_date"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_root / "data" / "lineage.json").write_text(
        json.dumps({"views": [{"view": "ic_monthly", "source_kind": "migration",
                               "evidence": ["migration_log §2"], "certainty": "inferred"}]}),
        encoding="utf-8",
    )
    (tmp_root / "sources.json").write_text(
        json.dumps({"sources": [{"id": "csmar", "name": "CSMAR", "type": "parquet",
                                 "tier": "DQ2", "status": "ACTIVE",
                                 "datasets": {"TRD_Dalyr": "data/ref/csmar/TRD_Dalyr.parquet"}}]}),
        encoding="utf-8",
    )

    obj = build_from_legacy(tmp_root)
    ids = {d["id"] for d in obj["datasets"]}
    assert ids == {"ic_monthly", "trd", "panel_x"}, ids
    by = {d["id"]: d for d in obj["datasets"]}

    # 物理事实不入档
    assert "physical" not in by["ic_monthly"]
    # D2：被跨表套用的错误 gap 必须删掉
    gap_texts = [g["text"] for g in by["ic_monthly"]["gaps"]]
    assert not any("财务 PIT 不可用" in t for t in gap_texts)
    # 血缘：精确路径命中记 exact
    assert by["trd"]["lineage"][0]["certainty"] == "exact"
    assert by["trd"]["lineage"][0]["source_id"] == "csmar"
    # 血缘：前缀规则记 inferred（panel 表没在任何源里声明）
    assert by["panel_x"]["lineage"][0]["certainty"] in {"inferred", "unknown"}
    # catalog 独有的条目要标出待补口径
    assert "口径待补" in by["panel_x"]["status_reason"]
    # 源字典保留，datasets 字段改名
    assert "csmar" in obj["sources"]
    assert "declared_datasets" in obj["sources"]["csmar"]
    assert "datasets" not in obj["sources"]["csmar"]


def test_status_enum_size() -> None:
    """8 档状态枚举不许悄悄增减——下游 switch 依赖这个基数。"""
    assert len(STATUS_ENUM) == 8


# ------------------------------------------------ 引用反查与跨 run 依赖


def _fake_repo(tmp: Path, runs: dict[str, list[str]]) -> Path:
    """造一个最小仓库：data_root=tmp/数据源，repo=tmp。

    runs = {run_id: [写到该 run 的 run.json 里的文本]}
    """
    root = tmp / "数据源"
    (root / "data").mkdir(parents=True, exist_ok=True)
    for rid, texts in runs.items():
        d = tmp / "research" / "00_infra" / "runs" / rid
        (d / "02_data").mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(
            json.dumps({"run_id": rid, "note": " ".join(texts)}, ensure_ascii=False),
            encoding="utf-8",
        )
        for t in texts:
            if t.endswith(".py"):
                (d / "02_data" / t).write_text(f"# reads daily_ext.parquet\n", encoding="utf-8")
    return root


def test_scan_consumers_finds_run_that_reads_dataset(tmp_path: Path) -> None:
    from qrg.registry import scan_consumers

    root = _fake_repo(tmp_path, {"R1": ["uses daily_ext.parquet"], "R2": ["nothing here"]})
    p = root / "data" / "datasets.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "datasets": [{"id": "daily_ext", "path": "data/clean/daily_ext.parquet"}],
            }
        ),
        encoding="utf-8",
    )
    hits = scan_consumers(root)
    assert hits["daily_ext"] == ["R1"], "只有真正提到文件名的 run 才算消费者"


def test_find_references_reports_runs(tmp_path: Path) -> None:
    from qrg.registry import find_references

    root = _fake_repo(tmp_path, {"R1": ["daily_ext.parquet"], "R2": ["other.parquet"]})
    hits = find_references("daily_ext.parquet", root)
    assert [h["where"] for h in hits] == ["R1"]


def test_cross_run_artifacts_excludes_runs_holding_own_copy(tmp_path: Path) -> None:
    """引用了但自己有一份同名文件的，不算下游——否则每份副本都会误报。"""
    from qrg.registry import cross_run_artifacts

    root = _fake_repo(tmp_path, {"OWNER": ["shared.parquet"], "COPY": ["shared.parquet"], "USER": ["shared.parquet"]})
    repo = tmp_path
    for rid in ("OWNER", "COPY"):
        (repo / "research/00_infra/runs" / rid / "02_data" / "shared.parquet").write_bytes(b"x")
    (repo / "research/00_infra/runs/USER/02_data/other.parquet").write_bytes(b"x")

    arts = cross_run_artifacts(root)
    hit = {a["file"]: a for a in arts}
    assert "shared.parquet" in hit
    owners = set(hit["shared.parquet"]["owners"])
    assert owners == {"OWNER", "COPY"}
    assert hit["shared.parquet"]["used_by"] == ["USER"]
    assert hit["shared.parquet"]["ambiguous_owner"] is True


def test_refresh_consumers_only_touches_consumers(tmp_path: Path) -> None:
    from qrg.registry import refresh_consumers

    root = _fake_repo(tmp_path, {"R1": ["daily_ext.parquet"]})
    p = root / "data" / "datasets.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "datasets": [
                    {"id": "daily_ext", "path": "data/clean/daily_ext.parquet", "consumers": [], "caliber": "保留我"}
                ],
            }
        ),
        encoding="utf-8",
    )
    changed = refresh_consumers(p, data_root=root)
    assert changed["daily_ext"] == {"before": [], "after": ["R1"]}
    obj = json.loads(p.read_text(encoding="utf-8"))
    rec = obj["datasets"][0]
    assert rec["consumers"] == ["R1"]
    assert rec["caliber"] == "保留我", "刷新 consumers 不得改动其余字段"
    assert rec["consumers_scanned_at"]


def test_caliber_override_from_spec(tmp_path: Path) -> None:
    """口径覆盖表必须生效：datasets.json 里空的 caliber 由 spec 补上。"""
    from qrg.registry import apply_caliber_overrides

    ds = [{"id": "panel_monthly_csi800", "caliber": "", "gaps": []}]
    n = apply_caliber_overrides(ds)
    assert n == 1
    assert ds[0]["caliber"], "spec/dataset_caliber.json 应补上 caliber"
    assert ds[0]["caliber_source"] == "spec/dataset_caliber.json"
    assert ds[0]["gaps"], "caveats 应并入 gaps"


def test_legacy_archive_fallback_readable(tmp_path: Path) -> None:
    """旧表已归档到 _superseded 时，迁移仍应可重跑（R1：迁移要可复现）。"""
    from qrg.registry import LEGACY_ARCHIVE_STAMP, build_from_legacy

    root = tmp_path / "数据源"
    arch = root / "data" / "_superseded" / LEGACY_ARCHIVE_STAMP
    arch.mkdir(parents=True)
    (arch / "views.json").write_text(
        json.dumps({"views": [{"name": "a", "path": "data/a.parquet", "kind": "dataset", "caliber": "c"}]}),
        encoding="utf-8",
    )
    (arch / "catalog.json").write_text(json.dumps({"datasets": []}), encoding="utf-8")
    (arch / "lineage.json").write_text(json.dumps({"views": []}), encoding="utf-8")
    (arch / "sources.json").write_text(json.dumps({"sources": [{"id": "s1", "status": "ACTIVE"}]}), encoding="utf-8")

    obj = build_from_legacy(root)
    assert obj["datasets"][0]["id"] == "a"
    assert obj["spec_version"], "应自带 spec_version 以便自证遵循哪版契约"


# ------------------------------------------------ 登记入口（唯一合法路径）


def test_add_dataset_appends_and_sorts(tmp_path: Path) -> None:
    from qrg.registry import add_dataset

    root = tmp_path / "数据源"
    (root / "data").mkdir(parents=True)
    (root / "data" / "x.parquet").write_bytes(b"x")
    p = root / "data" / "datasets.json"
    p.write_text(json.dumps({"schema_version": 2, "datasets": [{"id": "z", "path": "data/z.parquet"}]}), encoding="utf-8")

    res = add_dataset(p, dataset_id="a", path="data/x.parquet", status="AVAILABLE",
                      caliber="测试", source_id="csmar", data_root=root)
    assert res["file_exists"] is True
    assert res["total"] == 2
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert [d["id"] for d in obj["datasets"]] == ["a", "z"], "写入后按 id 排序"
    assert obj["datasets"][0]["gaps"] == []


def test_add_dataset_rejects_duplicate_id(tmp_path: Path) -> None:
    from qrg.registry import add_dataset

    root = tmp_path / "数据源"
    (root / "data").mkdir(parents=True)
    p = root / "data" / "datasets.json"
    p.write_text(json.dumps({"schema_version": 2, "datasets": [{"id": "a", "path": "data/a.parquet"}]}), encoding="utf-8")
    with pytest.raises(RegistryError):
        add_dataset(p, dataset_id="a", path="data/a.parquet", data_root=root)


def test_add_dataset_rejects_bad_status(tmp_path: Path) -> None:
    from qrg.registry import add_dataset

    root = tmp_path / "数据源"
    (root / "data").mkdir(parents=True)
    p = root / "data" / "datasets.json"
    p.write_text(json.dumps({"schema_version": 2, "datasets": []}), encoding="utf-8")
    with pytest.raises(RegistryError, match="status 不在枚举内"):
        add_dataset(p, dataset_id="a", path="data/a.parquet", status="BOGUS", data_root=root)


def test_lint_registered_limitations_suppressed():
    """闭环语义：已在 known_limitations 留痕的缺陷降为 info，未登记的保持 fatal。"""
    from qrg.lint import LintResult, _suppress_registered
    from qrg.registry import Finding

    res = LintResult(dataset="panel", path="data/clean/factors/panel.parquet", rows_scanned=1, sampled=False)
    res.findings = [
        Finding("fatal", "L-P11-INDUSTRY", "panel", "行业名跨期不变"),
        Finding("fatal", "L-P04-PIT", "panel", "NOTICE_DATE 不可用"),
    ]
    rec = {"known_limitations": ["【qrg data lint 2026-11-22】行业名跨期 100% 不变（P11 前视指纹）……"]}
    _suppress_registered(res, rec)
    by_code = {f.code: f for f in res.findings}
    assert by_code["L-P11-INDUSTRY"].level == "info"       # 已登记 → info
    assert by_code["L-P04-PIT"].level == "fatal"           # 未登记 → 保持 fatal


def test_lint_suppression_requires_tag():
    from qrg.lint import LintResult, _suppress_registered
    from qrg.registry import Finding

    res = LintResult(dataset="x", path="a.parquet", rows_scanned=1, sampled=False)
    res.findings = [Finding("fatal", "L-P11-INDUSTRY", "x", "行业名跨期不变")]
    # 没有【qrg data lint】标签的局限条目不触发降级
    _suppress_registered(res, {"known_limitations": ["行业列有 P11 问题（人工备注）"]})
    assert res.findings[0].level == "fatal"


def test_lint_registered_limitations_suppressed():
    """闭环语义：已在 known_limitations 留痕的缺陷降为 info，未登记的保持 fatal。"""
    from qrg.lint import LintResult, _suppress_registered
    from qrg.registry import Finding

    res = LintResult(dataset="panel", path="data/clean/factors/panel.parquet", rows_scanned=1, sampled=False)
    res.findings = [
        Finding("fatal", "L-P11-INDUSTRY", "panel", "行业名跨期不变"),
        Finding("fatal", "L-P04-PIT", "panel", "NOTICE_DATE 不可用"),
    ]
    rec = {"known_limitations": ["【qrg data lint 2026-11-22】行业名跨期 100% 不变（P11 前视指纹）……"]}
    _suppress_registered(res, rec)
    by_code = {f.code: f for f in res.findings}
    assert by_code["L-P11-INDUSTRY"].level == "info"       # 已登记 → info
    assert by_code["L-P04-PIT"].level == "fatal"           # 未登记 → 保持 fatal


def test_lint_suppression_requires_tag():
    from qrg.lint import LintResult, _suppress_registered
    from qrg.registry import Finding

    res = LintResult(dataset="x", path="a.parquet", rows_scanned=1, sampled=False)
    res.findings = [Finding("fatal", "L-P11-INDUSTRY", "x", "行业名跨期不变")]
    # 没有【qrg data lint】标签的局限条目不触发降级
    _suppress_registered(res, {"known_limitations": ["行业列有 P11 问题（人工备注）"]})
    assert res.findings[0].level == "fatal"
