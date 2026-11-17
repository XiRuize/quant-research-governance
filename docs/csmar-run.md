# CSMAR 源接入记录（2026-09，含北交所与 ST proxy）

上一轮用新浪 `new_bars` 跑出来三个缺口（北交所缺失、ST 未知、无复权）。这一轮改用 CSMAR 原始表，一次性验证三件事。

## 输入

| 输入 | 来源 | 规模 |
|---|---|---|
| 面板 | `external/csmar/行情/TRD_Dalyr` | 2026-09：116,588 行 / 5,571 只（**含 348 只北交所 92x**） |
| 主数据 | `data/ref/stock_basic` | 上市日；退市用 `LISTING_STATE=2` + 最后交易日 proxy |
| ST | `行情/TRD_StockTrend.ShortName` | 简称含 `ST/*ST` → `st_on/st_off` 事件（**proxy**） |
| 复权 | `行情/TRD_AdjustFactor.CumulateBwardFactor` | 37,697 行（补了无事件代码的 1.0 锚点） |
| 行业 | `data/ref/industry_pit_sw` | SW2021 as-of |

## 结果对比

| 指标 | 新浪 new_bars（v0.4.1） | CSMAR TRD_Dalyr（v0.4.2） |
|---|---|---|
| universe | 5,195（无北交所） | **5,571（348 只北交所）** |
| 网格行数 | 116,860 | 116,755 |
| 停牌 | 7,972（6.82%） | **167（0.143%）** |
| 覆盖缺口 | 342 只（`GRID.master_coverage_gap`） | **0** |
| ST 状态 | unknown 100% | **proxy 100%（287 只 ST）** |
| 涨跌停歧义 | 2,695 行 | **0**（proxy 已定位 5% 限价） |
| 复权 | 跳过（无因子） | 完成，仅 22 只 `base_proxy` |
| 行业未知 | 4 行 | 4 行 |
| 零前视回放 | 通过 | 通过 |

停牌率从 6.82% 降到 0.143%——不是市场变了，是**北交所回来了、退市不再被当成永久停牌**。

## ST proxy 的口径

- 规则：`ShortName` 命中 `^*ST` / `^ST` / `^S*ST` 视为 ST；名称切换日 = 事件日；
- 覆盖：窗口前 400 天回看，287 只出现过 ST 名称；
- 与快照的差异：9 月在册 ST 317 只（快照 794 只含退市），proxy 287 只。差在名称缺失/改名滞后，**proxy 只能做限价判定的提示，不能当权威**；
- 网格对 proxy 行：`is_st_proxy=True`，按 5% 限价判定，并输出 `GRID.st_proxy`（WARN）。审计里仍然可见。

## 复权

`TRD_AdjustFactor` 的 `CumulateBwardFactor` 逐股单调递增，直接作为 `factor` 喂给 `adjust`：
- 除息日方向验证（前次窗口）：000019 原始 -0.97% → 复权后 +0.99%；
- 本轮 22 只 `ADJ.base_proxy` 全部是 2026-07-31 之后上市的新股，属于预期，不是 bug；
- 无公司行动的代码锚定 `factor=1.0`（无事件 → 因子恒定，不产生跳变）。

## 仍未闭合

- **ST 权威历史**：CSMAR 已购 653 表里没有“特别处理”表，proxy 是当前最优；
- **退市日期**：仍用最后交易日 +1 天 proxy；
- **北交所行业**：SW 分类对 92x 覆盖差，行业未知 4 行是低估的；
- **性能**：网格逐行 `master.state()`，11.7 万行约 11 秒。

## 复现

```powershell
$env:PYTHONPATH = "src"
python examples/csmar_prepare.py `
  --csmar-root "C:/.../量化研究/数据源/external/csmar" `
  --data-root  "C:/.../量化研究/数据源/data" `
  --start 2026-09-01 --end 2026-09-30 --out examples/_real/csmar_sept

python -m qrg clean --spec spec/cleaning.spec.json `
  --input examples/_real/csmar_sept/panel.parquet `
  --factors examples/_real/csmar_sept/factors.parquet `
  --sessions examples/_real/csmar_sept/sessions.csv `
  --security-rules examples/_real/csmar_sept/security.json `
  --industry-rules examples/_real/csmar_sept/industry.json `
  --out examples/_real/csmar_sept/out
