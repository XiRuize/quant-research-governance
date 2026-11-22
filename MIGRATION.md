# 统一归档记录（2026-11-21）

> 本次把散落在 `量化研究/数据源/` 与 `research/` 的口径类文件收进本库，使
> **qrg = 唯一「口径与规则」来源，量化研究 = 唯一「数据与结论」来源**。
> 下面每一条都可回滚：原件全部留档，没有一处是"删了再写"。

## 1 迁入了什么

| 原位置 | 现位置 | 处理 |
|---|---|---|
| `数据源/data_cleaning_spec.v1.0-locked.json` | `spec/cleaning.spec.json`（0.2.0） | **合并**：7 块 + C1–C12 全量并入；原文件降级为指针（保留 `status: SUPERSEDED` 与 `migration_map`） |
| 同上（字节级原件） | `spec/history/cleaning.v1.0-locked.original.json` | **归档**：原样复制，供逐字段比对 |
| `spec/cleaning.spec.json`（0.1.0） | 同名，升 0.2.0 | **扩写**：新增 9 块 + C13–C18 |
| `research/迁移总控.md §6 交付规范` | `spec/run.spec.json` | **契约化**：从散文改为可校验的字段契约 |
| `数据源/data/{catalog,views,lineage,sources}.json` 的字段语义 | `spec/registry.spec.json` | **契约化**：四表 → 一主表 + 源字典 |
| `数据源/README.md` §8/§10 的「引用前必知」、各 run 的 `known_limitations`、历史对话中的清洗决策 | `paradigms/P01–P11` | **提炼为范式卡**：每条含事故、证据、检查 |

## 2 没有迁的，以及为什么

| 文件 | 留下理由 |
|---|---|
| `数据源/数据清洗目标与缺口审计.md`（25 KB） | 是**审计留痕**，不是规范。它记录了"当时为什么判 G3 WARN"，属于证据不属于规则 |
| `research/registry/*.json`（rounds / assets / …） | 是**研究侧台账**，描述"做了什么"，不描述"该怎么做" |
| `research/90_legacy/`（8.7 GB 表观 / 4,149 文件） | 迁移未完成，按决定**暂时搁置**；迁完即删。状态标 `DEPRECATED_IN_PROGRESS`，引用必须带 `migration.status` |
| 各 run 的 `run.json` | 是**结论载体**，结构由 `run.spec.json` 管，内容不搬 |
| `docs/*`（架构、性能、真实数据记录） | 是 qrg 自身的技术文档，与量化研究口径无关 |

## 3 三处关键裁决

### 3.1 会过期的事实不进契约

v1.0-locked 把 `field_availability`（字段可用性快照）写在契约里。问题不是它错，而是**它会过期**：
`industry_pit`、`st_status`、`total_mv` 在 2026-11 都已变化，但规则文件不该跟着变。

处置：可用性移入 `registry.datasets[].status`，契约只留 8 档枚举与含义（C16）。
**判据**：一条内容如果半年后可能变，它属于 registry；如果只会在"口径修订"时变，它属于 spec。

### 3.2 重复键必须显式裁定，不能靠 JSON 的"后者覆盖"

`field_availability.st_status` 在 v1.0-locked 里出现两次：先是
`{"status":"SNAPSHOT_ONLY","source":"...794 只 ST..."}`，后是 `{"status":"UNAVAILABLE"}`。
Python 的 `json.loads` 静默取后者，于是**文本上写着"可用"、机器读出来"不可用"**，
且没有任何报错。裁定结果 `SNAPSHOT_ONLY` 已写入 C14。

### 3.3 成交价口径以实际执行为准

qrg 0.1.0 写 `price_basis: "vwap"`，而库内**没有任何分钟线**，vwap 物理上不可实现；
全部历史结论（UNI-002 / FREQ / BEAR）都按 T+1 开盘执行。裁定以 T+1 开盘为准，vwap 废止（C13）。
**教训**：契约写在代码之前时，容易写成"理论上更好"而不是"实际上做得到"。

## 4 已完成（原待办，2026-11-21 当日闭环）

| 项 | 结果 |
|---|---|
| `datasets.json` 生成 | **135 条 + 14 源**，扫描器移入 `qrg/registry.py`，摆脱已不存在的 `未来/金融智能体`；`qrg data init --from-legacy` 可重跑（旧表已归档，自动读 `_superseded/2026-11-21/` 副本） |
| 三个子命令 | 落成 8 个：`data init / check / list / stat / consumers / refs / orphans / lint`。`run check` **有意不写**——存量 63 个 run 全走 `legacy` 门只校验 `run_id`，新 run 为 0，写了也是空跑 |
| 缺 `run.json` 的目录 | 实为 **10 个**（原报 11）：3 个不是 run（`QM-DEBUG` 调试面板、`QM-20261119-106` 的 5 个 `_dbg` 脚本、`QM-20261031-001` 单个 md），按 R1 删除/降级；**7 个真 run 补齐 12 字段** |
| `trading_flags` 副本 | **不收敛**。5 份哈希全不同（`69f8/c986/55fa/82f5/a28e`），是不同清洗参数下的标记结果，删任一份即丢失"换 DRET/NL 后哪些票被排除"的证据（R2 不可再生） |
| `数据源/` 根目录 md | 未动。属留痕而非规范，收敛需逐份判定，收益低风险高 |

## 4b 由此新增的两个能力（针对已发生的真实事故）

| 命令 | 解决什么 | 事故 |
|---|---|---|
| `qrg data refs <file>` | 删任何文件前查"谁在用" | `daily_ext.parquet`（235 MB）住在 BEAR run 的 `02_data/` 下，被 FREQ / UNI-002 跨引用，**却不在任何一张登记表里**——登记表只覆盖 `数据源/`，run 产出物全部在盲区 |
| `qrg data orphans` | 列出"某 run 产出物被其他 run 引用" | 实测 **14 个**跨 run 隐式依赖，合计约 470 MB。这类文件删了不报错，只会在下游下一轮炸掉 |
| `qrg data consumers` | 增量刷新"谁用了这张表" | `consumers` 若存静态快照就会重演 `catalog.json` 的过期命运；改为现场扫描 + 缓存，`stat` 每次重扫 |

口径（`caliber`）单独放 `spec/dataset_caliber.json`，由 `load_registry()` 读取时覆盖。
理由：`datasets.json` 是可重跑的迁移产物，而"这张表能回答什么问题"是人写的判断，
重跑迁移不该把它冲掉。

## 5 验收方式

```powershell
cd quant-research-governance
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m pytest -q          # 355 passed
.venv\Scripts\python.exe -m qrg version        # 0.7.0
.venv\Scripts\python.exe -m qrg data check     # 0 fatal / 0 warn / 135 条
.venv\Scripts\python.exe -m qrg data orphans   # 14 个跨 run 依赖
.venv\Scripts\python.exe -c "from qrg.spec import load_spec; d=load_spec('spec/cleaning.spec.json'); print(d.spec_version, len(d.model_extra['rulings']['items']))"
# 期望：0.2.0 18
```
