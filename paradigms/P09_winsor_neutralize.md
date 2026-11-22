# P09 缩尾与中性化：两层 MAD，只对总市值与行业

**一句话**：缩尾用两层 MAD（不用 3σ），中性化只对「申万一级哑变量 + ln(总市值)」；总市值缺失时可以用流通市值代理，但必须标 proxy 并披露。

## 触发

任何进入组合构建的截面因子。

## 规则

1. 缩尾：`MAD_two_layer`
   - 第一层：按**日截面**做 MAD 缩尾，`k` 读 `mad_k`；
   - 第二层：在**组合内部**再做一次按因子截断；两层参数互不复用；
   - `MAD = 0` 时回退 IQR；
   - 样本量低于 `small_sample_n` 时**只标记不缩尾**（缩尾会把小样本压成常数）。
2. 中性化：设计矩阵只含 `申万一级哑变量 + log(总市值)`，用 `np.linalg.pinv` 求伪逆（避免行业哑变量共线）。
3. `R² > 0.8` 标 `high_collinearity`——说明因子几乎是风格的线性组合，不该当独立 alpha。
4. **市值口径**：`log(总市值)` 是默认；总市值缺失期可用 `log(流通市值)` 代理，但必须标 `proxy` 并在结论里披露。
5. 排序方向：中性化后按截面**降序**排名归一（0 = 最优端），方向必须与 SIGN 预锁一致。

## 反例

用 3σ 缩尾：A 股收益分布尖峰厚尾，3σ 会把真实的极端收益当异常值砍掉，而这些极端值恰恰是反转类因子信息量最大的部分。MAD 用中位数与中位绝对偏差，对厚尾稳健。

用流通市值当默认口径：实测**会掩盖市值暴露**——流通市值与总市值的比值本身带因子信息（解禁、股权结构），把流通市值塞进中性化等于把一部分 alpha 也中性化掉了。

排序方向搞反：曾把多头取在因子**最差**端，整轮毛 Sharpe 全负，跑了 25 分钟才发现。这不是数值问题，是符号问题——所以方向必须预锁并单独校验。

## 证据

- `research/00_infrastructure/runs/CLN-20261031-001/neutralization_compare.json`：总市值 vs 流通市值 proxy 的实测差异
- `research/00_infrastructure/runs/UNI-20261121-002/run.json → portfolios.neutralization`：`MAD winsor + ln(total_mv) + SW L1 industry dummies`
- `research/00_infrastructure/runs/FREQ-20261121-001/run.json → corrections_issued`：方向反号事故
- 契约块：`outlier`

## 检查

```python
assert design.shape[1] == n_industry + 1          # 哑变量 + ln(mv)，别偷偷加项
assert X["ln_mv"].notna().mean() > 0.95
assert (rank_desc == rank_desc.sort_values()).all()  # 0 端必须是最优
```

## 状态

`DONE` — 已入契约；两层 MAD 与 pinv 在 UNI/BEAR/FREQ 三个 run 中一致实现。
