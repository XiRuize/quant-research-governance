"""数据集登记：加载、校验、现场探测。

设计原则（见 spec/registry.spec.json）：

1. **一条数据集 = 一条记录**，主键 `id` 唯一，分母唯一。
2. **源不是数据集**，`sources` 保留为字典被 `datasets[].lineage[].source_id` 引用。
3. **物理事实不入档**。行数、体积、字段数由 `probe()` 现场算，
   不写进 datasets.json —— 静态快照必然过期，catalog.json 就是前车之鉴
   （67 条里 5 条 error 残留、gaps 写着已被推翻的结论）。

旧体系四张表的分母分别是 67 / 128 / 36 / 14，彼此不互指，没有任何主键约束，
于是 68 个数据集在 catalog 里查不到、lineage 只覆盖 23%。本模块把
「一致性靠人肉对账」换成「主键机械校验」。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 8 档数据状态，与 cleaning.spec.json → data_status_enum 对齐
STATUS_ENUM = {
    "AVAILABLE",       # 实测可用，覆盖期达标
    "DERIVED",         # 由其他数据集派生
    "PARTIAL",         # 有数据但覆盖不全
    "STALE",           # 有数据但已停止更新
    "SNAPSHOT_ONLY",   # 只有单时点快照，不能当历史序列用
    "UNAVAILABLE",     # 实测不可用
    "FROZEN",          # 冻结只读，迁移中
    "UNKNOWN",         # 未验证；不得当可用使用
}

# 视为「应该能在磁盘上找到文件」的状态
_EXPECT_EXISTS = {"AVAILABLE", "DERIVED", "STALE", "SNAPSHOT_ONLY"}

# status=AVAILABLE 等却找不到文件 = fatal；其余状态找不到 = 正常
KIND_ENUM = {"dataset", "dimension", "panel", "event_table"}

# 旧四表被归档到 数据源/data/_superseded/<stamp>/；迁移从这里可复现重跑
LEGACY_ARCHIVE_STAMP = "2026-11-21"


class RegistryError(ValueError):
    """登记表不合法（结构性错误）。"""


def default_data_root() -> Path:
    """数据集根目录。

    优先 `QRG_DATA_ROOT`；否则取仓库同级的 `数据源/`。
    不依赖任何已不存在的旧仓库路径（`未来/金融智能体` 已删除，
    旧 `build_catalog.py` 因为硬编码它而彻底失效）。
    """
    env = os.environ.get("QRG_DATA_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / "数据源").resolve()


def default_registry_path(data_root: Path | None = None) -> Path:
    root = data_root or default_data_root()
    return root / "data" / "datasets.json"


# ---------------------------------------------------------------- 加载


@dataclass
class Registry:
    """datasets.json 的内存视图。"""

    path: Path | None
    data_root: Path
    schema_version: int
    generated_at: str
    sources: dict[str, dict[str, Any]]
    datasets: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)
    _live_consumers: dict[str, list[str]] | None = field(default=None, repr=False, compare=False)

    # -- 查询 -------------------------------------------------------

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {d["id"]: d for d in self.datasets}

    def get(self, dataset_id: str) -> dict[str, Any]:
        hit = self.by_id.get(dataset_id)
        if hit is None:
            near = [k for k in self.by_id if dataset_id in k][:5]
            raise KeyError(f"未登记的数据集 {dataset_id!r}；相近: {near}")
        return hit

    def ids_matching(self, pattern: str) -> list[str]:
        p = pattern.lower()
        return sorted(k for k in self.by_id if p in k.lower())

    def abs_path(self, dataset_id: str) -> Path:
        rec = self.get(dataset_id)
        return (self.data_root / str(rec["path"]).replace("\\", "/")).resolve()

    def consumers_of(self, dataset_id: str, *, live: bool = True) -> list[str]:
        """谁在用这张表。默认**现场重扫**，不用落盘缓存（缓存会过期）。

        live=False 时只读 datasets.json 里的缓存值，用于离线/快速路径。
        """
        rec = self.get(dataset_id)
        out: set[str] = set(rec.get("consumers") or []) if not live else set()
        if live:
            if self._live_consumers is None:
                try:
                    object.__setattr__(self, "_live_consumers", scan_consumers(self.data_root))
                except Exception:
                    object.__setattr__(self, "_live_consumers", {})
            out |= set((self._live_consumers or {}).get(dataset_id, []))
        # 反向扫：任何 datasets 的 lineage.raw_tables 里提到本 id 的也算
        for d in self.datasets:
            for edge in d.get("lineage") or []:
                if dataset_id in (edge.get("raw_tables") or []) and d["id"] != dataset_id:
                    out.add(d["id"])
        return sorted(out)


def _spec_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "spec"


def _spec_version() -> str:
    """读 registry.spec.json 的版本号，保证 datasets.json 能自证遵循哪版契约。"""
    try:
        return str(json.loads((_spec_dir() / "registry.spec.json").read_text(encoding="utf-8")).get("spec_version") or "")
    except Exception:
        return ""


def caliber_overrides() -> dict[str, dict[str, Any]]:
    """spec/dataset_caliber.json 里的口径覆盖。

    口径归治理层：迁移可重跑，但「这张表能回答什么」是人写的判断，
    重跑不该冲掉它，所以独立存一份、读取时覆盖进来。
    """
    p = _spec_dir() / "dataset_caliber.json"
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    ov = obj.get("overrides")
    return ov if isinstance(ov, dict) else {}


def apply_caliber_overrides(datasets: list[dict[str, Any]]) -> int:
    """把覆盖表写进 datasets[]，返回命中条数。"""
    ov = caliber_overrides()
    n = 0
    for d in datasets:
        hit = ov.get(d.get("id"))
        if not hit:
            continue
        cal = hit.get("caliber")
        if cal:
            d["caliber"] = cal
            d["caliber_source"] = "spec/dataset_caliber.json"
            n += 1
        cav = hit.get("caveats")
        if cav:
            have = {g.get("text") for g in (d.get("gaps") or []) if isinstance(g, dict)}
            gaps = list(d.get("gaps") or [])
            for c in cav:
                if c not in have:
                    gaps.append({"text": c, "verified_at": "2026-11-21", "note": "补充自 dataset_caliber.json"})
            d["gaps"] = gaps
    return n


def load_registry(path: str | Path | None = None, *, data_root: Path | None = None) -> Registry:
    """读取 datasets.json。文件不存在时抛 RegistryError（调用方给明确指引）。"""
    root = Path(data_root) if data_root else default_data_root()
    p = Path(path) if path else default_registry_path(root)
    if not p.exists():
        raise RegistryError(
            f"数据集登记表不存在: {p}\n"
            "  生成方式: qrg data init --from-legacy"
        )
    with p.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or "datasets" not in raw:
        raise RegistryError(f"{p} 不是合法的 datasets.json（缺少 datasets 数组）")
    datasets = list(raw["datasets"])
    apply_caliber_overrides(datasets)
    return Registry(
        path=p,
        data_root=root,
        schema_version=int(raw.get("schema_version", 0)),
        generated_at=str(raw.get("generated_at", "")),
        sources=dict(raw.get("sources") or {}),
        datasets=datasets,
        raw=raw,
    )


# ---------------------------------------------------------------- 探测


@dataclass
class Probe:
    """一次现场探测的物理事实。**不落盘**，用完即弃。"""

    exists: bool
    format: str = ""
    rows: int | None = None
    n_fields: int | None = None
    file_mb: float | None = None
    field_list: tuple[str, ...] = ()
    error: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "format": self.format,
            "rows": self.rows,
            "n_fields": self.n_fields,
            "file_mb": round(self.file_mb, 2) if self.file_mb is not None else None,
            "fields": list(self.field_list),
            "coverage": self.coverage,
            "error": self.error,
        }


def _parquet_meta(p: Path) -> tuple[int, list[str]]:
    import pyarrow.parquet as pq

    md = pq.ParquetFile(p).metadata
    return int(md.num_rows), [md.schema.column(i).name for i in range(md.num_columns)]


def _csv_meta(p: Path, *, max_rows: int | None = None) -> tuple[int, list[str]]:
    """CSV 只读表头 + 数行，**不走 parquet 读器**。

    旧 catalog 的 D1 缺陷就是把 csv 丢进 parquet footer 探测，
    于是 5 个正常 CSV 被标成 `Parquet magic bytes not found`。
    这里按扩展名分派，从根上避免。
    """
    import csv

    n = 0
    with p.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return 0, []
        for _ in reader:
            n += 1
            if max_rows is not None and n >= max_rows:
                break
    return n, header


def probe(path: Path | str, *, full: bool = True) -> Probe:
    """探测一个数据集的物理事实。

    Args:
        path: 文件路径（已解析为绝对路径）
        full: True 时统计全量行数；False 时 parquet 只读 metadata（快）、
              csv 只数前 10 万行（标记 approximate）。
    """
    p = Path(path)
    if not p.exists():
        return Probe(exists=False, error="文件不存在")
    size = p.stat().st_size
    suffix = p.suffix.lower()
    fmt = suffix.lstrip(".")

    try:
        if suffix == ".parquet":
            # parquet metadata 自带 num_rows，无需读数据
            rows, cols = _parquet_meta(p)
            cov = {}
            if full:
                cov = _parquet_coverage(p, cols)

            return Probe(True, fmt, rows, len(cols), size / 1e6, tuple(cols), coverage=cov)
        if suffix in {".csv", ".txt"}:
            rows, cols = _csv_meta(p, max_rows=None if full else 100_000)
            approx = "" if full else " (前10万行)"
            return Probe(
                True, fmt, rows, len(cols), size / 1e6, tuple(cols),
                coverage={"note": f"行数为文本行计数{approx}"},
            )
        if suffix == ".json":
            with p.open(encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, list):
                return Probe(True, fmt, len(obj), None, size / 1e6)
            if isinstance(obj, dict):
                return Probe(True, fmt, len(obj), None, size / 1e6, tuple(obj.keys())[:20])
            return Probe(True, fmt, 1, None, size / 1e6)
        if suffix == ".zip":
            import zipfile

            with zipfile.ZipFile(p) as zf:
                names = zf.namelist()
            return Probe(True, fmt, len(names), None, size / 1e6, tuple(names[:20]))
        return Probe(True, fmt, None, None, size / 1e6, error=f"未知格式 {suffix}")
    except Exception as exc:  # noqa: BLE001 - 探测失败要报告而不是中断
        return Probe(True, fmt, None, None, size / 1e6, error=f"{type(exc).__name__}: {exc}")


_CODE_HINTS = ("code", "stkcd", "security_code", "symbol")
_DATE_HINTS = ("trade_date", "trddt", "month_end", "date", "reportdate", "snapshot_date")


def _pick(cols: Iterable[str], hints: tuple[str, ...]) -> str | None:
    low = {c.lower(): c for c in cols}
    for h in hints:
        if h in low:
            return low[h]
    for h in hints:
        for lc, orig in low.items():
            if h in lc:
                return orig
    return None


def _parquet_coverage(p: Path, cols: list[str]) -> dict[str, Any]:
    """只读两列估算覆盖期与对象数，避免全表进内存。

    1884 万行的 TRD_Dalyr 全量读要 1.5GB，这里只读 code+date 两列。
    """
    try:
        import pyarrow.parquet as pq

        code_col = _pick(cols, _CODE_HINTS)
        date_col = _pick(cols, _DATE_HINTS)
        want = [c for c in (code_col, date_col) if c]
        if not want:
            return {}
        tbl = pq.read_table(p, columns=want)
        out: dict[str, Any] = {}
        if date_col:
            dates = tbl.column(date_col).to_pandas()
            dates = dates.dropna()
            if len(dates):
                out["start"] = str(dates.min())[:10]
                out["end"] = str(dates.max())[:10]
        if code_col:
            codes = tbl.column(code_col).to_pandas()
            out["n_codes"] = int(codes.nunique())
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": f"coverage probe failed: {type(exc).__name__}"}


# ---------------------------------------------------------------- 校验


@dataclass
class Finding:
    level: str      # fatal | warn | info
    code: str       # 稳定标识，便于测试与检索
    target: str     # 数据集 id 或文件
    message: str

    def __str__(self) -> str:
        tag = {"fatal": "FAIL", "warn": "WARN", "info": "INFO"}[self.level]
        return f"[{tag}] {self.code} {self.target}: {self.message}"


def _caliber_filled(cal: Any) -> bool:
    """caliber 允许多种形态：一句话字符串，或结构化对象（如 views 里的
    `{generation, neutralization, screen_rule}`）。非空即视为已填。"""
    if cal is None:
        return False
    if isinstance(cal, str):
        return bool(cal.strip())
    if isinstance(cal, (dict, list, tuple)):
        return len(cal) > 0
    return bool(cal)


def check(
    reg: Registry,
    *,
    verify_paths: bool = True,
    previous_ids: set[str] | None = None,
) -> list[Finding]:
    """按 spec/registry.spec.json 的 check_rules 校验。"""
    out: list[Finding] = []

    # -- fatal: 主键唯一 ------------------------------------------
    seen: dict[str, int] = {}
    for d in reg.datasets:
        seen[d.get("id", "")] = seen.get(d.get("id", ""), 0) + 1
    for did, n in seen.items():
        if n > 1:
            out.append(Finding("fatal", "R-PK-DUP", did, f"主键重复 {n} 次"))
    for d in reg.datasets:
        if not d.get("id"):
            out.append(Finding("fatal", "R-PK-EMPTY", "?", "记录缺少 id"))

    # -- fatal: 必填字段 -----------------------------------------
    required = ("id", "path", "kind", "status", "lineage")
    for d in reg.datasets:
        did = d.get("id", "?")
        for k in required:
            if k not in d:
                out.append(Finding("fatal", "R-FIELD-MISSING", did, f"缺必填字段 {k}"))
        if d.get("kind") and d["kind"] not in KIND_ENUM:
            out.append(
                Finding("fatal", "R-KIND-ENUM", did, f"kind={d['kind']!r} 不在 {sorted(KIND_ENUM)}")
            )
        st = d.get("status")
        if st and st not in STATUS_ENUM:
            out.append(Finding("fatal", "R-STATUS-ENUM", did, f"status={st!r} 不在 8 档枚举内"))

    # -- fatal: lineage.source_id 必须存在 ------------------------
    for d in reg.datasets:
        for edge in d.get("lineage") or []:
            sid = edge.get("source_id")
            if sid and sid not in reg.sources:
                out.append(
                    Finding("fatal", "R-LINEAGE-ORPHAN", d["id"], f"lineage.source_id={sid!r} 不在 sources 字典")
                )

    # -- fatal: 声明可用但文件不存在 ------------------------------
    if verify_paths:
        for d in reg.datasets:
            p = reg.data_root / str(d.get("path", "")).replace("\\", "/")
            if not p.exists() and d.get("status") in _EXPECT_EXISTS:
                out.append(
                    Finding(
                        "fatal", "R-PATH-MISSING", d["id"],
                        f"status={d['status']} 但文件不存在: {d.get('path')}",
                    )
                )

    # -- warn: caliber 为空 --------------------------------------
    for d in reg.datasets:
        if not _caliber_filled(d.get("caliber")):
            out.append(Finding("warn", "R-CALIBER-EMPTY", d["id"], "caliber 为空，读者不知道这张表能回答什么"))

    # -- warn: 同一 path 两条记录 --------------------------------
    path_seen: dict[str, list[str]] = {}
    for d in reg.datasets:
        key = str(d.get("path", "")).replace("\\", "/").lower()
        path_seen.setdefault(key, []).append(d["id"])
    for path, ids in path_seen.items():
        if len(ids) > 1:
            out.append(Finding("warn", "R-PATH-DUP", ",".join(ids), f"同一 path 被 {len(ids)} 条记录引用: {path}"))

    # -- warn: gaps 超过 180 天未复验 -----------------------------
    from datetime import date as _date

    today = _date.today()
    for d in reg.datasets:
        for g in d.get("gaps") or []:
            va = g.get("verified_at")
            if not va:
                out.append(Finding("warn", "R-GAP-NO-VERIFIED", d["id"], f"gap 未标 verified_at: {g.get('text', '')[:40]}"))
                continue
            try:
                y, m, dd = (int(x) for x in str(va)[:10].split("-"))
                age = (today - _date(y, m, dd)).days
            except ValueError:
                out.append(Finding("warn", "R-GAP-BAD-DATE", d["id"], f"verified_at 无法解析: {va}"))
                continue
            if age > 180:
                out.append(
                    Finding("warn", "R-GAP-STALE", d["id"], f"gap 已 {age} 天未复验: {g.get('text', '')[:40]}")
                )

    # -- warn: FROZEN 但无 consumers ------------------------------
    for d in reg.datasets:
        if d.get("status") == "FROZEN" and not reg.consumers_of(d["id"]):
            out.append(Finding("warn", "R-FROZEN-NO-CONSUMER", d["id"], "冻结但无人引用，应评估下线"))

    # -- regression: 总数下降 ------------------------------------
    if previous_ids is not None:
        now = {d["id"] for d in reg.datasets}
        lost = sorted(previous_ids - now)
        for did in lost:
            out.append(Finding("warn", "R-REGRESS-LOST", did, "上次有、本次消失（可能误删登记）"))

    return out


def render_findings(findings: list[Finding]) -> str:
    if not findings:
        return "registry check: 全部通过，无 fatal / warn。"
    order = {"fatal": 0, "warn": 1, "info": 2}
    rows = sorted(findings, key=lambda f: (order.get(f.level, 9), f.code, f.target))
    n_fatal = sum(1 for f in rows if f.level == "fatal")
    n_warn = sum(1 for f in rows if f.level == "warn")
    lines = [f"registry check: {n_fatal} fatal / {n_warn} warn / {len(rows)} total", ""]
    lines += [str(f) for f in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- 从旧四表合并

# 路径前缀 → 源 id。**这是推断，不是观测**，所以 certainty 一律 inferred，
# 并在 evidence 里写明命中的是哪条规则，方便日后人工升格为 exact。
PATH_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("data/ref/csmar/", "csmar"),
    ("data/ref/binance/", "binance_public"),
    ("data/crypto/", "btc_proxy"),
    ("data/ref/academic/", "academic_ceads"),
    ("data/ref/industry/", "swsresearch"),
    ("data/ref/panel/", "repo_panel"),
    ("data/ref/factors/", "repo_panel"),
    ("data/ref/lhb/", "eastmoney"),
    ("data/ref/margin/", "eastmoney"),
)

# 截面快照：只有单一时点，不能当历史序列用
SNAPSHOT_IDS = {"stock_basic", "st_status", "industry_snapshot"}
SNAPSHOT_PATH_HINTS = ("snapshot",)

# D2：这条 gap 只对 yjbb 成立，被跨表套用到 financial_pit；合并时删除
_KNOWN_BAD_GAPS = {
    "财务 PIT 不可用（announce_date 87% 滞后 >12 月）",
    "财务PIT不可用（announce_date 87% 滞后 >12 月）",
}


def _norm_path(p: Any) -> str:
    return str(p).replace("\\", "/").strip("/").lower()


def _infer_source(rel_path: str, sidx: dict[str, list[str]]) -> tuple[str | None, str, str]:
    """返回 (source_id, certainty, evidence)。

    三级：路径精确命中 → exact；前缀规则命中 → inferred；都没有 → unknown。
    """
    k = _norm_path(rel_path)
    if k in sidx:
        sids = sorted(set(sidx[k]))
        return sids[0], "exact", f"sources.datasets 声明路径精确命中 {rel_path}"
    for prefix, sid in PATH_PREFIX_RULES:
        if k.startswith(prefix):
            return sid, "inferred", f"路径前缀规则 {prefix}* → {sid}"
    return None, "unknown", "无路径命中、无前缀规则适用，需人工确认上游"


_CONSUMER_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "_superseded"}


def scan_consumers(
    data_root: str | Path | None = None,
    *,
    max_file_mb: float = 1.0,
    max_run_mb: float = 24.0,
) -> dict[str, list[str]]:
    """扫 research/*/runs/*/ 里的 run.json 与脚本，反查「谁用了这张表」。

    为什么现场扫而不存快照：consumers 会随新 run 增加而过期，
    与 catalog 的物理事实同类问题（R1 可再生产物不该落盘）。
    存下来的 consumers 只作为缓存，`consumers_of` 会以现场扫结果为准。

    匹配 token 取「相对路径」与「文件名」两种形态：
      - `data/clean/factors/panel.parquet`（显式路径）
      - `panel.parquet`（脚本里常见写法 DATA/"clean/factors"/"panel.parquet"）
    文件名短于 8 字符不参与匹配，避免 market.parquet 这类噪声。
    """
    root = Path(data_root) if data_root else default_data_root()
    repo = root.parent
    reg_path = default_registry_path(root)
    if not reg_path.exists():
        return {}
    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    tokens: dict[str, list[str]] = {}
    for rec in raw.get("datasets") or []:
        p = _norm_path(rec.get("path"))
        tk = []
        if p:
            tk.append(p)
            base = p.rsplit("/", 1)[-1]
            if len(base) >= 8:
                tk.append(base)
        if tk:
            tokens[str(rec["id"])] = tk

    hits: dict[str, set[str]] = {k: set() for k in tokens}
    for run_id, blob in _run_blobs(repo).items():
        for did, tk in tokens.items():
            if any(t in blob for t in tk):
                hits[did].add(run_id)

    return {k: sorted(v) for k, v in hits.items() if v}


def _run_blobs(
    repo: Path, *, max_file_mb: float = 1.0, max_run_mb: float = 24.0
) -> dict[str, str]:
    """把每个 run 目录里的文本读成一个大字符串，供引用反查。

    返回 {run_id: blob}。只读 run.json / .py / .md / .json / .toml / .yaml，
    超过 max_file_mb 的单文件跳过，单 run 累计超过 max_run_mb 即停。
    """
    out: dict[str, str] = {}
    for rjp in sorted(repo.glob("research/*/runs/*/run.json")):
        budget = max_run_mb * 1024 * 1024
        chunks: list[str] = []
        try:
            txt = rjp.read_text(encoding="utf-8")
        except Exception:
            continue
        chunks.append(txt)
        budget -= len(txt)
        for f in sorted(rjp.parent.rglob("*")):
            if budget <= 0:
                break
            if not f.is_file() or f.suffix.lower() not in {".py", ".md", ".json", ".toml", ".yaml", ".yml"}:
                continue
            if any(part in _CONSUMER_SKIP_DIRS for part in f.parts):
                continue
            try:
                if f.stat().st_size > max_file_mb * 1024 * 1024:
                    continue
                t = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            chunks.append(t)
            budget -= len(t)
        out[rjp.parent.name] = "\n".join(chunks).replace("\\", "/")
    return out


def find_references(
    target: str,
    data_root: str | Path | None = None,
    *,
    include_repo_scripts: bool = True,
) -> list[dict[str, str]]:
    """谁引用了某个文件。用于「删之前先查谁在用」。

    target 可以是相对路径、绝对路径或纯文件名。返回
    [{where, kind, file}]，kind ∈ {run, script}。
    """
    root = Path(data_root) if data_root else default_data_root()
    repo = root.parent
    t = str(target).replace("\\", "/").strip()
    probes = {t}
    base = t.rsplit("/", 1)[-1]
    if len(base) >= 6:
        probes.add(base)

    hits: list[dict[str, str]] = []

    for run_id, blob in _run_blobs(repo).items():
        if any(p in blob for p in probes):
            hits.append({"where": run_id, "kind": "run", "file": ""})

    if include_repo_scripts:
        for f in sorted(repo.glob("scripts/**/*.py")):
            if any(part in _CONSUMER_SKIP_DIRS for part in f.parts):
                continue
            try:
                if f.stat().st_size > 2 * 1024 * 1024:
                    continue
                blob = f.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
            except Exception:
                continue
            if any(p in blob for p in probes):
                hits.append({"where": str(f.relative_to(repo)), "kind": "script", "file": ""})

    return hits


def cross_run_artifacts(
    data_root: str | Path | None = None,
    *,
    dirs: tuple[str, ...] = ("02_data", "03_out"),
) -> list[dict[str, Any]]:
    """跨 run 隐式依赖：某 run 的产出物被**其他** run 引用。

    这类文件是最危险的：它不在 datasets.json 里（不是数据源），
    却有下游依赖。删掉它不会报错，只会在别人的下一轮研究里炸掉。
    `daily_ext.parquet`（235 MB，住在 BEAR run 下、被 FREQ/UNI 引用）就是实例。
    """
    root = Path(data_root) if data_root else default_data_root()
    repo = root.parent

    data_sfx = {".parquet", ".csv", ".npz", ".pkl", ".feather"}
    # 每个 run 实际持有哪些数据文件名（全目录，不限 02_data/03_out）
    holds: dict[str, set[str]] = {}
    sizes: dict[str, int] = {}
    for f in repo.glob("research/*/runs/*/**/*"):
        if not f.is_file() or f.suffix.lower() not in data_sfx:
            continue
        parts = f.parts
        if "runs" not in parts:
            continue
        rid = parts[parts.index("runs") + 1]
        holds.setdefault(rid, set()).add(f.name)
        try:
            sizes[f.name] = max(sizes.get(f.name, 0), f.stat().st_size)
        except OSError:
            pass

    # 候选 = 出现在 02_data / 03_out 下的文件（run 的产出物，而非脚本或输入副本）
    candidates: dict[str, set[str]] = {}
    for d in dirs:
        for f in repo.glob(f"research/*/runs/*/{d}/**/*"):
            if not f.is_file() or f.suffix.lower() not in data_sfx:
                continue
            parts = f.parts
            rid = parts[parts.index("runs") + 1]
            candidates.setdefault(f.name, set()).add(rid)

    blobs = _run_blobs(repo)
    out: list[dict[str, Any]] = []
    for name, own in sorted(candidates.items()):
        # 只把「引用了但自己不持有同名文件」的算作下游
        users = sorted(r for r, b in blobs.items() if name in b and name not in holds.get(r, set()))
        if not users:
            continue
        out.append(
            {
                "file": name,
                "owners": sorted(own),
                "used_by": users,
                "mb": round(sizes.get(name, 0) / 1e6, 1),
                "ambiguous_owner": len(own) > 1,
            }
        )
    return sorted(out, key=lambda x: (-len(x["used_by"]), -x["mb"]))


def add_dataset(
    registry_path: str | Path | None,
    *,
    dataset_id: str,
    path: str,
    status: str = "AVAILABLE",
    kind: str = "dataset",
    caliber: str = "",
    source_id: str | None = None,
    certainty: str = "unknown",
    gaps: list[str] | None = None,
    data_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """向 datasets.json 追加一条数据集登记（幂等：同 id 已存在则报错）。

    存在的理由：归档 `register_dataset.py` 之后必须有唯一合法入口，
    否则下一个人要么手改 JSON、要么把旧脚本复活，两条路都会再次分裂登记。
    """
    root = Path(data_root) if data_root else default_data_root()
    p = Path(registry_path) if registry_path else default_registry_path(root)
    if not p.exists():
        raise RegistryError(f"数据集登记表不存在: {p}\n  生成方式: qrg data init --from-legacy")
    raw = json.loads(p.read_text(encoding="utf-8"))
    datasets = list(raw.get("datasets") or [])
    if any(d.get("id") == dataset_id for d in datasets):
        raise RegistryError(f"数据集 id 已存在: {dataset_id}")
    if status not in STATUS_ENUM:
        raise RegistryError(f"status 不在枚举内: {status}；合法值 {sorted(STATUS_ENUM)}")
    if kind not in KIND_ENUM:
        raise RegistryError(f"kind 不在枚举内: {kind}；合法值 {sorted(KIND_ENUM)}")

    rel = _norm_path(path)
    exists = (root / rel).exists()
    rec: dict[str, Any] = {
        "id": dataset_id,
        "path": rel,
        "kind": kind,
        "status": status,
        "caliber": caliber,
        "lineage": [
            {
                "source_id": source_id,
                "evidence": [f"qrg data add 于 {_now_iso()[:10]}"],
                "certainty": certainty,
            }
        ],
        "gaps": [{"text": g, "verified_at": _now_iso()[:10], "note": "qrg data add"} for g in (gaps or [])],
        "consumers": [],
        "verified_at": _now_iso()[:10],
    }
    datasets.append(rec)
    raw["datasets"] = sorted(datasets, key=lambda x: str(x["id"]))
    if not dry_run:
        p.write_text(json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"record": rec, "file_exists": exists, "total": len(datasets)}


def refresh_consumers(
    registry_path: str | Path | None = None,
    *,
    data_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """只重扫 consumers，不动其余字段。返回 {id: [run_id]} 的变更摘要。

    为什么单独一条命令：`init --from-legacy` 是一次性迁移，
    而 consumers 会随新 run 不断增加。若不提供增量刷新，
    这个字段就会重演 catalog 的命运——写下即过期。
    """
    root = Path(data_root) if data_root else default_data_root()
    p = Path(registry_path) if registry_path else default_registry_path(root)
    if not p.exists():
        raise RegistryError(f"数据集登记表不存在: {p}\n  生成方式: qrg data init --from-legacy")
    raw = json.loads(p.read_text(encoding="utf-8"))
    hits = scan_consumers(root)
    stamp = _now_iso()
    changed: dict[str, Any] = {}
    for d in raw.get("datasets") or []:
        new = hits.get(d["id"], [])
        old = list(d.get("consumers") or [])
        if new != old:
            changed[d["id"]] = {"before": old, "after": new}
        d["consumers"] = new
        d["consumers_scanned_at"] = stamp
    if not dry_run:
        p.write_text(json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return changed


def build_from_legacy(data_root: str | Path | None = None) -> dict[str, Any]:
    """把 catalog / views / lineage / sources 合并成单表 datasets.json。

    合并规则（见 spec/registry.spec.json → migration_from_legacy_tables）：

    * **以 views 为基准**（128 条，最全）建 datasets[]。
    * **catalog 独有的追加**（7 条旧派生面板），status 先置 PARTIAL 等补口径。
    * **物理事实不继承**：catalog 的 rows / fields / file_mb / error 一律丢弃，
      改由 `probe()` 现场算（R1：可再生产物不入档）。但 pit_anchor / refresh /
      fetch_script 是口径而非物理事实，保留。
    * **血缘靠路径推断**：`sources[].datasets` 精确命中记 exact；否则按
      PATH_PREFIX_RULES 推断记 inferred；都没有记 unknown（source_id 置 null）。
    * **修 D1**：不再用 parquet 读器探测 csv，5 条 `error` 残留自然消失。
    * **修 D2**：删除被跨表套用的「财务 PIT 不可用」gap。
    """
    root = Path(data_root) if data_root else default_data_root()
    ddir = root / "data"
    archived = ddir / "_superseded" / LEGACY_ARCHIVE_STAMP

    def _locate(name: str) -> Path:
        """旧表查找顺序：data/ 原位 → 数据源/ 根（sources.json 历史位置）→ 归档副本。

        三级兜底是为了让迁移可重跑（R1）：旧表一旦移进 _superseded，
        `data init --from-legacy` 仍能从归档副本完整复现。
        """
        for cand in (ddir / name, root / name, archived / name):
            if cand.exists():
                return cand
        raise RegistryError(
            f"缺少旧表 {name}（查过 {ddir / name}、{root / name}、{archived / name}）"
        )

    def _read(name: str, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        p = _locate(name)
        with p.open(encoding="utf-8") as fh:
            obj = json.load(fh)
        return list(obj.get(key) or []), obj

    views, _ = _read("views.json", "views")
    catalog, _ = _read("catalog.json", "datasets")
    lineage, _ = _read("lineage.json", "views")
    sp = _locate("sources.json")
    with sp.open(encoding="utf-8") as fh:
        src_obj = json.load(fh)
    src_list = list(src_obj.get("sources") or [])

    # sources → 字典；同时建 path 索引
    sources: dict[str, dict[str, Any]] = {}
    sidx: dict[str, list[str]] = {}
    for s in src_list:
        sid = s["id"]
        rec = {k: v for k, v in s.items() if k != "datasets"}
        # 源自己声明的数据集清单改存为 raw_declared，不再是路径真源
        rec["declared_datasets"] = sorted((s.get("datasets") or {}).keys())
        sources[sid] = rec
        for _name, path in (s.get("datasets") or {}).items():
            sidx.setdefault(_norm_path(path), []).append(sid)

    cat_by_name = {d["name"]: d for d in catalog}
    lin_by_name = {l["view"]: l for l in lineage}

    datasets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _mk(rec: dict[str, Any], *, base: str) -> dict[str, Any]:
        did = str(rec["name"] if "name" in rec else rec["id"]).strip()
        rel = str(rec.get("path") or "").replace("\\", "/")
        abs_p = root / rel
        exists = abs_p.exists()

        cat = cat_by_name.get(did) or {}
        lin = lin_by_name.get(did) or {}

        gaps: list[dict[str, Any]] = []
        merge_day = _now_iso()[:10]
        for g in rec.get("gaps") or []:
            text = g if isinstance(g, str) else str(g)
            if text in _KNOWN_BAD_GAPS:
                continue  # D2 修复
            # verified_at = 录入日（合并当日确实打开过这张表），
            # inherited=True 表示「内容本身未复验」，check 会在 180 天后提醒
            gaps.append({"text": text, "verified_at": merge_day, "inherited": True})
        for g in lin.get("gaps") or []:
            text = g if isinstance(g, str) else str(g)
            gaps.append({"text": text, "verified_at": merge_day, "inherited": True, "from": "lineage"})

        sid, certainty, evidence = _infer_source(rel, sidx)
        edge: dict[str, Any] = {"source_id": sid, "evidence": [evidence], "certainty": certainty}
        if lin.get("evidence"):
            edge["evidence"] = list(lin["evidence"]) + [evidence]
        if lin.get("certainty") and certainty == "unknown":
            edge["certainty"] = "unknown"
        # lineage 独有的来源方式（migration/collected/derived/ingest）保留为备注
        if lin.get("source_kind"):
            edge["source_kind"] = lin["source_kind"]
        # 注意：不推断 raw_tables。sources.declared_datasets 里那些不是数据集 id
        # 的键（如 TRD_Dalyr）确实是原始表名，但一个源常挂十几张原始表，
        # 无法判定某张原始表喂给了哪个数据集 —— 硬挂会制造假血缘。
        # 原始表名保留在 sources[].declared_datasets 里，人工确认后再补。

        if not exists:
            status, why = "UNAVAILABLE", "合并时实测文件不存在"
        elif did in SNAPSHOT_IDS or any(h in rel.lower() for h in SNAPSHOT_PATH_HINTS):
            status, why = "SNAPSHOT_ONLY", "截面快照，不可当历史序列"
        elif rel.startswith("data/clean/") or base == "catalog":
            status, why = "DERIVED", f"派生表（合并自 {base}）"
        else:
            status, why = "AVAILABLE", "合并时实测文件存在"

        dr = rec.get("date_range") or cat.get("date_range")
        cov: dict[str, Any] = {}
        if isinstance(dr, (list, tuple)) and len(dr) == 2:
            cov = {"start": str(dr[0])[:10], "end": str(dr[1])[:10]}

        out: dict[str, Any] = {
            "id": did,
            "path": rel,
            "format": (rec.get("format") or cat.get("format") or (abs_p.suffix.lstrip(".") if rel else None)),
            "kind": rec.get("kind") or "dataset",
            "frequency": rec.get("frequency") or cat.get("frequency"),
            "caliber": rec.get("caliber"),
            "status": status,
            "status_reason": why,
            "coverage": cov,
            # 口径而非物理事实，保留
            "pit": {"anchor": cat.get("pit_anchor")} if cat.get("pit_anchor") else None,
            "refresh": cat.get("refresh"),
            "fetch_script": cat.get("fetch_script"),
            "symbol_column": rec.get("symbol_column", cat.get("symbol_column")),
            "date_column": rec.get("date_column", cat.get("date_column")),
            "lineage": [edge],
            "gaps": gaps,
            "consumers": [],
            "verified_at": "2026-11-21",
            "merged_from": base,
        }
        if rec.get("note"):
            out["note"] = rec["note"]
        return {k: v for k, v in out.items() if v is not None}

    for v in views:
        if v["name"] in seen:
            continue
        datasets.append(_mk(v, base="views"))
        seen.add(v["name"])
    for d in catalog:
        if d["name"] in seen:
            continue
        rec = dict(d)
        rec.setdefault("kind", "dataset")
        rec["status_reason_hint"] = "merged_from_catalog_only"
        datasets.append(_mk(rec, base="catalog"))
        seen.add(d["name"])
        datasets[-1]["status_reason"] = "catalog 独有条目，views 未登记，口径待补"

    for d in datasets:
        if d["id"] in cat_by_name and d.get("merged_from") == "views":
            d["merged_from"] = "views+catalog"

    # consumers：现场扫 run 目录反查「谁在用」，只作缓存，stat 时会重扫
    scanned_at = _now_iso()
    cons = scan_consumers(root)
    for d in datasets:
        d["consumers"] = cons.get(d["id"], [])
        d["consumers_scanned_at"] = scanned_at

    # 口径覆盖：从 spec/dataset_caliber.json 补 caliber 与 caveats
    apply_caliber_overrides(datasets)

    out = {
        "schema_version": 2,
        "spec_version": _spec_version(),
        "generated_at": _now_iso(),
        "generator": "qrg data init --from-legacy",
        "note": (
            "单表数据集登记。物理事实（行数/体积/字段）**不入档**，"
            "用 `qrg data stat <id>` 现场探测；consumers 为扫描缓存，"
            "以 `qrg data stat` 重扫结果为准。血缘 certainty=inferred 表示按路径前缀推断。"
        ),
        "data_root": str(root),
        "sources": sources,
        "datasets": sorted(datasets, key=lambda x: x["id"]),
    }
    return out


def _spec_version() -> str:
    """读 registry.spec.json 的版本号，保证 datasets.json 能自证遵循哪版契约。"""
    p = Path(__file__).resolve().parents[2] / "spec" / "registry.spec.json"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("spec_version") or "")
    except Exception:
        return ""


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")

