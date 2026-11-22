# P05 行业 PIT：没有 vintage 就是 UNKNOWN

**一句话**：行业分类必须能回答"这一天，这家公司属于哪个行业"，答案来自生效日 as-of 查询；拿不出 vintage 的行业字段禁止入模。

## 触发

- 行业中性化；
- 行业轮动 / 行业因子；
- 任何按行业分组的统计。

## 规则

1. 必须同时具备 `classification_version`（如 SW2021）与 `vendor_vintage_date`（数据商版本日）。
2. 取值按 `effective_from` as-of：`industry(code, t) = 最后一个 effective_from ≤ t 的版本`；同一 code 同日多版本取最后生效版本。
3. **无 vintage 的行业字段即 UNKNOWN**，不得入模，不得用于中性化。
4. 历史段若只能用快照，结论必须显式标注「行业快照前视」，且不得与 PIT 结论并列比较。
5. 旧行业代码查不到名称的样本标 UNKNOWN，不得映射到相近行业。

## 反例

- 旧 `panel.industry_name` **跨月变动为 0**——这是前视指纹：如果行业真的随时间变，逐月面板里必然有变动。零变动说明它被当日快照回填到了所有历史月份。
- 曾有结论"行业反转显著，t 值很大"，用 SW PIT 重算后 **0/18 存活**。原因：行业截面 n≈31，旧 t 值把个股自由度当成了行业自由度。
- 行业中性化若用了前视行业，等于用今天的分类去切十年前的样本，会把"后来被分到好行业"的公司提前筛出来。

## 证据

- `ref/industry/industry_pit_sw.parquet`：12,929 条变更史，SW2021，月末截面覆盖 **99.97%**
- `research/13_style_rotation/runs/QM-20261101-008/`：`FALSIFIED_MIGRATED`，PIT 下 0/18
- `research/00_infrastructure/runs/CLN-20261031-001/neutralization_compare.json`：中性化口径实测
- 契约块：`industry_pit`、裁决 C6

## 检查

```python
# as-of 查询不得返回生效日之后的行
assert (industry.effective_from <= query_date).all()
# 前视指纹：同一只股票的行业在时间上必须有变动
chg = industry.groupby("code")["sw_l1_name"].nunique()
assert (chg > 1).any(), "行业全期零变动 = 快照回填"
```

## 状态

`DONE` — SW2021 PIT 已入库；`vintage` 字段随行保留。
