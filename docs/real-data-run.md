# 真实数据接入记录（2026-09，A 股）

目的：用本地真实数据跑一遍 `qrg clean`，看门禁会报什么。**结果比 synthetic 测试更有价值**：规则本身没崩，但暴露了三类真实的建模缺口。

## 输入

| 输入 | 来源 | 规模 |
|---|---|---|
| 面板 | `ref/new_bars/new_bars.parquet`（新浪不复权） | 2026-09 共 108,888 行、5,195 只 |
| 交易日历 | `ref/calendar/calendar.parquet` | 21 个交易日 |
| 证券主数据 | `ref/stock_basic/stock_basic.parquet` | 5,921 个 `listed` 事件 |
| 行业 as-of | `ref/industry/industry_pit_sw.parquet` | 12,928 条（1 条零长度区间被剔除） |
| 制度规则 | 本仓库 `rules/regime.cn.json` | 默认 |

复权因子未接（`--factors` 缺省），因此 `adjust` 阶段跳过——这是本次唯一的主动省略。

## 运行结果

```text
gate = warn
steps = normalize, validate, grid, pit, lookahead
rows = 108,888
耗时 ≈ 9.5s（单机、单线程）
```

| 检查 | 结果 |
|---|---|
| 硬校验（主键/高低/负量/代码白名单） | **0 违规** |
| 网格 | 123,956 行（21 日 × 5,912 只） |
| 停牌 | 15,068 行（12.16%） |
| 涨跌停 | 涨停 1,146 / 跌停 262 / 锁死 93 |
| ST | **0**（应为 317 只、4,018 行） |
| 新股 | 24 |
| 行业未知 | 4 行（3.7e-5） |
| 零前视回放 | 通过（0 差异） |

结论：**数据本身很干净，问题全在“元数据没接上”**。

## 三个真实缺口

### 1. 退市/长期停牌未建模 → 停牌率被虚增 12%

- 网格里 717 只股票整个 9 月从未成交，贡献 14,861 行（占全部停牌的 99%）；
- 这 717 只里 `LISTING_STATE=2`（退市）336 只、`=0` 377 只、`=10` 4 只；
- 剔除它们后，真实停牌率只有 **0.19%**。

原因：适配器把 `stock_basic` 的每一行都当成“已上市且未退市”，而库没有收到任何 `delisted` 事件，于是死代码在网格里变成“永久停牌”。这正是蓝图里 `f-master` 要解决的生存偏差，只是真实数据比预想更极端。

### 2. ST 状态没接 → 5% 涨跌停被当成 10%/20%

- 网格中有 317 只 ST，4,018 行已成交；
- 当前 `limit_pct` 分布：0.10 有 2,919 行、0.20 有 1,099 行，**没有一行是 0.05**；
- 也就是说，这 4,018 行的涨跌停判定全部偏松，锁死票会被误判为可交易。

原因：`st_status.parquet` 只有当前快照，没有 `st_on/st_off` 历史日期；按治理规则**不能用快照回填历史**，所以库里没有 ST 事件，`is_st` 全是 False。

### 3. 复权没接（主动省略，但要知道后果）

`new_bars` 是不复权价，9 月无除权日，所以本次不影响；跨除权日就会把送转当成暴跌，硬校验会把它当“异常”而不是“公司行动”。

## 顺带验证到的

- `industry_pit_sw` 质量很好：12,929 条里只有 1 条零长度区间（`from == to`），适配器剔除并计数；
- `new_bars` 的 OHLC/量额没有任何结构性错误，说明源本身可用；
- 截断回放（21 个交易日 × 10.9 万行）跑通，说明 `lookahead` 在真实规模上可用；
- 整条链路 9.5 秒，其中网格的逐行 `master.state()` 是主要开销。

## 复现

```powershell
$env:PYTHONPATH = "src"
python examples/real_data_prepare.py `
  --data-root "C:/Users/生升/Desktop/未来/量化研究/数据源/data" `
  --start 2026-09-01 --end 2026-09-30 --out examples/_real/sept

python -m qrg clean --spec spec/cleaning.spec.json `
  --input examples/_real/sept/panel.parquet `
  --sessions examples/_real/sept/sessions.csv `
  --security-rules examples/_real/sept/security.json `
  --industry-rules examples/_real/sept/industry.json `
  --out examples/_real/sept/out
```

## 下一步（按性价比排序）

1. **退市事件接入**：`LISTING_STATE=2` 必须变成 `delisted` 事件；退市日期拿不到就标 UNKNOWN，并让网格把“未知”与“停牌”区分开，不能默认可交易。
2. **ST 历史接入**：只有快照时，只允许“当日”使用 ST 标记，历史区间一律 UNKNOWN（现在已经是这个行为，但需要在报告里显式计数，而不是静默 is_st=0）。
3. **复权因子接入**：`daily_factors` 有 `close` 与 `prev_close`，可反推因子；接入后跑跨除权月的窗口，验证 `adj_jump` 与 `B2` 的可逆性。
4. **性能**：网格里逐行 `master.state()` 换成按 code 分段 + 事件驱动，10.9 万行目前 9.5s，全市场 10 年会是小时级。
