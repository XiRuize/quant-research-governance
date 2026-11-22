# P04 财务 PIT：按表分档，长滞后表禁用 NOTICE_DATE

**一句话**：财务数据用公告日做时点锚，但**整表公告日不可信时必须整表换锚**——不是逐行打补丁，也不是干脆不用公告日。

## 触发

任何使用财务字段的因子、事件研究、选股规则。

## 规则

1. 默认锚：`NOTICE_DATE ≤ trade_date`；公告时间 > 15:00 视为盘后，顺延 T+1。
2. 单行长滞后：滞后 > **365 天**的行，改用 `report_period + 90 天` 保守锚，标 `is_proxy=true`，样本权重 8 折。
3. **整表例外**（本次新增）：若某张表的公告日整体不可信，该表**整表**禁用 `NOTICE_DATE`，直接走 `report_period + 90 天`：

| 表 | 公告滞后中位 | >365 天占比 | 处理 |
|---|---:|---:|---|
| `yjbb`（业绩报表） | 418 天 | 86.2% | 禁用 NOTICE_DATE |
| `lrb`（利润表） | 396 天 | 87.1% | 禁用 NOTICE_DATE |
| `xjll`（现金流量表） | 396 天 | 87.1% | 禁用 NOTICE_DATE |
| `yjkb_compliant`（业绩快报） | 59 天 | 0.0% | 可直接作 PIT 锚 |

4. 重述（restatement）取 as-of 保守值，不取事后修订值。
5. 覆盖率门：低于 `announce_coverage_min` 的年份整段标 UNKNOWN，不得插补。

## 反例

- `yjbb` 有 86% 的行公告滞后超过一年。若照常用 `NOTICE_DATE`，因子在 2019 年就能"看到"2021 年才公告的报表——这不是保守与否，是**直接前视**。
- 反过来，把三张长滞后表整体弃用也是错的：`report_period + 90` 是法定截止日，是**可用的保守锚**。丢掉数据比打 8 折损失大得多。
- 曾把"财务 PIT 不可用（87% 滞后 >12 月）"这句写死在 `catalog.json` 的 gaps 里。该结论只对 `yjbb` 成立，对 `financial_pit` 表不成立（后者 NOTICE_DATE 覆盖 100%、中位滞后 57 天）。**整表结论不能跨表套用。**

## 证据

- `ref/financial/financial_pit.parquet`：283,498 行，NOTICE_DATE 覆盖 100%，中位滞后 57 天，长滞后 4.38%（12,415 行）
- `research/00_infrastructure/runs/CLN-LEGACY-PROMOTE-20261121/`：三张表滞后分布的实测
- `research/00_infrastructure/runs/CLN-CSMAR-FIN-20261111/`：财务 PIT 面板 244,031 行，2018+ 公告覆盖 88.3%
- 契约块：`financial_pit`、裁决 C5 / C17

## 检查

```python
lag = (panel.trade_date - panel.notice_date).dt.days
assert lag.min() >= 0                                   # 不存在负滞后
assert (table in {"yjbb","lrb","xjll"}) == panel.anchor.eq("report_period_plus_90").all()
assert panel.loc[panel.is_proxy, "weight"].max() <= 0.8
```

## 状态

`DONE`（`financial_pit`）/ `DONE`（三张长滞后表的例外规则已落契约，尚未在代码层强制）
