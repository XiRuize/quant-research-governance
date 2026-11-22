# P11 前视黑名单：字段存在 ≠ 字段能用

**一句话**：一批字段看着可用，实际上是前视或全空；判断「能不能用」的唯一依据是实测填充率与时间分布，不是列是否存在。

## 触发

新接入任何字段；反驳任何"数据缺口"结论；写任何 gap 条目。

## 规则

1. 黑名单（禁止入模）：

| 字段 | 为什么不能用 |
|---|---|
| `lhb.D1..D30` | 龙虎榜次日及后续收益，直接是前视 |
| `financial.UPDATE_DATE` | 数据更新日，晚于公告日 |
| `TRD_Dalyr.LimitStatus` | 2016–2026 **全为 0**，字段存在但零信息 |
| `panel.industry_name` | 跨月零变动 = 静态快照被回填到历史 |

2. **缺口判定纪律**：说某字段"缺"之前必须实测填充率；没测过只能写「待核」，不能写「缺」，更不能写「查无此数」。
3. 零前视检查双轨：
   - **截断回放**：把数据截到 t 日重算因子，与全量结果比对，差异必须为 0；
   - **静态扫描**：源码层禁 `shift(-n)`、禁居中的 rolling window。
4. 回放默认抽 10 个交易日（`sampled=True` 需标注）；全量回放代价 O(D×N)。

## 反例

`Opnprc` 被判为"历史日线无 open"，导致打板族 4 轮 + 全部组合类执行层停在 `close_to_close_PROXY` 长达数周。实测填充率 **100%**（2016–2026）——纯属探测遗漏。

同一批里 `LimitStatus` 是反例：字段存在、类型正确、无缺失，但**所有值都是 0**。前者"看着缺其实有"，后者"看着有其实空"，凭列存在与否判断，两种都会判错。

第三次同类错误：把"财务 PIT 不可用（87% 滞后 >12 月）"写进 catalog 的 gaps。该数字只对 `yjbb` 成立，`financial_pit` 表 NOTICE_DATE 覆盖 100%。整表结论跨表套用，制造了一个不存在的数据缺口。

## 证据

- `research/00_infrastructure/迁移总控.md` §5 第 0 条：`Opnprc` 填充率 100% 的更正记录
- `contracts`：`lookahead.blacklist`、裁决 C18
- `research/00_infrastructure/runs/CLN-20261031-001/data_quality_report.json`：回放审计结果
- `src/qrg/clean/lookahead.py`：双轨检查实现

## 检查

```python
assert df[field].notna().mean() > 0.01, "全 NaN ≠ 缺口，需查源"
assert df[field].nunique() > 1, "单值字段无信息"
assert (df[field].nunique() if numeric else 0) != 1
# 前视扫描
assert not src_contains(r"shift\(\s*-\d+")
assert not src_contains(r"center\s*=\s*True")
```

## 状态

`DONE` — 黑名单已入契约；`qrg clean` 已实现零前视双轨检查。
