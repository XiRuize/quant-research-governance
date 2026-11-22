# 清洗契约变更记录

> 包版本（`pyproject.toml` / `qrg.__version__`）与契约版本（`spec.cleaning.spec.json` 的
> `spec_version`）相互独立：契约只在**口径**变化时升版本，包在功能/修复时升版本。

## spec 0.2.0 — 2026-11-21

**合并**：`量化研究/数据源/data_cleaning_spec.v1.0-locked.json`（7 块 + C1–C12 裁决）并入本契约，
原文件归档至 [`history/cleaning.v1.0-locked.original.json`](history/cleaning.v1.0-locked.original.json)，
`数据源/` 侧降级为一行指针。自此清洗口径只有一个来源。

**新增 9 块**（历史实际采纳、但此前从未入任何契约）：

| 块 | 内容 | 来源事故 |
|---|---|---|
| `dedup_and_units` | TRD_Dalyr 同主键 6–8 行重复；Dsmvosd 千元；Trddt 为 varchar | 不去重收益放大 6–8 倍 |
| `time_alignment` | 月末 = 该月最后一个交易日；分段按日历月 | 日历月末曾丢 35/105 个月 |
| `universe_and_board` | main/star_chinext/bse 三分；跨表合并前按 board 过滤 | yjbb 混新三板，样本 ×2.1 |
| `financial_pit.table_exception_*` | yjbb/lrb/xjll 中位滞后 396–418 天，整表禁用 NOTICE_DATE | 86–87% 超 365 天 |
| `outlier.neutralization_mv_rule` | 总市值默认口径；流通市值仅 proxy 且须披露 | proxy 掩盖市值暴露 |
| `execution` + `cost` | T+1 开盘公式；佣金 2.6bp/印花 5bp/冲击 η·σ₂₀·√参与率 | 成本口径分裂 |
| `lookahead.blacklist` | lhb D1..D30、UPDATE_DATE、LimitStatus、industry_name | 4 条独立前视事故 |
| `portfolio_constraints` | 带宽进出池、相位必须全覆盖、q5 默认、多空须并列纯多头 | 相位差 0.5 Sharpe |
| `data_status_enum` | 8 档状态枚举 + 含义 | 可用性散落在各处 |

**关键设计变更**：`field_availability`（字段可用性快照）**移出契约**，改由
`spec/registry.spec.json → datasets[].status` 承载。理由：v1.0-locked 把「规则」和「当时的
现状」混在一个文件里，现状会过期（st_status、industry_pit 都已变），而规则不该跟着过期。

**新裁决 C13–C18**：

- **C13 成交价**：0.1.0 写 `price_basis=vwap`，但库内无分钟线、且全部实际结论按 T+1 开盘 →
  以 T+1 开盘为准，vwap 废止。
- **C14 st_status 重复键**：v1.0-locked 的 `field_availability` 里 `st_status` 出现两次，
  JSON 解析时后者 `UNAVAILABLE` 覆盖前者 `SNAPSHOT_ONLY`，导致契约文本与机器读取不一致 →
  裁定 `SNAPSHOT_ONLY`（有 794 只当期快照），历史段 UNKNOWN。
- **C15 产物命名**：`data_clean_report.json`（v1.0）vs `clean_report.json`（0.1.0）→
  统一前者，后者作别名。
- **C16 字段可用性归属**：见上。
- **C17 长滞后表**：整表禁用 NOTICE_DATE，走 `report_period + 90`。
- **C18 缺口判定**：未实测填充率不得写「缺」，只能写「待核」；反面案例 `LimitStatus` 全 0。

**测试**：`tests/test_e2e_v02.py`、`tests/test_e2e_v04.py` 原硬编码 `"0.1.0"`，改为读契约
实际版本，避免每次升版本都要改测试（真实回归是"版本能从契约传到产物"，不是"版本是 0.1.0"）。

## spec 0.1.0 — 2026-11

- 初版锁定：五类硬校验、price_adjustment（raw + factor）、missing 网格化、lookahead 规则、
  execution 口径、四件套产物清单。
- 与本地 `量化研究/数据源/data_cleaning_spec.v1.0-locked.json` 的键名保持兼容；
  本地契约中的 `dynamic_threshold_asof`、`trial_registry` 等由治理层（E 层）承担，不在本 spec 内。
  **（0.2.0 起改为完全并入，不再"保持兼容"。）**

## 变更规则

1. 任何口径变化（阈值键、字段定义、时点规则）必须升 `spec_version` 并在本文件补一条；
2. 阈值只写键名，不写数值；数值放 `rules/` 或环境配置；
3. 变更需同步更新 `spec/schema.json` 与 `spec/cleaning.spec.json`；
4. 已发布版本的契约不得原地修改，只能新版本叠加。

## pkg 0.7.1 — 2026-11-22

**新增 `qrg run check`**（`src/qrg/run_check.py` + 13 条测试）：run.spec.json check_rules 的可执行版。
发现 research/<family>/runs/ 下全部 run（含无 run.json 的目录，即 fatal RUN_JSON_MISSING）；
v1 强制 12 必填字段，legacy 门只强制 run_id；caliber 允许「登记键+分隔符+说明后缀」并认可迁移流的
caliber_id 别名；契约 caliber_registry 新增 EVAL-CALIBER-v2.2、fixed_single_side_30bps；verdict 词根
新增 INCONCLUSIVE。首日实战：补齐 2026-11-21/22 迁移流 17 个 run.json 后 0 fatal / 203 warn（legacy 债）。

**lint 闭环语义**：数据集 known_limitations 中带【qrg data lint】标签且含同 P 卡编号的已知缺陷，
fatal/warn 降为 info（数据修复并删除登记条目后自动转红）。首日 7 fatal 全部转 info；
panel / panel_official_backup / yjbb / yjkb_compliant 的 P11 前视指纹与 lrb / xjll / yjbb 的
P04 NOTICE_DATE 失效已回写登记表。

**registry.spec 1.0.2**：commands 段补 `qrg run check`。

## pkg 0.7.2 / run.spec 1.0.1 — 2026-11-22

**评估语义立卡 P12**（paradigms/P12_evaluation_semantics.md）：四段切分（live=2026-07 起只多、
归属按收益实现期末、n<12 标 INSUFFICIENT）、因子看多空/实盘只多（十分组 10%/10%）、
beta 双报边界（因子评估控制 beta；择时/配置绝对收益不盲目剥离；原始与 alpha 并列、双基准硬要求）、
隔夜跳空单列披露不额外扣（T+1 开盘成交下已内嵌，额外扣=双重计费）。

同步修订：eval_caliber_v2.md §4 补「跳空不是成本项」明文、§5 补「剥离适用边界」表；
run.spec caliber_registry 的 EVAL-CALIBER-v2.2 描述扩充并升 1.0.1（无结构变更）。
INDEX 路由「做组合构建 / 报 Sharpe」加入 P12。
