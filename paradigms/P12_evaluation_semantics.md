# P12 评估语义：四段切分、多空 vs 只多、beta 双报

**一句话**：报结论之前先回答三个问题——收益算在哪一段（train/val/oos/**live**）、
检验的是因子还是策略（多空 vs 只多）、剥离 beta 了吗（并列报告，不许只报一个）。
权威实现：`research/registry/eval_caliber_v2.md`（`EVAL-CALIBER-v2.2`）+ `scripts/qm_eval.py`。

## 触发

任何要写「Sharpe / 年化 / alpha」结论的地方；引用任何旧 run 的数字做对比之前。

## 规则

1. **四段切分，live 从 2026-07 起**：

| 段 | 起 | 止 | 用途 |
|---|---|---|---|
| train | 2018-01-01 | 2022-12-31 | 因子挖掘、参数定标 |
| val | 2023-01-01 | 2024-12-31 | 模型选择（含 2024 极端 regime） |
| oos | 2025-01-01 | 2026-06-30 | 一次性裁判 |
| **live** | **2026-07-01** | 动态逐月增长 | **实盘，只多** |

   - 归属按**收益实现期末**（`next_month_end`），不按信号日——信号 2026-06-30 的收益在 2026-07 实现，归 live；
   - 硬规则：`n < 12` 的段标 `INSUFFICIENT`，只列不解、不排名、不进摘要。

2. **因子有效性看多空（`D10 − D1`），实盘看只多（`D10`）**，两个数并列；
   十分组用 q10（10%/10%），q5（20%）仅作稳健性。

3. **beta 双报边界**（2026-11-22 明文，禁止只报其一）：
   - 评估**因子选股能力 / 增量 alpha** → 剥离或控制大盘 beta（`+NEUT` 子口径或双基准 alpha）；
   - 评估**资产配置 / 择时策略的绝对收益** → **不盲目剥离**——市场 beta 本身可能就是收益来源，
     择时的盈利正来自「何时持有 beta」，剥离后回答的问题就变了；
   - 底线：**原始收益与 beta 调整后 alpha 并列报告**。基准必须与组合加权方式匹配（等权持仓配全市场等权，
     市值加权配中证1000）；双基准是硬要求。

4. **隔夜跳空单列披露，不是成本项**：T+1 开盘成交下跳空已内嵌于成交价（你买的就是 `open[T+1]`），
   再按平均跳空率扣一笔 = 双重计费。`gap_in / gap_out` 只做方向分析
   （选股组 gap_in +11.67bp 买贵 vs 事件组 −9.83bp 受益），**方向随策略类型反转，不许套用同一假设**。
   只有按 T 收盘成交的对照口径才把跳空当滑点扣。

## 反例

- **beta 污染**：R3 多空 oos 年化 −25.52%，中性化后 −2.35%——**崩塌的 79% 是 beta**
  （D1 腿 beta60 从 1.145 升到 1.613，多空天然带 −0.92 的 beta 敞口）。不剥离就把「市场跌了」误判成「因子死了」。
- **基准翻转结论**：R3 中性只多 alpha——对中证1000 报 10.10%（t=1.20），对全市场等权报 0.49%（t=0.22）。
  **只报一个基准，结论可以完全相反。**
- **成本口径污染 live**：旧轮固定 0.2%/月口径（≈2.4%/年，不到真实成本一半）算出的 Sharpe 一律视为毛值，
  与 `EVAL-CALIBER-v2.2` 的数字不可并列比较。

## 证据

- `research/registry/eval_caliber_v2.md`：§1 四段、§2 成本、§3 十分组与方向语义、§4 跳空、§5 双报
- `research/00_infrastructure/runs/UNI-20261121-002/`：分段与归属规则的来源 run
- `research/10_baseline_portfolio/runs/QM-20261122-001/`：live 段观测候选（`UNVERIFIED`，复检门 oos ≥ 12 月）
- 契约：`spec/run.spec.json` → `caliber_registry.EVAL-CALIBER-v2.2`；范式 P10（执行成本）

## 检查

```python
# 分段边界
assert seg.train == ("2018-01-01", "2022-12-31") and seg.live_start == "2026-07-01"
# live 只多
assert not seg.live_positions.any_short()
# 双报
assert {"raw_ann", "alpha_ann"} <= set(report.columns)      # 原始与 alpha 并列
assert report["benchmark"].nunique() >= 2 or "neut" in report.columns
# 因子评估必须控制 beta
assert report.get("beta_ctrl") is True or "NEUT" in report.get("variant", "")
# 段长度
for name, n in seg.lengths.items():
    if n < 12:
        assert report.loc[name, "flag"] == "INSUFFICIENT"    # 只列不解
```

## 状态

`DONE`（四段 / 十分组 / 方向语义 / 双报边界 / 跳空判定）/ `UNCALIBRATED`（live 段样本量随时间增长）
