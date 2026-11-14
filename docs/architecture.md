# 架构

## 分层

```text
A 总览      契约(spec) / CLI / 管线编排(pipeline)
F 主数据    证券主数据、交易日历、公司行动、规范化
B 清洗      硬校验、复权、制度日历、行业日历、网格、PIT、零前视、产物
C 执行成本  成交模拟、滑点、费用、冲击、归因
D 容量尾部  ADV、容量曲线、尾部情景
G 文本事件  采集快照、来源分级、事件 PIT（抽取/实体/映射未实现）
E 治理      门禁、trial registry、provenance
```

## 依赖链

```text
spec ─┬─> normalize ─> validate ─┬─> adjust ─┐
      │                          │           ├─> grid ─> pit ─> lookahead ─> artifacts ─> gate ─> provenance
      └─> master/actions ────────┘           │
              session ─> regime ─────────────┘
              industry ──────────────────────┘
```

分区模式（`--partition-year`）在 `pipeline.clean_partitioned` 中按自然年切分，每块带 lookback
上下文以保留 `prev_close` 跨年连续性；flags 按年落盘后用 `ParquetWriter` 串成一份。

## 关键设计

1. **契约唯一来源**：阈值、白名单、基准日、产物清单都在 `spec/cleaning.spec.json`；代码不内联阈值。
2. **因子阶跃、价格不填充**：复权因子按 as-of 取值；行情/财务缺失一律 NaN。
3. **网格化**：`trade_date × 在册股票`，行缺失 = 显式停牌，禁止隐式 join 补齐；另有公告停牌与无涨跌幅限制表。
4. **产物确定性**：不写时间戳、不依赖行序，manifest 记录 sha256；篡改可检出。
5. **前视双检**：截断回放（自适应抽样）+ 静态扫描（源码），违反即 BLOCKED。
6. **未知显式化**：ST 状态区分 `known / proxy / unknown`，代理数据只用于提示，不冒充权威。

## 模块状态（v0.6）

| 模块 | 文件 | 状态 |
|---|---|---|
| A1 契约 | `src/qrg/spec.py` | ✅ |
| 管线编排/分区 | `src/qrg/pipeline.py` | ✅ |
| F7 规范化 | `src/qrg/quality/normalize.py` | ✅ |
| F1 证券主数据 | `src/qrg/master/security.py` | ✅ |
| F2 交易日历 | `src/qrg/calendar/session.py` | ✅ |
| F3 公司行动 | `src/qrg/master/actions.py` | ✅ |
| B1 硬校验 | `src/qrg/clean/validate.py` | ✅ |
| B2 复权 | `src/qrg/clean/adjust.py` | ✅ |
| B6 制度日历 | `src/qrg/calendar/regime.py` | ✅ |
| B7 行业日历 | `src/qrg/calendar/industry.py` | ✅ |
| B3 网格 | `src/qrg/clean/grid.py` | ✅ |
| B4 PIT | `src/qrg/clean/pit.py` | ✅ |
| B8 零前视 | `src/qrg/clean/lookahead.py` | ✅ |
| B5 四件套 | `src/qrg/clean/artifacts.py` | ✅ |
| C4 成交模拟 | `src/qrg/exec/simulator.py` | ✅ |
| C5 滑点判定 | `src/qrg/exec/slippage.py` | ✅ |
| C1 费用科目 | `src/qrg/cost/fees.py` | ✅ |
| C2 冲击模型 | `src/qrg/cost/impact.py` | ✅ |
| C3 逐笔归因 | `src/qrg/cost/attribution.py` | ✅ |
| E1 门禁 | `src/qrg/gov/gate.py` | ✅ |
| E2 trial registry | `src/qrg/gov/registry.py` | ✅ |
| E3 provenance | `src/qrg/gov/provenance.py` | ✅ |
| D1 ADV/参与率 | `src/qrg/capacity/adv.py` | ✅ |
| D2 容量曲线 | `src/qrg/capacity/curve.py` | ✅ |
| D3 尾部情景 | `src/qrg/tail/scenarios.py` | ✅ |
| G1 采集快照 | `src/qrg/text/ingest.py` | ✅ |
| G5 来源分级 | `src/qrg/text/sources.py` | ✅ |
| G6 事件 PIT | `src/qrg/text/event_pit.py` | ✅ |
| G3/G4/G7 抽取/实体/映射 | — | ❌ 未实现 |

## CLI

```text
qrg version | stages
qrg clean   --spec ... --input ... --out ... [--factors --financial --industry-rules
            --security-rules --sessions --regime-rules --no-limit --suspensions
            --partition-year --replay-step --registry --scan]
qrg gate    --dir OUT
qrg simulate --orders ... --flags ... --panel ... --out ...
qrg cost    --fills ... [--slippage ...] --out ...
qrg capacity --weights ... --panel ... --gross-alpha-bps ... --out ...
qrg tail    --curve ... --out ...
```

`clean` 产出四件套 + `gate.json` + `provenance.json`；被门禁 BLOCKED 时退出码 3。

## 口径与假设

- **capacity_aum（D2）**：网格上最大的可行 AUM（net_bps > 0）；网格上限内仍可行则返回网格上界。
- **capacity（D3）**：按 AUM 升序首个 net_bps ≤ 0 的点；曲线单调时与 D2 一致。
- **locked_ratio**：可交易部分按 (1-locked) 缩放参与率；锁仓部分失去收益暴露、费用仍按全仓计提（保守假设，未校准）。
- **impact**：`rules/impact.json` 的 coef 为文献默认值，标 `uncalibrated`；对外结论不得当作预测。
- **fees**：按成交日制度日历逐项计算，未校准券商实际费率。
- **回放抽样**：`--replay-step 0`（默认）按面板规模抽样（≤60 或 10 个交易日），meta 标 `sampled=True`；
  `--replay-step 1` 为全量审计，代价 O(D×N)。

## 性能

3.2M 行约 100s / 1.7GB；6.4M 分区约 203s / 2.4GB；10M 外推约 5.3 分钟 / 3.0–3.3GB。
详见 [`performance.md`](performance.md)。
