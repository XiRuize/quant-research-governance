# P02 复权：存 raw + factor，qfq 派生

**一句话**：价格永远存原始价 + 逐日复权因子，前复权价是派生量；任何跨源拼接先归一到同一基准，否则禁止拼价格水平。

## 触发

- 入库任何价格序列；
- 拼接两个不同来源的价格（如历史 qfq + 新浪不复权新窗口）；
- 拿价格水平做排序以外的任何计算（收益率、价差、比率）。

## 规则

1. 存储形态：`close_raw` + `factor`，`factor = close_hfq / close_raw`（上市累计、非递减、除权除息处跳变）。
2. 任意基准复权：`adj(b) = close_raw × factor / factor[b]`。基准日固定 **2026-07-31**，理由是与既有 `daily_factors` 对齐，保证历史结论可对比。
3. **跨源拼接禁止直接拼价格水平**。必须先把新源价格乘 factor 归一到同一基准，再拼。理由：不同源的不复权口径不同（新浪 raw vs CSMAR Clsprc），直接拼会在拼接点造出假跳空。
4. 跳空识别：`|return| > 该板块涨跌停幅度 + 2pp` 计为 `adj_jump`。阈值不内联，读 `hard_validation`。
5. 复权因子不得前向填充到「未来才除权」的日子；按 as-of 取当日已生效的因子。

## 反例

- 曾只存前复权价，导致基准日一改，全库要重算，且无法回答「当时看到的价格是多少」。
- 曾把新浪不复权新窗口直接接到历史 qfq 上，拼接日出现 30% 假跌幅。
- 用行情 `Clsprc` 当收益源而不配 `TRD_NoLimit`，`adj_jump` 高达 0.0033%；配上后降到 **0.00035%**（−89.5%）。

## 证据

- 推导 qfq vs 既有 qfq：中位相对误差 **2.1e-6**（600519），全局 ≤ 1e-3
- 事件法拼接：877 事件 / 857 只，反验 **4.7e-05**；CSMAR 续接首日 `close` **5196/5196 完全一致**，中位收益差 2.5e-07（`BEAR-20261121-001/run.json`）
- `research/00_infrastructure/runs/CLN-CSMAR-COMPARE/`：Dretwd + TRD_NoLimit 对比
- 契约块：`price_adjustment`

## 检查

```python
# 因子非递减、无缺失
f = df.sort_values(["code","trade_date"]).groupby("code")["factor"]
assert (f.diff().dropna() >= -1e-9).all()
# 拼接点无异常跳空
assert df.loc[stitch_dates, "ret"].abs().max() < 0.11
```

## 状态

`PARTIAL` — 冻结窗口用既有 qfq（自洽）；2026-08 之后用事件法增量拼接（已验证）。**全量逐日 factor 仍需 DQ2 数据源**，任意基准复权重算目前只能靠事件法覆盖的部分。
