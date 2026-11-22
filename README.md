# qrg — 量化研究数据清洗/治理标准库

> Quant Research Governance. 目标不是“再写一套清洗脚本”，而是把**口径、规则、产物、证据**固化成可复用、可审计、可复现的标准库。

## 为什么需要它

开源回测框架的抽象边界是“撮合与记账”，默认数据已经干净：

- 日历只到“交易所开闭市”，没有涨跌停幅度、印花税、ST、新股无限制这类**制度区间**；
- 成本多是单一 bps，“佣金/印花税/过户费/经手费”没有统一科目与逐笔归因；
- 容量要么没有，要么只是成交量上限，不是可回测的容量曲线；
- 数据质量没有契约、没有门禁、没有审计产物。

实际事故绝大多数不来自缺失值，而来自**实体、时间、事件、标签**：代码复用把两家公司拼成一条序列、调休日缺 bar、送转没进事件表、标签跨过了停牌与涨跌停。

## 范围（v0.7.0）

27 个模块 + 管线编排 + 三份契约：

| 层 | 模块 |
|---|---|
| A 总览 | 契约与规范：`cleaning.spec.json` / `run.spec.json` / `registry.spec.json`（版本化 + schema 校验） |
| F 主数据 | 证券主数据、交易日历与会话、公司行动、规范化、指数成分（as-of） |
| B 清洗 | 硬校验、复权、制度日历、行业日历、网格与可交易标志、PIT、零前视审计、四件套产物 |
| C 执行与成本 | 成交模拟、滑点判定、费用科目、冲击模型、逐笔归因 |
| D 容量与尾部 | ADV/参与率、容量曲线、尾部情景 |
| G 文本与事件 | 采集快照、来源分级与仲裁、事件 PIT |
| E 治理 | 门禁、trial registry、provenance |

未包含（明确非目标）：事件抽取、实体消歧、事件-标的映射（G3/G4/G7），高频撮合引擎，组合优化。

## 结构

```text
spec/          清洗/归档/登记三份契约（唯一口径来源）+ 变更记录 + 历史版本
  cleaning.spec.json   清洗口径（0.2.0，已并入 v1.0-locked 全部内容）
  run.spec.json        研究轮次归档与 run.json 字段
  registry.spec.json   数据集登记（一主表 + 源字典）
  dataset_caliber.json 口径覆盖表：id → 「这张表能回答什么」（人写，重跑不冲掉）
  history/             废止契约的字节级原件
paradigms/     清洗范式卡 P01–P11：每条规则背后的真实事故与验证方法
rules/         可版本化规则：制度区间、费率、行业、公司行动、会话
src/qrg/       标准库（含 pipeline.py 分区编排）
tests/         验收用例
benchmarks/    规模基准（合成面板，可复现）
examples/      真实数据适配器与分区基准
docs/          架构、性能、真实数据记录、人机分工
MIGRATION.md   2026-11-21 统一归档记录（迁入什么、留什么、为什么）
```

## 快速开始

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
$env:PYTHONPATH = "src"
python -m pytest -q
python -m qrg version
python -m qrg clean --spec spec/cleaning.spec.json --input panel.parquet --out runs/demo --registry runs/trials
# 可选输入：财务 PIT、市值、覆盖清单
python -m qrg clean --spec spec/cleaning.spec.json --input panel.parquet --factors factors.parquet `
  --financial financial.parquet --market-cap market_cap.parquet --vintages vintages.json --out runs/full
python -m qrg gate --dir runs/demo
```

全市场十年（约 1000 万行）用分区模式：

```powershell
python -m qrg clean --spec spec/cleaning.spec.json `
  --input panel.parquet --factors factors.parquet `
  --sessions sessions.csv --security-rules security.json `
  --industry-rules industry.json --partition-year --out runs/ten-year
```

## 性能（注意口径）

- **同一审计强度**（1.6M 行、回放 10 日）：v0.5.2 **71.4s** → v0.6 **48.7s**，主要来自 `normalize`（34.4s → 7.5s）。
- **分区不是提速手段**：3.2M 单进程 101s、分区 103s，分区换的是内存（峰值 1.7GB）。
- **10M 外推 ~5.3 分钟 / ~3GB**，前提是回放只审计 10 个抽样交易日；`--replay-step 1` 全量回放会到小时级。

口径、取舍与复现命令见 [`docs/performance.md`](docs/performance.md)。

## 数据集治理命令

```bash
qrg data check                    # 按 spec/registry.spec.json 校验（0 fatal 才算过）
qrg data stat panel               # 现场探测物理事实 + 谁在用 + 已知缺口（不落盘）
qrg data refs daily_ext.parquet   # 删任何文件前必查：谁引用了它
qrg data orphans                  # 跨 run 隐式依赖：别人的产出物被谁引用
qrg data consumers                # 增量刷新 consumers
qrg data lint --level fatal       # 按范式卡 P01–P11 体检
```

两条设计取舍：

- **物理事实不入档**。行数、体积、字段改由 `stat` 现场算。`catalog.json` 就是反例——
  67 条里 5 条挂着过期的 `error`、gaps 写着已被推翻的结论。
- **口径归治理层**。`spec/dataset_caliber.json` 存「这张表能回答什么」，
  读取时覆盖进 `datasets[].caliber`。迁移可重跑，但人写的判断不该被重跑冲掉。

## 三条硬规则

1. **代码在主干**：复权、网格、PIT 时点、费用/冲击/容量、hash、门禁一律确定性实现，不接受概率输出。
2. **AI 在边界**：LLM 只在“入库前”出现，产出候选 + 置信 + 证据；回测只读冻结表。
3. **口径可追溯**：任何结论都要能回到 spec 版本、规则版本、数据版本与运行 id；产物带内容 hash。

## 文档

- [清洗范式卡 P01–P11](paradigms/INDEX.md) — **给后续 AI 研究员的入口**：每条规则背后的真实事故
- [统一归档记录](MIGRATION.md) — 2026-11-21 从量化研究迁入什么、留什么、为什么
- [架构与模块状态](docs/architecture.md)
- [性能与分区](docs/performance.md)
- [真实数据接入（CSMAR）](docs/csmar-run.md)
- [代码 review 与修复](docs/code-review-v041.md)
- [文本证据层](docs/text-evidence.md)
- [人机分工](docs/ai-collaboration.md)

## 状态

v0.7.0：清洗 + 执行成本 + 容量尾部 + 文本证据 + 治理五层可端到端运行；**三份契约（清洗 / run 归档 / 数据集登记）取代原先散落的四张登记表与两份互不引用的清洗契约**；`datasets.json` 135 条、`registry check` 零告警；测试 355 项全过；支持 1996 起长历史、十年全市场分区跑、指数成分 as-of 查询。模块就绪情况见 `python -m qrg stages`。

## License

[MIT](LICENSE)
