# P10 执行与成本：T+1 开盘，成本必须随换手走

**一句话**：信号日收盘下单、次一交易日开盘成交；成本不是固定 bps，是「线性项 + 冲击项」且冲击项随规模与换手变化。

## 触发

任何要报 Sharpe 的地方，包括"只是看看"的中间结果——因为口径一旦混用，后面所有对比都失效。

## 规则

1. 成交价：`t_plus_1_open`。`open_adj = close_adj × Opnprc / Clsprc`（用不复权开盘比乘到复权收盘上）。开盘缺失率实测 **1.88e-4**；缺失时该标的**当日不可成交**，不得退回 close。
2. 成本三项：

```
comm   = 2.6bp × 双边
stamp  = 5.0bp × 卖出腿
impact = η × σ_i(20d) × sqrt(Q_i / ADV20_i),  η = 0.5
```

3. 线性项合计 **10.2bp**（2×2.6 + 5）。成本分解时**不得**用单腿冲击除以双腿总成本——那会把线性项高估约 2 倍。
4. 换手定义：`turnover = 0.5 × Σ|w_t − w_{t−1}|`（单边目标权重差）。**不含持仓市值漂移**，相对低估 6%–10%（成本 +18bp~+86bp/年）。
5. `η = 0.5` 标 `uncalibrated`——A 股无公开冲击曲线。任何结论若对 η 敏感，必须并列披露 η 减半后的结果。
6. 固定 0.2%/月 是历史对照口径，用它算出的 Sharpe **一律视为毛值**，不得与换手驱动口径并列比较。
7. 多空必须同时给纯多头超额 IR。A 股空头不可得，只报多空 Sharpe 会高估策略。

## 反例

33 个因子，多空毛 Sharpe 中位 **0.915**；换成换手驱动成本 @5 亿规模后中位 **−0.151**。成本 **5.42%/年**，盈亏平衡换手 **0.728** vs 实际 **0.760**——差 4%，整个因子库从"能用"变成"不能用"。

同一批因子在固定 0.2%/月口径下看起来全都活着，就是因为那 0.2%/月大约只对应 2.4%/年，不到真实成本的一半。

## 证据

- `research/00_infrastructure/runs/UNI-20261121-002/`：`UNI-CALIBER-v2`，`LS q5 毛 0.915 → 净 −0.151`，`breakeven_turnover 0.728`
- `research/00_infrastructure/runs/FREQ-20261121-001/`：月→日 换手 ×4.53，成本 5.5%→16.4%/年，净 Sharpe −0.17→−1.25
- 契约块：`execution`、`cost`、裁决 C13（vwap 废止）

## 检查

```python
assert (fill_date > signal_date).all()                 # 严格 T+1
assert abs(linear_bp - (2*2.6 + 5.0)) < 1e-9           # 10.2bp
assert 0 < turnover.median() < 2
assert "caliber" in run_json and ("gross" in run_json["caliber"] or "v2" in run_json["caliber"])
```

## 状态

`DONE`（T+1 开盘 / 佣金 / 印花税）/ `UNCALIBRATED`（η）/ `UNMODELED`（融券成本、涨停买不到）
