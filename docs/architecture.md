# 架构

## 分层

```text
A 总览      契约(spec) / CLI
F 主数据    证券主数据、交易日历、公司行动、规范化
B 清洗      硬校验、复权、制度日历、行业日历、网格、PIT、零前视、产物
C 执行成本  成交模拟、滑点、费用、冲击、归因        (v0.2)
D 容量尾部  容量曲线、尾部情景                     (v0.3)
G 文本事件  采集快照、抽取、实体、来源、事件 PIT、映射 (v0.2)
E 治理      门禁、trial registry、provenance        (v0.2)
```

## 依赖链

```text
spec ─┬─> normalize ─> validate ─┬─> adjust ─┐
      │                          │           ├─> grid ─> pit ─> lookahead ─> artifacts
      └─> master/actions ────────┘           │
              session ─> regime ─────────────┘
              industry ──────────────────────┘
```

## 关键设计

1. **契约唯一来源**：阈值、白名单、基准日、产物清单都在 `spec/cleaning.spec.json`；代码不内联阈值。
2. **因子阶跃、价格不填充**：复权因子按 as-of 取值是正确做法；行情/财务缺失一律 NaN。
3. **网格化**：`trade_date × 在册股票`，行缺失 = 显式停牌，禁止隐式 join 补齐。
4. **产物确定性**：不写时间戳、不依赖行序，manifest 记录 sha256；篡改可检出。
5. **前视双检**：截断回放（动态）+ 静态扫描（源码），违反即 BLOCKED。

## 模块状态（v0.2）

| 模块 | 文件 | 状态 |
|---|---|---|
| A1 契约 | `src/qrg/spec.py` | ✅ |
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

## v0.3 新增的 CLI

```text
qrg capacity --weights weights.csv --panel panel.parquet --gross-alpha-bps 100 --out cap/
qrg tail     --curve cap/capacity_curve.parquet --out tail/
```

`capacity` 输出 `capacity_curve.parquet`（aum/participation/impact_bps/fee_bps/net_bps/feasible）与 `capacity.json`（容量上限 + uncalibrated 标记）；
`tail` 输出三情景（normal/stress/illiquid）曲线，同情景同种子逐值可复现。

## 口径与假设（v0.3）

- **capacity_aum（D2）**：网格上最大的可行 AUM（net_bps > 0）；网格上限内仍可行则返回网格上界，需自己扩网格。
- **capacity（D3）**：按 AUM 升序首个 net_bps ≤ 0 的点；曲线单调时与 D2 一致，非单调时以 D3 的定义为准（报告里同时给 meta["scenarios"]）。
- **locked_ratio**：可交易部分按 (1-locked) 缩放参与率；锁仓部分失去收益暴露、费用仍按全仓计提（保守假设，未校准）。
- **impact**：`rules/impact.json` 的 coef 为文献默认值，标 `uncalibrated`；对外结论不得当作预测。
- **fees**：按成交日制度日历逐项计算，未校准券商实际费率。

## v0.2 新增的 CLI

```text
qrg simulate --orders orders.csv --flags flags.parquet --panel panel.parquet --out sim/
qrg cost     --fills fills.csv --slippage slip.csv --out cost/
```

`clean` 现在额外产出 `gate.json`（pass/warn/blocked）与 `provenance.json`（spec 版本 + 规则哈希 + 配置指纹 + 产物清单）。
