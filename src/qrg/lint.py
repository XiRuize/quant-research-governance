"""数据体检：把范式卡 P01–P11 里**能机器判定**的条款做成可执行检查。

为什么需要这个模块
------------------
范式卡记下了历史踩过的坑（`TRD_Dalyr` 同 `(Stkcd,Trddt)` 有 6–8 行完全重复，
不去重收益放大 6–8 倍；`yjbb` 中位滞后 396 天，禁用 NOTICE_DATE；月末必须取
最后一个交易日而不是自然月末）。但**卡片没人读**。下一个 AI 研究员拿到表就
开始算因子，坑会原样重踩一遍。

所以这里只做一件事：在算因子之前，把「这张表有什么毛病」直接报出来。

覆盖边界
--------
| 卡 | 可机器判 | 原因 |
|---|---|---|
| P01 网格化/停牌 | 部分（ffill 痕迹） | 网格完整性需全表对照交易日历 |
| P02 复权 | 是（价与因子配对） | |
| P03 涨跌停/ST | 是（标记全 0 / 全空、ST 单快照） | |
| P04 财务 PIT | 是（滞后中位数） | |
| P05 行业 PIT | 是（effective_from 存在性） | |
| P06 股票池/板块 | 是（非 A 股代码混入） | |
| P07 去重/单位 | 是（主键重复 + 量级） | |
| P08 时间对齐 | 是（月末是否交易日） | |
| P09 缩尾/中性化 | **否** | 属研究层，数据层无从检 |
| P10 执行成本 | **否** | 属研究层 |
| P11 前视黑名单 | 是（禁用字段 + 跨月不变指纹） | |

采样策略：单表超过 `MAX_SCAN_ROWS` 时按 row group 均匀采样，
取首/中/尾三段，避免「只看到最早三年」的偏差。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .registry import Finding, Registry, _pick

MAX_SCAN_ROWS = 4_000_000

# 字段名模式 → 语义列。按模式识别而非按表名，新表也能被检。
_CODE_HINTS = ("stkcd", "symbol", "security_code", "code")
# 绝不作为标的列：币种/项目/基金代码都不是股票标的
_CODE_EXCLUDE = ("currency", "item", "master", "fund", "industry", "board", "exchange")
# 顺序即优先级：主键用的时间列必须排在前面，
# 否则 vendor_vintage_date / update_date 这类「含 date 但不是主键」的列会被误选
_DATE_HINTS = (
    "trade_date", "trddt", "month_end", "effective_from",
    "reportdate", "report_date", "snapshot_date", "date",
)
# 绝不作为主轴的时间列：供应商版本日与更新日都不是数据时点
_DATE_EXCLUDE = ("vintage", "update_date", "updated", "fetch", "crawl", "created")
_MONEY_HINTS = ("dsmvosd", "dsmvtll", "dnvaltrd", "amount", "amt", "turnover_value", "total_mv", "circ_mv")

# P11：出现过前视事故的字段名
FORBIDDEN_FIELDS = {
    "update_date": "财务表 UPDATE_DATE（数据商更新时刻，非公告时刻，含未来信息）",
    "limitstatus": "LimitStatus（实测 2016 年后全为 0，不可用作涨跌停判定）",
}
# 龙虎榜 D1..D30 是未来收益列，出现在任何数据集里都是事故
FORBIDDEN_PATTERN = "^d([1-9]|[12][0-9]|30)$"


@dataclass
class LintResult:
    dataset: str
    path: str
    rows_scanned: int
    sampled: bool
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def worst(self) -> str:
        lv = {f.level for f in self.findings}
        if "fatal" in lv:
            return "fatal"
        if "warn" in lv:
            return "warn"
        return "ok"


# ---------------------------------------------------------------- 读取


def _iter_sample(path: Path, cols: Sequence[str], *, max_rows: int = MAX_SCAN_ROWS):
    import pandas as pd
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    total = pf.metadata.num_rows
    names = set(pf.schema_arrow.names)
    cols = [c for c in cols if c in names]
    if not cols:
        return pd.DataFrame(), 0, False
    if total <= max_rows:
        return pq.read_table(path, columns=cols).to_pandas(), total, False

    groups = pf.num_row_groups
    per = max_rows // 3
    picks: list[int] = []
    for anchor in (0, groups // 2, groups - 1):
        if anchor not in picks:
            picks.append(anchor)
    frames = []
    got = 0
    for g in picks:
        tbl = pf.read_row_group(g, columns=cols)
        if tbl.num_rows > per:
            tbl = tbl.slice(0, per)
        frames.append(tbl.to_pandas())
        got += tbl.num_rows
    import pandas as pd2

    return pd2.concat(frames, ignore_index=True), got, True


def _calendar_days(reg: Registry) -> set[str] | None:
    """交易日历（只取 is_trading_day=True 的日期，字符串 YYYY-MM-DD）。"""
    try:
        rec = reg.get("calendar")
    except KeyError:
        return None
    p = reg.data_root / str(rec["path"]).replace("\\", "/")
    if not p.exists():
        return None
    import pandas as pd

    df = pd.read_parquet(p, columns=None)
    col = _pick(df.columns, ("trade_date",))
    if col is None:
        return None
    if "is_trading_day" in df.columns:
        df = df[df["is_trading_day"]]
    return {str(x)[:10] for x in df[col].dropna()}


# ---------------------------------------------------------------- 检查


def _month_end_dates(s: "Any") -> set[str]:
    import pandas as pd

    d = pd.to_datetime(s, errors="coerce").dropna()
    if not len(d):
        return set()
    d = d.dt.normalize()
    # 每月最后一天（自然月末）；对齐检查就看它是否落在交易日
    return {str(x)[:10] for x in d[d.dt.is_month_end].unique()}


def _pick_ex(cols: Iterable[str], hints: tuple[str, ...], exclude: tuple[str, ...] = ()) -> str | None:
    """先剔除排除项再选列，避免 vendor_vintage_date 抢走主键时间轴。"""
    cands = [c for c in cols if not any(x in c.lower() for x in exclude)]
    return _pick(cands, hints)


def lint_dataset(
    reg: Registry,
    dataset_id: str,
    *,
    calendar: set[str] | None = None,
    max_rows: int = MAX_SCAN_ROWS,
) -> LintResult:
    """对单个数据集跑体检。返回的 findings 带 P 卡编号便于溯源。"""
    rec = reg.get(dataset_id)
    rel = str(rec["path"]).replace("\\", "/")
    p = (reg.data_root / rel).resolve()
    res = LintResult(dataset=dataset_id, path=rel, rows_scanned=0, sampled=False)

    out = _lint_dataset_inner(reg, rec, dataset_id, p, rel, res, calendar=calendar, max_rows=max_rows)
    _suppress_registered(out, rec)
    return out


def _suppress_registered(res: "LintResult", rec: dict) -> None:
    """闭环语义：缺陷已在登记表 known_limitations 留痕（带【qrg data lint】标签 +
    同 P 卡编号）→ fatal/warn 降为 info。数据修复重派生后登记条目应删除，lint 自动转红。"""
    kls = rec.get("known_limitations") or []
    tags = {k for kl in kls if isinstance(kl, str) and "【qrg data lint" in kl
            for k in ("P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10", "P11") if k in kl}
    if not tags:
        return
    for f in res.findings:
        pcode = f.code.split("-")[1] if f.code.startswith("L-P") else None
        if pcode in tags and f.level in ("fatal", "warn"):
            f.level = "info"
            f.message += "（已知缺陷：已在登记表 known_limitations 留痕；数据修复后删除登记条目，本项自动转红）"


def _lint_dataset_inner(
    reg: Registry,
    rec: dict,
    dataset_id: str,
    p: Path,
    rel: str,
    res: "LintResult",
    *,
    calendar: set[str] | None = None,
    max_rows: int = MAX_SCAN_ROWS,
) -> "LintResult":

    if not p.exists():
        res.findings.append(Finding("fatal", "L-PATH", dataset_id, f"文件不存在: {rel}"))
        return res
    if p.suffix.lower() != ".parquet":
        res.skipped.append(f"非 parquet（{p.suffix}），跳过结构体检")
        res.findings.append(
            Finding("info", "L-FORMAT", dataset_id, f"{p.suffix} 文件，只做存在性与体积核对")
        )
        return res

    import pyarrow.parquet as pq

    schema_names = list(pq.ParquetFile(p).schema_arrow.names)
    df, n, sampled = _iter_sample(
        p, [c for c in schema_names if _needed(c, schema_names)], max_rows=max_rows
    )
    res.rows_scanned, res.sampled = n, sampled
    if df.empty:
        res.skipped.append("无可用列")
        return res

    code_c = _pick_ex(df.columns, _CODE_HINTS, _CODE_EXCLUDE)
    date_c = _pick_ex(df.columns, _DATE_HINTS, _DATE_EXCLUDE)
    is_snapshot = rec.get("status") == "SNAPSHOT_ONLY"
    # 价格时序表：同一 (标的, 日期) 只应有一行。事件表（董事名单、财务报表、
    # 违规记录）同一天天然多条，(code,date) 不是主键，重复是正常的。
    _price_like = {"opnprc", "clsprc", "close", "open", "adjprcwd", "adjprcnd", "hiprc", "loprc"}
    n_price = len({c.lower() for c in schema_names} & _price_like)
    is_price_series = n_price >= 2

    # ---- P11 禁用字段 -----------------------------------------
    import re

    for c in schema_names:
        lc = c.lower()
        if lc in FORBIDDEN_FIELDS:
            res.findings.append(Finding("warn", "L-P11-FIELD", dataset_id, f"含禁用字段 {c}：{FORBIDDEN_FIELDS[lc]}"))
        if re.match(FORBIDDEN_PATTERN, lc):
            res.findings.append(Finding("fatal", "L-P11-LOOKAHEAD", dataset_id, f"含前视收益列 {c}（龙虎榜 D1..D30）"))

    # ---- P07 主键唯一 -----------------------------------------
    if code_c and date_c:
        dup = int(df.duplicated(subset=[code_c, date_c]).sum())
        if dup:
            rate = dup / max(len(df), 1)
            if is_price_series:
                # 行情表重复是硬事故：不去重收益会放大数倍
                lvl = "fatal" if rate > 0.01 else "warn"
                res.findings.append(
                    Finding(
                        lvl, "L-P07-DUP", dataset_id,
                        f"价格表 ({code_c},{date_c}) 重复 {dup:,} 行（{rate:.2%}）；"
                        "不去重会放大收益，须先 drop_duplicates",
                    )
                )
            else:
                # 事件表/多记录表：(code,date) 本来就不是主键
                res.findings.append(
                    Finding(
                        "info", "L-P07-COMPOSITE", dataset_id,
                        f"({code_c},{date_c}) 非唯一，{rate:.1%} 行重复 —— 本表为事件/多记录表，"
                        "每期多条属正常；当坐标用必须先按业务键聚合，不可直接当时序",
                    )
                )
    # ---- P06 股票池：非 A 股代码混入 --------------------------
    if code_c:
        s = df[code_c].astype(str).str.strip()
        bad = s.str.match(r"^(8|4|920)")
        n_bad = int(bad.sum())
        if n_bad:
            rate = n_bad / max(len(s), 1)
            uniq = sorted(set(s[bad]))[:5]
            # 占比高说明是「表本身就混了非 A 股」（事故）；
            # 占比低说明是「全市场表恰好含少量北交所」（正常）
            lvl = "warn" if rate > 0.05 else "info"
            res.findings.append(
                Finding(
                    lvl, "L-P06-UNIVERSE", dataset_id,
                    f"{n_bad:,} 行（{rate:.1%}）非 A 股代码（8xx/4xx/920 开头，新三板/北交所），"
                    f"例 {uniq}；若研究对象为 A 股须先按 board 过滤",
                )
            )

    # ---- P08 月末是否落在交易日 -------------------------------
    # 快照表只有一个时点，做月末对齐检查无意义（会 100% 假阳性）
    if calendar is not None and date_c and not is_snapshot:
        me = _month_end_dates(df[date_c])
        if len(me) >= 3:
            miss = sorted(d for d in me if d not in calendar)
            # 自然月末落在周末是常态；真正的问题是「月末值根本不是任何交易日」
            if miss and len(miss) == len(me):
                res.findings.append(
                    Finding(
                        "fatal", "L-P08-ALIGN", dataset_id,
                        f"{len(me)} 个月末日期**全部**不在交易日历内，时间轴对齐错误",
                    )
                )
            elif miss:
                res.findings.append(
                    Finding(
                        "info", "L-P08-ALIGN", dataset_id,
                        f"{len(miss)}/{len(me)} 个月末是自然月末（落在非交易日），"
                        "若下游按自然月末取数会丢月份，应改用该月最后一个交易日",
                    )
                )

    # ---- P03 涨跌停标记失效 -----------------------------------
    for c in ("LimitUp", "LimitDown", "LimitStatus"):
        if c in df.columns:
            col = df[c]
            if col.isna().all():
                res.findings.append(
                    Finding("warn", "L-P03-LIMIT", dataset_id, f"{c} 全为空，涨跌停不可识别")
                )
            elif c == "LimitStatus" and (col.fillna(0) == 0).all():
                res.findings.append(
                    Finding("fatal", "L-P03-LIMIT", dataset_id, "LimitStatus 全为 0，用它判涨跌停会全部放行")
                )

    # ---- P04 财务 PIT 滞后 ------------------------------------
    rep = _pick(df.columns, ("reportdate", "report_date", "report_period"))
    notice = _pick(df.columns, ("notice_date", "announce_date"))
    if rep and notice:
        import pandas as pd

        lag = (pd.to_datetime(df[notice], errors="coerce") - pd.to_datetime(df[rep], errors="coerce")).dt.days
        lag = lag.dropna()
        if len(lag):
            med = float(lag.median())
            pct = float((lag > 365).mean())
            if med > 365:
                res.findings.append(
                    Finding(
                        "fatal", "L-P04-PIT", dataset_id,
                        f"{notice}-{rep} 中位滞后 {med:.0f} 天（{pct:.0%} 超 365 天）；"
                        "NOTICE_DATE 不可用，须改 report_period+90 并标 is_proxy",
                    )
                )
            elif pct > 0.1:
                res.findings.append(
                    Finding(
                        "warn", "L-P04-PIT", dataset_id,
                        f"{pct:.0%} 的 {notice} 滞后超 365 天（中位 {med:.0f} 天），核对是否混入补发报表",
                    )
                )

    # ---- P05 行业 PIT -----------------------------------------
    if "industry" in dataset_id or "sw_l1" in " ".join(schema_names):
        has_eff = any(c in schema_names for c in ("effective_from", "start_date"))
        has_vint = any("vintage" in c for c in schema_names)
        if not has_eff:
            res.findings.append(
                Finding("warn", "L-P05-PIT", dataset_id, "行业表无 effective_from，无法 as-of 回溯，不得直接入模")
            )
        elif not has_vint:
            res.findings.append(
                Finding("info", "L-P05-PIT", dataset_id, "有 effective_from 但无 vendor_vintage_date，无法证明无前视")
            )

    # ---- P11 industry_name 跨月不变（前视指纹）-----------------
    if "industry_name" in df.columns and code_c and date_c:
        g = df.groupby(code_c)["industry_name"].nunique(dropna=True)
        if len(g) and (g <= 1).mean() > 0.95:
            res.findings.append(
                Finding(
                    "fatal", "L-P11-INDUSTRY", dataset_id,
                    f"{(g <= 1).mean():.0%} 的标的行业名跨期不变 —— 典型前视指纹"
                    "（历史行业分类被当前分类覆盖），不可用于回测",
                )
            )

    # ---- P01 ffill 痕迹 ---------------------------------------
    if code_c and date_c:
        import pandas as pd

        num_cols = [
            c for c in df.columns
            if c not in (code_c, date_c) and pd.api.types.is_numeric_dtype(df[c])
        ][:6]
        if num_cols:
            df2 = df.sort_values([code_c, date_c])
            same_prev = df2[num_cols].diff().eq(0).all(axis=1)
            same_group = df2[code_c].eq(df2[code_c].shift())
            rate = (same_prev & same_group).groupby(df2[code_c]).mean()
            if len(rate) and float(rate.median()) > 0.9:
                res.findings.append(
                    Finding(
                        "warn", "L-P01-FFILL", dataset_id,
                        f"中位 {float(rate.median()):.0%} 的相邻行为完全同值，"
                        "疑似 ffill 或非交易日填充；停牌应区分三态而非复制前值",
                    )
                )

    # ---- P07 单位量级（仅报告，供人工判）----------------------
    for c in df.columns:
        if c.lower() in _MONEY_HINTS:
            s = df[c].dropna()
            if len(s) and s.median() and 1e3 < s.median() < 1e7:
                res.findings.append(
                    Finding(
                        "info", "L-P07-UNIT", dataset_id,
                        f"{c} 中位 {s.median():,.0f}，疑似千元单位（CSMAR Dsmvosd 为千元），换算前先确认",
                    )
                )

    # ---- P02 复权配对 -----------------------------------------
    price_cols = [c for c in schema_names if c.lower() in ("clsprc", "close", "open", "opnprc")]
    adj_cols = [c for c in schema_names if "adj" in c.lower() or "factor" in c.lower()]
    if price_cols and not adj_cols:
        res.findings.append(
            Finding(
                "warn", "L-P02-ADJUST", dataset_id,
                f"有原始价 {price_cols[:2]} 但无复权因子列，跨除权日收益会断",
            )
        )

    return res


def _needed(col: str, all_cols: Sequence[str]) -> bool:
    """只为检查需要的列付费，避免把 24 列全表读进内存。"""
    lc = col.lower()
    keep = (
        lc in _MONEY_HINTS
        or lc in FORBIDDEN_FIELDS
        or lc in ("limitup", "limitdown", "limitstatus", "industry_name",
                  "effective_from", "start_date", "notice_date", "announce_date",
                  "reportdate", "report_date", "report_period", "vendor_vintage_date")
        or any(h in lc for h in _CODE_HINTS + _DATE_HINTS)
        or "vintage" in lc
        or lc in ("clsprc", "close", "open", "opnprc")
    )
    if keep:
        return True
    # 复权因子列（adj/factor）
    return "adj" in lc or "factor" in lc


def lint_all(
    reg: Registry,
    *,
    ids: Iterable[str] | None = None,
    pattern: str | None = None,
    max_rows: int = MAX_SCAN_ROWS,
) -> list[LintResult]:
    cal = _calendar_days(reg)
    if ids:
        targets = list(ids)
    elif pattern:
        targets = reg.ids_matching(pattern)
    else:
        targets = [d["id"] for d in reg.datasets]
    out = []
    for i in targets:
        try:
            out.append(lint_dataset(reg, i, calendar=cal, max_rows=max_rows))
        except KeyError:
            out.append(
                LintResult(i, "", 0, False, findings=[Finding("fatal", "L-ID", i, "未登记")])
            )
    return out


def summarize(results: Sequence[LintResult]) -> dict[str, Any]:
    by_level = {"fatal": 0, "warn": 0, "info": 0}
    codes: dict[str, int] = {}
    for r in results:
        for f in r.findings:
            by_level[f.level] = by_level.get(f.level, 0) + 1
            codes[f.code] = codes.get(f.code, 0) + 1
    return {
        "datasets": len(results),
        "findings": by_level,
        "by_code": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "clean": [r.dataset for r in results if r.worst == "ok"],
        "fatal_datasets": [r.dataset for r in results if r.worst == "fatal"],
    }


def render(results: Sequence[LintResult], *, level: str = "warn") -> str:
    order = {"fatal": 0, "warn": 1, "info": 2}
    thresh = order[level]
    lines: list[str] = []
    s = summarize(results)
    lines.append(
        f"lint data: {s['datasets']} 个数据集 | fatal {s['findings']['fatal']} / "
        f"warn {s['findings']['warn']} / info {s['findings']['info']}"
    )
    if s["by_code"]:
        lines.append("按检查项: " + ", ".join(f"{k}×{v}" for k, v in s["by_code"].items()))
    lines.append("")
    for r in results:
        hits = [f for f in r.findings if order[f.level] <= thresh]
        if not hits:
            continue
        sample = "（采样）" if r.sampled else ""
        lines.append(f"── {r.dataset}  [{r.rows_scanned:,} 行{sample}]  {r.path}")
        for f in sorted(hits, key=lambda x: order[x.level]):
            tag = {"fatal": "FAIL", "warn": "WARN", "info": "INFO"}[f.level]
            lines.append(f"   [{tag}] {f.code}: {f.message}")
    if s["clean"]:
        lines.append("")
        lines.append(f"无问题（{len(s['clean'])}）: " + ", ".join(s["clean"][:20]) + (" ..." if len(s["clean"]) > 20 else ""))
    return "\n".join(lines)
