# 性能：全市场十年怎么跑

## 先把口径说清楚

早期版本的表格是「单进程 vs 分区」，**这不是提速对比，而是内存取舍**：分区会多读上下文、多建网格，
所以同一规模下耗时不降反升。真正的提速对比要看「优化前 vs 优化后」。

### 同一审计强度（1.6M 行，回放抽样 10 个交易日）

| 阶段 | v0.5.2 | v0.6 | 变化 |
|---|---:|---:|---:|
| normalize | 34.4s | 7.5s | **-78%** |
| validate | 0.7s | 0.8s | 持平 |
| grid | 13.1s | 12.8s | 持平 |
| pit | 6.3s | 6.2s | 持平 |
| lookahead（10 日） | 15.0s | 17.1s | 持平 |
| **合计** | **71.4s** | **48.7s** | **-32%** |

结论：单进程的提速主要来自 `normalize`（Decimal 逐值 → numpy half-up），
网格/PIT/回放已经是向量化实现，常数项没有质变。

### 分区（仅用于内存，不是提速）

| 行数 | 单进程 | 分区（按年） | 分区峰值 |
|---:|---:|---:|---:|
| 1.6M | 48.7s | 57.8s | 1,083MB |
| 3.2M | 101s | 103s | 1,730MB |
| 6.4M | 未测 | 203s | 2,402MB |
| 10M（外推） | ~6.4GB | ~5.3 分钟 | ~3.0–3.3GB |

**10M 能跑起来，靠的是两件事**：分区把峰值从 ~6.4GB 压到 ~3GB；回放从「全部交易日」降到
「10 个抽样日」。后者是**审计强度的让步**——它把回放成本从数小时降到几十秒。

如果要求全量回放（`--replay-step 1`），10M 的耗时不是 5 分钟，而是按 `O(D×N)` 增长：
每个交易日重算一次全量特征，量级在小时以上。这一点不能含糊。

## 做了什么

1. **normalize 向量化**：价格四舍五入用 numpy half-up，只有恰好落在 `.5` 附近（二进制浮点边界）
   才走 Decimal 兜底。合成面板 1.6M 行 34.4s → 7.5s。
2. **state_arrays**：主数据按 (code, day) 逐对计算，不再逐行 `master.state()`。
3. **industry classify_arrays**：PIT 的行业 as-of 从每行一次遍历改为按代码块 searchsorted。
4. **自适应回放**：`replay_check` 默认最多审计 60 个交易日（大面板 10），meta 标 `sampled=True`；
   `--replay-step 1` 恢复全量。
5. **分区 + 流式 flags**：按自然年切分，每块带 5 个交易日 lookback；flags 按年写 parquet，
   最后用 ParquetWriter 串成一份，避免把十年 flags 拼在内存里。

## 怎么用

```powershell
# 十年全市场（约 10M 行）
python -m qrg clean --spec spec/cleaning.spec.json `
  --input panel.parquet --factors factors.parquet `
  --sessions sessions.csv --security-rules security.json `
  --industry-rules industry.json --partition-year `
  --out runs/ten-year

# 复现基准（合成数据）
python benchmarks/scale_bench.py --codes 1600 --days 1000
python examples/partition_bench.py --codes 3200 --days 2000 --partition
```

## 边界与取舍

- **分区不是免费的**：3.2M 时与单进程基本持平（+2%），1.6M 时 +19%；小数据不要分区。
- **回放抽样弱化了审计**：10 年只抽查 10 个交易日，能发现系统性泄漏，发现不了个别日期的泄漏。
- **峰值内存按 WorkingSet 测量**（psutil `peak_wset`），含解释器与 pandas 缓冲，不同机器差 10–20%。
- **合成面板**没有停牌/ST/公司行动的真实分布，真实数据耗时会略高。
- **分钟线不在本表范围**：10 年 × 20 分钟线约 1.2 亿行，需要列式存储 + 按日分区，是另一个量级。

## 下一步要真提速，只能换算法

当前瓶颈是 `O(D×N)` 的回放与 pandas 的分组/排序常数。要继续压：

1. **增量回放**：把「前缀特征」做成可增量算子，只对变化点重算，而不是每个抽样日全量重算；
2. **列式引擎**：polars / arrow compute 替换 pandas groupby 热路径；
3. **按日分区**：分钟线必须按日切，而不是按年。

这三件是架构级改动，不是调参。
