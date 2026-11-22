# P03 涨跌停与 ST：无名录就说 UNKNOWN

**一句话**：涨跌停按板块分别判定，锁死的票当日不可买卖；没有当日 ST 名录时一律标 UNKNOWN，不许"按 10% 处理"，也不许用股票简称反推。

## 触发

- 任何组合构建（信号日 / 执行日）；
- 任何以"可交易性"为前提的事件研究（打板、首板、涨停后漂移）；
- 任何 `adj_jump` 判定。

## 规则

1. 板块幅度：主板 10% / 科创创业 20% / 北交所 30% / ST 5%。板块按**代码前缀**判定，不按简称。
2. `high == low` 且封在限价 → `limit_up_locked` / `limit_down_locked`，**当日既不能买也不能卖**。
3. **组合必须剔除信号日锁定票**，否则回测结果作废（`invalid_execution`）。这不是"保守处理"，是硬约束。
4. ST 状态：有当期截面名录时按 ±5% 识别；无名录的历史段一律 `UNKNOWN`。
   - 禁止把 ST 当 10% 处理（会漏判 5% 触板）；
   - 禁止用当日简称反推当日状态（简称是快照，会前视）；
   - `|ret| ∈ [4.5%, 5.5%]` 只标 `st_suspect_band` 作提示，不冒充识别。
5. 注册制新股上市前 5 日无涨跌幅限制，**不计入** `adj_jump` 违规。

## 反例

- 曾把 `LimitStatus` 字段存在当作"涨跌停可判定"，实际该字段 2016–2026 **全为 0**，一行信息都没有。
- 曾判定"ST 历史缺失 → 打板族 4 轮全阻塞"，后来发现 `trading_flags` 已自带 `limit_up_locked` / `st_suspect_band` / `is_st`，涨跌停判定**不依赖 ST 名录**。两次误判都源于没有实测字段填充率。
- 开仓日涨停买不到至今**未建模**（CSMAR `LimitUp/LimitDown` 2016 年后全 NaN），方向偏向高估高频策略——这条必须写进每条高频结论的局限里。

## 证据

- `research/00_infrastructure/runs/CLN-20261031-001/trading_flags.parquet`（11.1M 行，含三个 flag 列）
- `docs/real-data-run.md`：2026-09 真实跑批 涨停 1,146 / 跌停 262 / 锁死 93；ST 识别 **0**（应为 317 只），即典型 UNKNOWN 场景
- `research/16_daban_limit_up/runs/QM-20261102-022/`：112,486 涨停，T+1 开盘可买 88.1%
- 契约块：`limit_and_st`

## 检查

```python
locked = (df.high == df.low) & (df.close == df.limit_price)
assert not (holdings.index.isin(locked[locked].index)).any()   # 组合不含锁死票
assert set(df.st_status.dropna().unique()) <= {"known","proxy","unknown"}
```

## 状态

`DONE`（涨跌停）/ `SNAPSHOT_ONLY`（ST：仅 2026-10-31 快照 794 只，历史段 UNKNOWN）