```

## v0.5：性能与制度日历补全

### 网格向量化

- `SecurityMasterImpl.state_arrays()`：按 (code, day) 逐对计算，不建 DataFrame、不做 merge；
- `RegimeCalendar.limit_frame()`：按板块/生效区间 searchsorted，ST 与新股无限制向量化；
- 网格自动选择向量路径，缺任一能力时退回逐行循环（协议兼容）。

| 场景 | 优化前 | 优化后 |
|---|---|---|
| 网格（116,755 行，≤2s 目标） | 2.14s | **1.49s** |
| normalize（116,588 行） | 3.53s | **2.25s** |
| 端到端（含 lookahead 回放） | ~11s | **9.5s** |

`normalize` 通过“已在 tick 网格上的值跳过 Decimal 往返”降到 2.25s；端到端约 8.5s，瓶颈是 `lookahead` 回放。
新增 `--replay-step` 抽样（step=5 时端到端 5.7s），但抽样式审计弱于全量回放。向量路径与逐行路径的一致性由
`tests/test_grid_equivalence.py` 保证（同输入逐值相等）。

## v0.6.1：退市、逐日 ST、财务 PIT、市值接入

| 输入 | 现状 | 2026-09 实测 |
|---|---|---|
| `DERIVED_delist_dates.parquet` | 387 条，多源交叉，带 `date_conflict_days` | 4 条真实退市、0 proxy；600018 早期生命周期冲突跳过 |
| `TRD_StockTrend` 日频简称 | 11,469,614 行、2016–2026 日频 `ShortName` | 生成 **6,699 条 `st_on/st_off`**（585 只曾 ST），全部 proxy；同日简称变化不再等年度 |
| `financial_pit.parquet` | `NOTICE_DATE` 2018-04..2026-10，7 个科目 | 长表 **1,077,236 行**；PIT 覆盖率 **99.98%**（缺 24 行） |
| `market_cap.parquet` | 2018-01..2026-10 | 9 月 **109,221 行**；`panel.parquet` 里 `total_mv/circ_mv/pe_ttm/pb` 覆盖 109,014 / 116,588 行 |

产物新增：

- `vintages.json`：每个输入的 source / rows / min / max / snapshot_date；
- `data_coverage.json`：面板窗口 + 各输入覆盖区间；主数据来自单日快照时记 `COV.single_vintage_snapshot` (WARN)；
- `panel.parquet`：仅在给 `--market-cap` 时写出（含行业、财务 PIT、市值列）。

2026-09 重跑：网格 116,588 行、停牌 241 行（含公告停牌）、`is_st=True` 4,252 行（全部 proxy）、行业未知 4 行。

仍未闭合：ST 仍是**简称派生**（proxy），不是交易所正式特别处理事件；`stock_basic` 只有单日快照，历史 universe 仍有幸存者偏差；财务字段只有 7 个科目、2018 年前无数据。



### 无涨跌幅限制日（TRD_NoLimit）

- 9 月共 63 个 (code, day) 无限制记录；
- 网格新增 `no_limit` 标记，当日 `limit_pct=inf`、不做锁死判定，审计输出 `GRID.no_limit_days`（INFO）。

### 停复牌区间（TSR_Stkstat）

- 9 月 47 段停牌区间（Type=1），覆盖 201 个 (code, day)；
- 网格新增 `suspension_announced`：即使当天有行情行，也按公告停牌处理；
- `suspended = 缺行 ∪ 公告停牌`，审计输出 `GRID.suspension_announced`（INFO）。

9 月停牌从 167 行升到 201 行——多出来的不是噪声，是“有行但公告停牌”的部分。

## v0.6.2：30 年行情、20 年因子、指数成分

数据源补下载了长历史与指数表：

| 表 | 现状 | 用途 |
|---|---|---|
| `TRD_Dalyr` | **1996-01-02..2026-10-30，18,839,260 行**（原 2016 起） | 面板可回溯到 1996 |
| `TRD_AdjustFactor` | **2006-01-04..2026-10-30，54,317 行**（原 2016 起） | 复权覆盖 20 年 |
| `IDX_Smprat` | 7,384,082 行，11 个指数，2005-01..2026-11 日频成分权重 | 指数成分/权重 as-of |
| `IDX_Chgsmp` | 13,410,883 行，5,040 个指数，含 000300/000905 调整记录 | 未接入（字段编码需字典，且仅 2015–2019） |

适配器改为**优先读 `data/ref/csmar` 整合版**（含长历史），缺失才回退 `external` 原始表。

### 2010–2012 回归

```
universe 2,469 / panel 1,508,720 行 / clean 123s / gate warn
```

- 退市：236 条真实日期（0 proxy）；
- 复权：33,711 行；**4 只股票的后复权因子在长历史里下降**（000887 / 600094 / 600145 / 600381），
  不做修复，直接剔除并记 `factor_dropped_nonmonotonic=4`，由 `ADJ.base_proxy` 兜底；
- ST：`TRD_StockTrend` 仍从 2016 起，2010 窗口 **全部 `is_st_unknown`（1,566,547 行）**，
  `GRID.limit_ambiguous` 78,260 行——这是诚实结果，不拿当前 ST 反推历史；
- 财务：2018 起，窗口内 100% 缺失，`PIT.financial_missing` 如实记录；
- 指数：`index_weights.parquet` 1,373,000 行 / 6 个指数（含 400 天 lookback）。

### 指数成分 API

新增 `qrg.master.index_members.IndexConstituents`：

```python
cal = IndexConstituents.from_parquet("index_weights.parquet")
cal.members("000906", "2011-01-05")      # as-of，不前视
cal.weight_of("000906", "2011-01-05", "000001")
```

限制：`IDX_Smprat` 只覆盖 11 个指数（含 000906 中证800、000010 上证180 等），**不含 000300/000905 的完整权重史**；
`IDX_Chgsmp` 可补成分变更，但字段编码未定且仅到 2019，暂不接入。