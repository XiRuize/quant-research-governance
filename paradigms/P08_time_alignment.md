# P08 时间对齐：月末是最后一个交易日，不是 31 号

**一句话**：所有"月末"必须取该月最后一个**交易日**；所有分段按**日历月**切，不按日期边界切。

## 触发

月度调仓、月度面板构建、远期收益窗口、训练/验证/样本外切分。

## 规则

1. 月末截面 = 该月最后一个交易日的收盘。**不得**用 `resample("ME")` 或 `MonthEnd()` 直接生成。
2. 远期收益起点 = 信号日之后的**执行日开盘**；终点 = 下一期执行日开盘。不得跳过停牌日直接拼接。
3. 分段按日历月归属：边界日落在非交易日时，归入前一个交易月。
4. 窗口长度用**实际交易日数**校验，不用自然日数推算。10 月只有 17 个交易日，20 日窗口要跨到下月中。

## 反例

用日历月末生成 105 个月的截面，实测**丢了 35 个月**——因为 2018-03-31 是周六，2018-06-30 是周六，等等。丢月不会报错：`groupby` 少几个组，Sharpe 照样算得出来，只是样本少三分之一。

窗口长度的同类错误：`2026-09-30` 的信号，20 交易日远期要到 `2026-11-04` 才满（10 月仅 17 个交易日）。若按自然日近似，会得到一半是 NaN 的样本却以为窗口已满。

## 证据

- `数据源/README.md` §8 第 3 条：月末必须取最后一个交易日，曾丢 35/105 个月
- `research/00_infrastructure/runs/UNI-20261121-002/run.json`：`month_ends=106`、`months_used=105`，`segments_rule` 明确"by calendar month (not date boundary), 2025-01-27 case handled"
- `research/00_infrastructure/runs/BEAR-20261121-001/run.json → caliber.P1_sample_note`：2026-09-30 需到 11-04 才满 20 日
- 契约块：`time_alignment`

## 检查

```python
# 每个"月末"都必须是该月最后一个交易日
last = cal.groupby(cal.dt.to_period("M")).dt.max()
assert sorted(month_ends) == sorted(last[last.isin(month_ends)])
# 交易日数校验
assert (cal.dt > start).cumsum().eq(20).idxmax() <= end_date
```

## 状态

`DONE` — 已入契约与 UNI-002 实现；`BEAR` 与 `FREQ` 共用同一处理。
