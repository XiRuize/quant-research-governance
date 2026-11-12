# 性能：全市场十年怎么跑

## 结论

| 行数 | 单进程耗时 | 单进程峰值 | 分区耗时 | 分区峰值 |
|---:|---:|---:|---:|---:|
| 200,000 | 3.6s | — | — | — |
| 800,000 | 14.1s | — | — | — |
| 1,600,000 | 48.7s | 1,406MB | 57.8s | 1,083MB |
| 3,200,000 | 101s | 2,036MB | 103s | 1,730MB |
| 6,400,000 | — | — | 203s | 2,402MB |
| **10,000,000（外推）** | — | ~6.4GB | **~5.3 分钟** | **~3.0–3.3GB** |

10M = 全市场约 5,000 只 × 10 年（2,500 交易日）。外推基于 3.2M/6.4M 的线性拟合；目标 **≤10 分钟、≤4GB** 在分区模式下成立。

## 做了什么

1. **normalize 向量化**：价格四舍五入改为 numpy half-up，只有恰好落在 `.5` 附近（二进制浮点边界）才用 Decimal 兜底。合成面板 200K 行从 4.1s → 0.9s。
2. **state_arrays**：主数据按 (code, day) 逐对计算，不再逐行 `master.state()`；网格 116K 行 2.14s → 1.5s。
3. **industry classify_arrays**：PIT 的行业 as-of 从每行一次遍历改为按代码块 searchsorted。
4. **自适应回放**：`replay_check` 默认最多审计 60 个交易日（大面板降到 10），meta 标 `sampled=True`；`--replay-step 1` 恢复全量。
5. **分区 + 流式 flags**：按自然年切分，每块带 5 个交易日 lookback 保证 `prev_close` 跨年连续；flags 按年写 parquet，最后用 ParquetWriter 串成一份，避免把 10 年 flags 拼在内存里。

## 怎么用

```powershell
# 十年全市场（约 10M 行）
python -m qrg clean --spec spec/cleaning.spec.json `
  --input panel.parquet --factors factors.parquet `
  --sessions sessions.csv --security-rules security.json `
  --industry-rules industry.json --partition-year `
  --out runs/ten-year

# 复现基准（合成数据）
python benchmarks/scale_bench.py --codes 3200 --days 2000
python examples/partition_bench.py --codes 3200 --days 2000 --partition
```

## 边界与取舍

- **分区不是免费的**：3.2M 时与单进程基本持平（+2%），1.6M 时 +19%（重复读取上下文 + 多次建网格）；小数据不要分区。6.4M 只跑了分区模式，单进程未测。
- **回放抽样弱化了审计**：10 年只抽查 10 个交易日，能发现“时间方向上系统性泄漏”，但发现不了只在个别日期触发的泄漏；要严格审计就 `--replay-step 1`（代价是 O(D×N)）。
- **峰值内存按 WorkingSet 测量**（psutil `peak_wset`），含解释器与 pandas 缓冲；不同机器会差 10–20%。
- **合成面板**没有停牌/ST/公司行动的真实分布，真实数据的耗时会因行业/PIT 连接与缺失行略高；6.4M 的分区跑已接近线性，量级可信。
- 分钟线的 240 倍数据量不在本表范围内；按当前斜率，10 年 × 20 分钟线约 1.2 亿行，需要列式存储 + 按日分区，不是同一个量级的问题。
