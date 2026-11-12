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
| 网格（116,755 行） | 2.14s | **1.49s** |
| 端到端（含 normalize/lookahead） | ~11s | ~11s |

端到端没有明显下降，因为瓶颈已经转移到 `normalize`（约 3.5s）与 `lookahead` 回放（约 2.8s）——下一步要优化这两处，才谈得上“全市场十年”。

### 无涨跌幅限制日（TRD_NoLimit）

- 9 月共 63 个 (code, day) 无限制记录；
- 网格新增 `no_limit` 标记，当日 `limit_pct=inf`、不做锁死判定，审计输出 `GRID.no_limit_days`（INFO）。

### 停复牌区间（TSR_Stkstat）

- 9 月 47 段停牌区间（Type=1），覆盖 201 个 (code, day)；
- 网格新增 `suspension_announced`：即使当天有行情行，也按公告停牌处理；
- `suspended = 缺行 ∪ 公告停牌`，审计输出 `GRID.suspension_announced`（INFO）。

9 月停牌从 167 行升到 201 行——多出来的不是噪声，是“有行但公告停牌”的部分。
