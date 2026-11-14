# 清洗契约变更记录

> 包版本（`pyproject.toml` / `qrg.__version__`）与契约版本（`spec.cleaning.spec.json` 的
> `spec_version`）相互独立：契约只在**口径**变化时升版本，包在功能/修复时升版本。

## spec 0.1.0 — 2026-11

- 初版锁定：五类硬校验、price_adjustment（raw + factor）、missing 网格化、lookahead 规则、
  execution 口径、四件套产物清单。
- 与本地 `量化研究/数据源/data_cleaning_spec.v1.0-locked.json` 的键名保持兼容；
  本地契约中的 `dynamic_threshold_asof`、`trial_registry` 等由治理层（E 层）承担，不在本 spec 内。

## 变更规则

1. 任何口径变化（阈值键、字段定义、时点规则）必须升 `spec_version` 并在本文件补一条；
2. 阈值只写键名，不写数值；数值放 `rules/` 或环境配置；
3. 变更需同步更新 `spec/schema.json` 与 `spec/cleaning.spec.json`；
4. 已发布版本的契约不得原地修改，只能新版本叠加。
