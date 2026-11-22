# P01 网格化与停牌三态

**一句话**：面板必须重建到「交易日 × 在册股票」的全网格，停牌用显式标记表达，不用「行缺失」表达。

## 触发

任何行情/因子面板入库或入模之前。

## 规则

1. 网格键 = `(trade_date, code)`，`trade_date` 取交易日历（`ref/calendar/calendar.parquet`，8,797 日），不取自然日。
2. 每个单元格必须落到四态之一，不得留空：
   - `tradable`：有行情，数值有效
   - `suspended`：已上市但当日停牌，OHLCV 全 NaN，`suspended=true`
   - `pre_listing`：尚未上市
   - `post_delist`：已退市
3. 禁止 `ffill`、禁止 `interpolate`、禁止隐式 join 补齐。缺失就必须能被下游区分为「停牌」而不是「值为 0」。
4. 停牌日不计入远期收益的交易日计数；跨停牌的收益必须按交易日累乘，不按行数累乘。

## 反例（为什么写这条）

旧数据用「行缺失」表达停牌。实测每股中位只有 **2,289 / 2,428 天**有记录，缺的 139 天不是没上市，是停牌。后果有两层：

- 任何 `groupby().shift()` 都会把停牌前后的两天当成相邻，收益被跨越计算；
- 停牌比例无法统计（因为它表现为「不存在」），所以 G2 门禁无法判定。

## 证据

- `research/00_infrastructure/runs/CLN-20261031-001/trading_flags.parquet`（网格化后 11.1M 行，2018–2026）
- `docs/real-data-run.md`：真实 2026-09 跑批，网格 21 日 × 5,912 只 = 123,956 行，其中停牌 15,068 行（12.16%）
- 契约块：`spec/cleaning.spec.json → missing.grid_rule`、`suspension_delisting.states`

## 检查

```python
# 网格完备性
assert set(df.trade_date) <= set(calendar.trade_date)
assert df.groupby("code").size().nunique() <= 1        # 每只股票行数应一致
assert df[["open","high","low","close"]].isna().any(axis=1).eq(df.suspended | df.pre_listing | df.post_delist).all()
```

## 状态

`DONE` — 四件套已产出；`trading_flags.parquet` 存在同名 4 份副本，规范产物待收敛（见 MIGRATION.md）。
