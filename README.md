# qrg — 量化研究数据清洗/治理标准库

> Quant Research Governance. 目标不是“再写一套清洗脚本”，而是把**口径、规则、产物、证据**固化成可复用、可审计、可复现的标准库。

## 为什么需要它

开源回测框架的抽象边界是“撮合与记账”，默认数据已经干净：

- 日历只到“交易所开闭市”，没有涨跌停幅度、印花税、ST、新股无限制这类**制度区间**；
- 成本多是单一 bps，“佣金/印花税/过户费/经手费”没有统一科目与逐笔归因；
- 容量要么没有，要么只是成交量上限，不是可回测的容量曲线；
- 数据质量没有契约、没有门禁、没有审计产物。

实际事故绝大多数不来自缺失值，而来自**实体、时间、事件、标签**：代码复用把两家公司拼成一条序列、调休日缺 bar、送转没进事件表、标签跨过了停牌与涨跌停。

## v0.4 范围

27 个模块：

| 层 | 模块 |
|---|---|
| A 总览 | 契约与规范（spec schema / 版本化） |
| F 主数据 | 证券主数据、交易日历与会话、公司行动、规范化 |
| B 清洗 | 硬校验、复权、制度日历、行业日历、网格与可交易标志、PIT、零前视审计、四件套产物 |
| C 执行与成本 | 成交模拟、滑点判定、费用科目、冲击模型、逐笔归因 |
| D 容量与尾部 | ADV/参与率、容量曲线、尾部情景 |
| G 文本与事件 | 采集快照、来源分级与仲裁、事件 PIT |
| E 治理 | 门禁、trial registry、provenance |

## 结构

```text
spec/      清洗契约（唯一口径来源）
rules/     可版本化规则：制度区间、费率、行业、公司行动、会话
src/qrg/   标准库
tests/     验收用例
```

## 快速开始

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
$env:PYTHONPATH = "src"
python -m pytest -q
python -m qrg version
python -m qrg clean --spec spec/cleaning.spec.json --input panel.parquet --out runs/demo --registry runs/trials
python -m qrg gate --dir runs/demo
```

## 三条硬规则

1. **代码在主干**：复权、网格、PIT 时点、费用/冲击/容量、hash、门禁一律确定性实现，不接受概率输出。
2. **AI 在边界**：LLM 只在“入库前”出现，产出候选 + 置信 + 证据；回测只读冻结表。
3. **口径可追溯**：任何结论都要能回到 spec 版本、规则版本、数据版本与运行 id；产物带内容 hash。

## 状态

v0.4：清洗 + 执行成本 + 容量尾部 + 文本证据 + 治理五层可端到端运行（27 模块、287 用例）。模块就绪情况见 `python -m qrg stages`。

## License

MIT


