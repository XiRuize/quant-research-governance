# 代码 review 与修复（v0.4.1）

触发：2026-09 真实数据接入后发现“规则没崩，但结论不可信”。这一轮把 review 和真实数据放在一起做，找出的都是**单元测试用合成数据抓不到**的缺陷。

## 六个缺陷（都已修 + 补回归测试）

### 1. `build_factor` 多标的崩溃（F3）

`merge_asof` 要求两侧按 `on` 键**全局**有序，原实现只按 `["code","trade_date"]` 排序，多标的真实数据直接抛
`ValueError: left keys must be sorted`。合成测试只有单标的，从未触发。

修复：两侧改为按 `["trade_date","code"]` 排序。回归：`tests/test_actions_multi.py`。

### 2. `adjust_panel` 的 datetime 精度不匹配（B2）

真实 parquet 的 `trade_date` 是 `datetime64[us]`，因子表经 `pd.to_datetime` 变成 `datetime64[ns]`，
`merge_asof` 抛 `MergeError: incompatible merge keys`。

修复：两侧统一 `.astype("datetime64[ns]")`。

### 3. 停牌后复牌首日判不出涨跌停（B3）

原 `prev_close = close.shift(1)`：停牌日 `close` 是 NaN，复牌首行的 prev_close 也是 NaN，涨跌停永远不触发。

修复：`close.ffill().shift(1)`——只对派生列取“上一有效收盘”，不改数据本身。回归：`test_prev_close_survives_suspension`。

### 4. 面板有、主数据没有的代码被静默丢弃（B3）

网格由 `master.listable()` 驱动，面板里的幽灵代码不会进网格，也没有任何提示。

修复：新增 `GRID.panel_code_missing`（WARN，列出前 20 个代码）。

### 5. 主数据有、面板没有 = 被当成停牌（B3）

北交所 92x 代码不在 `new_bars` 覆盖内，网格把它们记成整月停牌，停牌率被虚增。

修复：新增 `GRID.master_coverage_gap`（WARN，按代码计数）与 `GRID.low_daily_coverage`（L2，逐日覆盖率 <50% 触发）。
后者在 2026-06 上抓到真实问题：`new_bars` 6 月 1 日只覆盖 7 只，6 月 18 日才恢复到 5,000+，整段被网格当成“全市场停牌”。

### 6. ST 未知被静默当成“非 ST”（B3）

`is_st=False` 既可能是“确认不是 ST”，也可能是“没有 ST 历史”。主数据没有 `st_on/st_off` 事件时，
5% 涨跌停会被按 10% 处理，锁死票被当成可交易。

修复：
- `SecurityState.st_status`（`known` / `unknown`）；
- 网格新增 `is_st_unknown`、`limit_ambiguous`：价格落在 ST 与板块限价之间时标记“无法归类”；
- 审计新增 `GRID.st_unknown`（WARN）与 `GRID.limit_ambiguous`（L2）；
- 单边价 + 触到任一规则限价 → 仍判不可交易（保守）。

## 真实数据复核（2026-09）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 网格行数 | 123,956 | 116,860 |
| 停牌率 | 12.16% | 6.82% |
| 其中覆盖缺口（北交所 92x 等） | 未区分 | 7,805 行 / 381 只 |
| 真实停牌（剔除缺口） | — | 约 0.14% |
| 退市代码 | 336 只永久停牌 | 336 只剔除、4 只 proxy 退市 |
| ST 未知 | 静默 0 | 116,860 行显式标记 |
| 涨跌停歧义 | 0 | 2,695 行（L2） |

2026-06-18 至 07-31 的复权复核：
- 1,349 只现金分红事件，`adjust` 无异常；
- 除息日方向正确：000019 原始收益 -0.97%，复权后 +0.99%（每股派 0.12）；
- 全样本最大单日复权收益 26.6%，来自科创板新股（前 5 日无涨跌幅），不是因子错误。

## 仍然存在的边界

> 以下为 v0.4.1 当时的状态；v0.6.1 已接入 `DERIVED_delist_dates` 与 `DERIVED_st_history`，
> 退市日期与 ST 年度史已不再是 proxy 之外的空缺（详见 [`csmar-run.md`](csmar-run.md)）。

- **退市日期是 proxy**（最后交易日 + 1 天），因为当时 `stock_basic` 没有退市日期；
- **ST 仍无历史**，只能显式标 unknown，不能补；
- **北交所覆盖缺口**属于数据源问题，库只能报告，不能修复；
- 网格逐行 `master.state()` 在 17 万行约 6 秒，全市场十年会到小时级，需要向量化。
