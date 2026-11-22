"""qrg command line.

阶段约定（按依赖顺序）：
    normalize -> validate -> adjust -> grid -> pit -> lookahead -> artifacts -> gate -> provenance

子命令：
    clean     按契约执行清洗管线
    simulate  成交模拟（orders + flags + panel -> fills）
    cost      成本归因（fills (+ slippage) -> attribution）
    stages    查看管线就绪状态

退出码：0 通过；2 管线不完整；3 被门禁拦截；4 输入/契约错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from . import __version__

PIPELINE_ORDER: tuple[str, ...] = (
    "normalize",
    "validate",
    "adjust",
    "grid",
    "pit",
    "lookahead",
    "artifacts",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage(name: str) -> Callable[..., object] | None:
    """惰性解析阶段实现；模块未就绪时返回 None。"""
    try:
        if name == "normalize":
            from .quality.normalize import normalize_panel

            return normalize_panel
        if name == "validate":
            from .clean.validate import validate_panel

            return validate_panel
        if name == "adjust":
            from .clean.adjust import adjust_panel

            return adjust_panel
        if name == "grid":
            from .clean.grid import build_grid

            return build_grid
        if name == "pit":
            from .clean.pit import pit_merge

            return pit_merge
        if name == "lookahead":
            from .clean.lookahead import replay_check

            return replay_check
        if name == "artifacts":
            from .clean.artifacts import write_artifacts

            return write_artifacts
    except ImportError:
        return None
    return None


def read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        return pd.read_parquet(p)
    if p.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(p)
    raise ValueError(f"unsupported table suffix: {p.suffix}")


def _write_json(path: str | Path, payload: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return p


def _fallback_master(panel: pd.DataFrame) -> Any:
    from .master.security import SecurityMasterImpl

    first = pd.to_datetime(panel["trade_date"]).min().strftime("%Y-%m-%d")
    codes = sorted(panel["code"].astype(str).unique())
    events = [{"code": c, "event": "listed", "effective_date": first, "payload": {"name": c}} for c in codes]
    return SecurityMasterImpl(events=events)


def _fallback_calendar(panel: pd.DataFrame) -> Any:
    from .calendar.session import SessionCalendar

    days = sorted(pd.to_datetime(panel["trade_date"]).dt.date.unique())
    return SessionCalendar(list(days))


def cmd_version(_: argparse.Namespace) -> int:
    print(f"qrg {__version__}")
    return 0


def cmd_stages(_: argparse.Namespace) -> int:
    for name in PIPELINE_ORDER:
        impl = _stage(name)
        print(f"{name:10s} {'ready' if impl else 'pending'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qrg", description="量化研究数据清洗/治理标准库")
    parser.add_argument("--version", action="version", version=f"qrg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="打印版本").set_defaults(func=cmd_version)
    sub.add_parser("stages", help="列出管线阶段与就绪状态").set_defaults(func=cmd_stages)

    clean = sub.add_parser("clean", help="按契约执行清洗管线")
    clean.add_argument("--spec", required=True)
    clean.add_argument("--input", required=True)
    clean.add_argument("--out", required=True)
    clean.add_argument("--factors", help="复权因子表 (code,trade_date,factor)")
    clean.add_argument("--financial", help="PIT 财务长表 (code,notice_date,item,value)")
    clean.add_argument("--industry-rules", help="行业日历规则 json")
    clean.add_argument("--security-rules", help="证券主数据规则 json")
    clean.add_argument("--sessions", help="交易日列表 csv/parquet")
    clean.add_argument("--regime-rules", help="制度日历规则 json")
    clean.add_argument("--rules-dir", default=str(REPO_ROOT / "rules"), help="规则目录（provenance 哈希用）")
    clean.add_argument("--run-id", help="运行 id；缺省由产物 hash 派生")
    clean.add_argument("--registry", help="trial registry 目录（写入一条试验记录）")
    clean.add_argument("--scan", help="静态未来函数扫描目录/文件")
    clean.add_argument("--no-limit", help="无涨跌幅限制日表 (code,trade_date)")
    clean.add_argument("--suspensions", help="停复牌区间表 (code,start_date,end_date)")
    clean.add_argument("--market-cap", help="市值/估值表 (code,trade_date,total_mv,circ_mv,...)")
    clean.add_argument("--vintages", help="数据 vintage 清单 json（adapter 的 vintages.json）")
    clean.add_argument("--partition-year", action="store_true", help="按自然年分区处理以控内存")
    clean.add_argument("--replay-step", type=int, default=0, help="回放抽样步长：0=自动(≤60日)，1=全量，N=每N日")

    gate = sub.add_parser("gate", help="校验产物 hash 与 provenance")
    gate.add_argument("--dir", required=True, help="clean 的产物目录")

    simulate = sub.add_parser("simulate", help="成交模拟")
    simulate.add_argument("--orders", required=True, help="orders csv: order_id,trade_date,code,side,qty,ref_price")
    simulate.add_argument("--flags", required=True, help="build_grid 产出的 flags parquet")
    simulate.add_argument("--panel", required=True, help="行情 parquet/csv（volume/amount）")
    simulate.add_argument("--out", required=True)
    simulate.add_argument("--participation-cap", type=float, default=0.1)
    simulate.add_argument("--price-basis", choices=["vwap", "close"], default="vwap")
    simulate.add_argument("--allow-t0", action="store_true", help="关闭 T+1 限制（默认开启）")

    cost = sub.add_parser("cost", help="成本归因")
    cost.add_argument("--fills", required=True, help="fills csv: order_id,trade_date,code,side,qty,price")
    cost.add_argument("--slippage", help="slippage csv（order_id,impact_bps,delay_bps,spread_bps,overnight_bps,residual_bps）")
    cost.add_argument("--out", required=True)
    cost.add_argument("--regime-rules", help="制度日历规则 json")

    cap = sub.add_parser("capacity", help="容量曲线")
    cap.add_argument("--weights", required=True, help="weights csv: code,weight")
    cap.add_argument("--panel", required=True, help="行情 parquet/csv（amount/volume）")
    cap.add_argument("--out", required=True)
    cap.add_argument("--gross-alpha-bps", type=float, required=True)
    cap.add_argument("--turnover", type=float, default=1.0)
    cap.add_argument("--sigma", type=float, default=1.0)
    cap.add_argument("--adv-window", type=int, default=20)
    cap.add_argument("--impact-rules", help="冲击模型规则 json")
    cap.add_argument("--regime-rules", help="制度日历规则 json")

    tail = sub.add_parser("tail", help="尾部情景")
    tail.add_argument("--curve", required=True, help="capacity 产出的 curve parquet")
    tail.add_argument("--out", required=True)
    tail.add_argument("--scenarios", help="scenarios json（缺省用内置三情景）")
    tail.add_argument("--impact-rules", help="冲击模型规则 json")
    tail.add_argument("--returns", help="收益序列 csv，用于 bootstrap 尾部估计")

    # ---- data：数据集登记与体检 ---------------------------------
    run = sub.add_parser("run", help="研究轮次：结构与 run.json 校验")
    rsub = run.add_subparsers(dest="run_command", required=True)
    r_check = rsub.add_parser("check", help="按 run.spec.json 检查全部 run（v1 必填 / legacy 门）")
    r_check.add_argument("--research", help="research 根目录，默认仓库同级 research/")
    r_check.add_argument("--json", action="store_true", help="以 JSON 输出 findings")

    data = sub.add_parser("data", help="数据集：登记、查询、现场探测、体检")
    dsub = data.add_subparsers(dest="data_command", required=True)

    d_init = dsub.add_parser("init", help="由旧四表生成 datasets.json（可重复执行，幂等）")
    d_init.add_argument("--from-legacy", action="store_true", help="从 catalog/views/lineage/sources 合并")
    d_init.add_argument("--data-root", help="数据集根，默认仓库同级 数据源/")
    d_init.add_argument("--out", help="输出路径，默认 <data-root>/data/datasets.json")
    d_init.add_argument("--dry-run", action="store_true", help="只报告将生成多少条，不写文件")

    d_cons = dsub.add_parser(
        "consumers", help="重扫「谁用了这张表」（增量刷新，不动其余字段）"
    )
    d_cons.add_argument("--registry", help="datasets.json 路径")
    d_cons.add_argument("--data-root", help="数据集根，默认仓库同级 数据源/")
    d_cons.add_argument("--dry-run", action="store_true", help="只报告差异，不写文件")

    d_add = dsub.add_parser("add", help="追加一条数据集登记（唯一合法入口）")
    d_add.add_argument("--id", required=True, dest="dataset_id", help="数据集 id（唯一）")
    d_add.add_argument("--path", required=True, help="相对 数据源/ 的路径")
    d_add.add_argument("--status", default="AVAILABLE", help="8 档状态之一")
    d_add.add_argument("--kind", default="dataset", help="dataset / dimension / panel / event_table")
    d_add.add_argument("--caliber", default="", help="这张表能回答什么问题")
    d_add.add_argument("--source", help="源 id（须在 sources 字典内）")
    d_add.add_argument("--certainty", default="unknown", help="exact / inferred / unknown")
    d_add.add_argument("--gap", action="append", help="已知缺口，可重复")
    d_add.add_argument("--registry", help="datasets.json 路径")
    d_add.add_argument("--data-root", help="数据集根，默认仓库同级 数据源/")
    d_add.add_argument("--dry-run", action="store_true", help="只校验不写")

    d_refs = dsub.add_parser("refs", help="谁引用了某个文件（删前必查）")
    d_refs.add_argument("target", help="相对路径、绝对路径或文件名")
    d_refs.add_argument("--data-root", help="数据集根，默认仓库同级 数据源/")

    d_orph = dsub.add_parser(
        "orphans", help="跨 run 隐式依赖：某 run 产出物被其他 run 引用"
    )
    d_orph.add_argument("--data-root", help="数据集根，默认仓库同级 数据源/")
    d_orph.add_argument("--json", action="store_true", dest="as_json")

    d_list = dsub.add_parser("list", help="列出已登记数据集")
    d_list.add_argument("--filter", help="按 id 子串过滤")
    d_list.add_argument("--status", help="按 status 过滤")
    d_list.add_argument("--registry", help="datasets.json 路径")

    d_stat = dsub.add_parser("stat", help="现场探测一个数据集（不落盘）")
    d_stat.add_argument("id", help="数据集 id")
    d_stat.add_argument("--registry", help="datasets.json 路径")
    d_stat.add_argument("--fast", action="store_true", help="跳过覆盖期统计（大表提速）")
    d_stat.add_argument("--json", action="store_true", dest="as_json")

    d_lint = dsub.add_parser("lint", help="按范式卡 P01–P11 体检数据集")
    d_lint.add_argument("--ids", nargs="*", help="指定数据集 id；缺省全量")
    d_lint.add_argument("--filter", help="按 id 子串过滤")
    d_lint.add_argument("--registry", help="datasets.json 路径")
    d_lint.add_argument("--level", choices=["fatal", "warn", "info"], default="warn")
    d_lint.add_argument("--max-rows", type=int, default=4_000_000, help="单表采样上限")
    d_lint.add_argument("--json", action="store_true", dest="as_json")
    d_lint.add_argument("--fail-on", choices=["fatal", "warn", "none"], default="fatal",
                        help="退出码策略：fatal=有 fatal 则 5；warn=有 warn 则 5；none=恒 0")

    d_check = dsub.add_parser("check", help="校验登记表（主键/路径/schema/枚举）")
    d_check.add_argument("--registry", help="datasets.json 路径")
    d_check.add_argument("--no-verify-paths", action="store_true", help="跳过文件存在性检查")
    d_check.add_argument("--json", action="store_true", dest="as_json")
    return parser


def cmd_clean(args: argparse.Namespace) -> int:
    from .api import GateBlocked, SpecError
    from .spec import load_spec

    try:
        doc = load_spec(args.spec)
        spec = doc.to_panel_spec()
        panel = read_table(args.input)
    except (SpecError, ValueError, OSError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 4

    from .pipeline import clean_panel, clean_partitioned

    artifacts = _stage("artifacts")
    if artifacts is None:
        print("pipeline incomplete, missing: artifacts", file=sys.stderr)
        return 2

    if args.sessions:
        from .calendar.session import SessionCalendar

        if args.sessions.endswith(".parquet"):
            calendar = SessionCalendar.from_parquet(args.sessions)
        else:
            calendar = SessionCalendar.from_csv(args.sessions)
    else:
        calendar = _fallback_calendar(panel)

    if args.security_rules:
        from .master.security import SecurityMasterImpl

        master = SecurityMasterImpl(path=args.security_rules)
    else:
        master = _fallback_master(panel)

    from .calendar.regime import RegimeCalendar

    regime = RegimeCalendar(path=args.regime_rules) if args.regime_rules else RegimeCalendar()

    industry = None
    if args.industry_rules:
        from .calendar.industry import IndustryCalendarImpl

        industry = IndustryCalendarImpl(path=args.industry_rules)

    factors = read_table(args.factors) if args.factors else None
    if factors is None:
        print("warn: --factors not given, skip adjust", file=sys.stderr)
    financial = read_table(args.financial) if args.financial else None
    no_limit = read_table(args.no_limit) if args.no_limit else None
    suspensions = read_table(args.suspensions) if args.suspensions else None
    market_cap = read_table(args.market_cap) if args.market_cap else None
    if market_cap is not None and args.partition_year:
        print("error: --market-cap 暂不支持与 --partition-year 同时使用", file=sys.stderr)
        return 4
    vintages = None
    if args.vintages:
        vintages = json.loads(Path(args.vintages).read_text(encoding="utf-8"))

    runner = clean_partitioned if args.partition_year else clean_panel
    result = runner(
        panel,
        spec=spec,
        calendar=calendar,
        master=master,
        regime=regime,
        industry=industry,
        financial=financial,
        factors=factors,
        no_limit=no_limit,
        suspensions=suspensions,
        market_cap=market_cap,
        replay_step=args.replay_step,
        replay_budget=(60 if len(panel) <= 2_000_000 else 10),
    )
    panel, flags, reports, steps = result.panel, result.flags, result.reports, result.steps
    total_rows = result.rows or int(len(panel))

    from .quality.coverage import build_coverage

    coverage_panel = panel if not panel.empty else pd.DataFrame({"trade_date": [], "code": []})
    coverage, coverage_report = build_coverage(
        coverage_panel,
        factors=factors,
        financial=financial,
        market_cap=market_cap,
        no_limit=no_limit,
        suspensions=suspensions,
        vintages=vintages,
    )
    if panel.empty:
        coverage["window"] = {"start": None, "end": None, "rows": total_rows}
    reports.append(coverage_report)

    if args.scan:
        from .clean.lookahead import scan_source

        reports.append(scan_source(args.scan))

    manifest = artifacts(  # type: ignore[operator]
        args.out,
        spec,
        reports,
        flags,
        sensitivity={"steps": steps, "rows": total_rows},
        panel=result.panel,
        coverage=coverage,
    )

    from .gov.gate import decide, write_decision

    decision = decide(reports, manifest)
    write_decision(args.out, decision)

    from .gov.provenance import collect, write_record

    run_id = args.run_id or f"run-{manifest['data_quality_report.json'].split(':')[-1][:12]}"
    record = collect(
        run_id,
        spec.spec_version,
        args.rules_dir,
        manifest,
        {"steps": steps, "rows": int(len(panel))},
    )
    write_record(args.out, record)

    if args.registry:
        from .gov.registry import TrialRegistry

        trial = TrialRegistry(args.registry)
        trial.register(
            spec_version=spec.spec_version,
            config={"steps": steps, "rows": int(len(panel))},
            note=f"qrg clean {run_id}",
            run_id=run_id,
        )

    summary = {
        "ok": decision.status != "blocked",
        "gate": decision.status,
        "run_id": run_id,
        "spec_version": spec.spec_version,
        "steps": steps,
        "rows": total_rows,
        "manifest": manifest,
        "hits": [h.to_dict() for r in reports for h in r.hits],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    try:
        for r in reports:
            r.raise_if_blocked()
    except GateBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 3
    return 0


def _read_orders(path: str) -> list[Any]:
    from .api import Order, Side

    df = read_table(path)
    required = {"order_id", "trade_date", "code", "side", "qty", "ref_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"orders missing columns: {sorted(missing)}")
    orders = []
    for row in df.itertuples(index=False):
        orders.append(
            Order(
                trade_date=pd.Timestamp(row.trade_date).date(),
                code=str(row.code),
                side=Side(str(row.side)),
                qty=float(row.qty),
                ref_price=float(row.ref_price),
                order_id=str(row.order_id),
            )
        )
    return orders


def cmd_simulate(args: argparse.Namespace) -> int:
    from .api import SpecError
    from .exec.simulator import simulate

    try:
        orders = _read_orders(args.orders)
        flags = read_table(args.flags)
        panel = read_table(args.panel)
    except (SpecError, ValueError, OSError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 4

    fills, report = simulate(
        orders,
        flags,
        panel,
        price_basis=args.price_basis,
        participation_cap=args.participation_cap,
        t_plus_1=not args.allow_t0,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([f.__dict__ for f in fills])
    if "side" in frame.columns:
        frame["side"] = frame["side"].astype(str)
    frame.to_parquet(out / "fills.parquet", index=False)
    _write_json(out / "exec_report.json", report.to_dict())
    print(json.dumps({"ok": report.ok, "fills": len(fills), "report": report.to_dict()}, ensure_ascii=False, indent=2))
    return 0 if report.ok else 3


def cmd_cost(args: argparse.Namespace) -> int:
    from .api import Fill, Side
    from .calendar.regime import RegimeCalendar
    from .cost.attribution import aggregate, attribute_fills

    try:
        df = read_table(args.fills)
        required = {"order_id", "trade_date", "code", "side", "qty", "price"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"fills missing columns: {sorted(missing)}")
        fills = [
            Fill(
                order_id=str(r.order_id),
                trade_date=pd.Timestamp(r.trade_date).date(),
                code=str(r.code),
                side=Side(str(r.side)),
                qty=float(r.qty),
                price=float(r.price),
            )
            for r in df.itertuples(index=False)
        ]
        slippage = read_table(args.slippage) if args.slippage else None
    except (ValueError, OSError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 4

    regime = RegimeCalendar(path=args.regime_rules) if args.regime_rules else RegimeCalendar()
    frame, report = attribute_fills(fills, regime, slippage)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out / "attribution.parquet", index=False)
    if not frame.empty:
        aggregate(frame, ("trade_date",)).to_parquet(out / "attribution_daily.parquet", index=False)
    _write_json(out / "cost_report.json", report.to_dict())
    print(json.dumps({"ok": report.ok, "fills": len(fills), "total_cost": float(frame["total_cost"].sum()) if not frame.empty else 0.0}, ensure_ascii=False, indent=2))
    return 0 if report.ok else 3


def cmd_capacity(args: argparse.Namespace) -> int:
    from .capacity.adv import compute_adv
    from .capacity.curve import capacity_curve
    from .cost.impact import load_impact

    try:
        panel = read_table(args.panel)
        weights_df = read_table(args.weights)
        missing = {"code", "weight"} - set(weights_df.columns)
        if missing:
            raise ValueError(f"weights missing columns: {sorted(missing)}")
        weights = {str(r.code): float(r.weight) for r in weights_df.itertuples(index=False)}
    except (ValueError, OSError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 4

    window = int(args.adv_window)
    adv_df, _ = compute_adv(panel, windows=(window,), min_periods=1)
    adv_col = f"adv_amount_{window}"
    last = adv_df.sort_values("trade_date").groupby("code", observed=True).tail(1)
    adv_map = {str(r.code): float(getattr(r, adv_col)) for r in last.itertuples(index=False) if pd.notna(getattr(r, adv_col))}

    from .calendar.regime import RegimeCalendar

    impact = load_impact(args.impact_rules) if args.impact_rules else load_impact()
    regime = RegimeCalendar(path=args.regime_rules) if args.regime_rules else RegimeCalendar()
    as_of = pd.to_datetime(panel["trade_date"]).max().date()

    curve, summary = capacity_curve(
        weights,
        adv_map,
        impact,
        regime,
        as_of,
        gross_alpha_bps=args.gross_alpha_bps,
        turnover=args.turnover,
        sigma=args.sigma,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    curve.to_parquet(out / "capacity_curve.parquet", index=False)
    _write_json(out / "capacity.json", summary)
    print(json.dumps({"ok": True, "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    from .api import SpecError
    from .cost.impact import load_impact
    from .tail.extremes import bootstrap_bands
    from .tail.scenarios import Scenario, run_scenarios

    try:
        base = read_table(args.curve)
        if args.scenarios:
            payload = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
            scenarios = [Scenario(**item) for item in payload]
        else:
            scenarios = [
                Scenario(name="normal"),
                Scenario(name="stress", vol_multiplier=2.0, adv_multiplier=0.5, seed=1),
                Scenario(name="illiquid", vol_multiplier=3.0, adv_multiplier=0.2, locked_ratio=0.3, seed=2),
            ]
    except (SpecError, ValueError, OSError, TypeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 4

    impact = load_impact(args.impact_rules) if args.impact_rules else load_impact()
    frame, report = run_scenarios(base, scenarios, impact)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out / "tail_curves.parquet", index=False)
    payload = {"report": report.to_dict(), "scenarios": [s.__dict__ for s in scenarios]}
    if args.returns:
        returns = read_table(args.returns)
        col = "ret" if "ret" in returns.columns else returns.columns[-1]
        payload["bootstrap"] = bootstrap_bands(returns[col].dropna())
    _write_json(out / "tail.json", payload)
    print(json.dumps({"ok": report.ok, "scenarios": len(scenarios)}, ensure_ascii=False, indent=2))
    return 0 if report.ok else 3


def cmd_gate(args: argparse.Namespace) -> int:
    """校验产物完整性：manifest hash + provenance + gate 结论。"""
    from .clean.artifacts import verify_manifest
    from .gov.provenance import load_record

    out = Path(args.dir)
    try:
        manifest_ok = verify_manifest(out)
        record = load_record(out)
        gate_payload = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        print(f"gate error: {exc}", file=sys.stderr)
        return 4

    status = gate_payload.get("status", "blocked")
    summary = {
        "manifest_ok": manifest_ok,
        "gate_status": status,
        "run_id": record.run_id,
        "spec_version": record.spec_version,
        "artifacts": len(record.artifacts),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if manifest_ok and status != "blocked" else 3


def cmd_data_init(args: argparse.Namespace) -> int:
    from .registry import RegistryError, build_from_legacy, default_data_root, default_registry_path

    if not args.from_legacy:
        print("目前只支持 --from-legacy（从 catalog/views/lineage/sources 合并）", file=sys.stderr)
        return 4
    root = Path(args.data_root) if args.data_root else default_data_root()
    try:
        obj = build_from_legacy(root)
    except RegistryError as exc:
        print(f"合并失败: {exc}", file=sys.stderr)
        return 4

    ds = obj["datasets"]
    by_status: dict[str, int] = {}
    for d in ds:
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
    by_cert: dict[str, int] = {}
    for d in ds:
        for e in d.get("lineage") or []:
            by_cert[e.get("certainty", "?")] = by_cert.get(e.get("certainty", "?"), 0) + 1

    out = Path(args.out) if args.out else default_registry_path(root)
    print(f"数据集 {len(ds)} 条 | 源 {len(obj['sources'])} 条")
    print("status 分布: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print("血缘 certainty: " + ", ".join(f"{k}={v}" for k, v in sorted(by_cert.items())))
    if args.dry_run:
        print(f"[dry-run] 未写文件；目标 {out}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {out}")
    return 0


def cmd_data_add(args: argparse.Namespace) -> int:
    from .registry import RegistryError, add_dataset

    try:
        res = add_dataset(
            args.registry,
            dataset_id=args.dataset_id,
            path=args.path,
            status=args.status,
            kind=args.kind,
            caliber=args.caliber,
            source_id=args.source,
            certainty=args.certainty,
            gaps=args.gap,
            data_root=args.data_root,
            dry_run=args.dry_run,
        )
    except RegistryError as exc:
        print(f"登记失败: {exc}", file=sys.stderr)
        return 4

    print(f"已登记 {args.dataset_id}（共 {res['total']} 条）")
    if not res["file_exists"]:
        print(f"  注意：文件 {args.path} 在磁盘上不存在；status={args.status} 会因此判 fatal", file=sys.stderr)
    if not args.caliber:
        print("  注意：caliber 为空，qrg data check 会报 warn", file=sys.stderr)
    if args.dry_run:
        print("[dry-run] 未写文件")
    else:
        print("  下一步：qrg data check 确认 0 fatal")
    return 0


def cmd_data_refs(args: argparse.Namespace) -> int:
    from .registry import find_references

    hits = find_references(args.target, args.data_root)
    if not hits:
        print(f"没有任何 run 或脚本引用 {args.target}")
        print("（这不等于可以删：可能是拼路径读的，先人工确认）")
        return 0
    print(f"{len(hits)} 处引用 {args.target}：")
    for h in hits:
        tag = "run   " if h["kind"] == "run" else "script"
        print(f"  [{tag}] {h['where']}")
    return 0


def cmd_data_orphans(args: argparse.Namespace) -> int:
    from .registry import cross_run_artifacts

    arts = cross_run_artifacts(args.data_root)
    if args.as_json:
        print(json.dumps(arts, ensure_ascii=False, indent=1))
        return 0
    if not arts:
        print("没有跨 run 隐式依赖。")
        return 0
    print(f"跨 run 隐式依赖 {len(arts)} 个（删除会断下游，且不在 datasets.json 里）")
    print(f"{'文件':30s} {'体积MB':>8s} {'拥有':32s} 引用")
    print("-" * 110)
    for a in arts:
        own = ",".join(a["owners"]) + (" [归属歧义]" if a["ambiguous_owner"] else "")
        print(f"{a['file']:30s} {a['mb']:8.1f} {own:32s} {','.join(a['used_by'][:5])}")
    print("\n删除前用 `qrg data refs <文件名>` 复核；这些文件应评估提升为 数据源/ 正式数据集。")
    return 0


def cmd_data_consumers(args: argparse.Namespace) -> int:
    from .registry import RegistryError, refresh_consumers

    try:
        changed = refresh_consumers(
            args.registry, data_root=args.data_root, dry_run=args.dry_run
        )
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 4

    if not changed:
        print("consumers 无变化。")
        return 0
    print(f"{len(changed)} 条数据集的 consumers 发生变化：")
    for did, diff in sorted(changed.items()):
        before = set(diff["before"])
        after = set(diff["after"])
        plus = sorted(after - before)
        minus = sorted(before - after)
        parts = []
        if plus:
            parts.append("+" + ",".join(plus))
        if minus:
            parts.append("-" + ",".join(minus))
        print(f"  {did:44s} {' '.join(parts)}")
    if args.dry_run:
        print("[dry-run] 未写文件")
    else:
        print("已写回 datasets.json")
    return 0


def cmd_data_list(args: argparse.Namespace) -> int:
    from .registry import STATUS_ENUM, load_registry

    try:
        reg = load_registry(args.registry)
    except Exception as exc:  # noqa: BLE001
        print(f"读取登记表失败: {exc}", file=sys.stderr)
        return 4

    rows = reg.datasets
    if args.filter:
        p = args.filter.lower()
        rows = [d for d in rows if p in d["id"].lower() or p in str(d.get("path", "")).lower()]
    if args.status:
        rows = [d for d in rows if d.get("status") == args.status]

    print(f"{'id':44s} {'status':14s} {'kind':10s} path")
    print("-" * 110)
    for d in rows:
        print(f"{d['id']:44s} {d.get('status', '?'):14s} {d.get('kind', '?'):10s} {d.get('path', '')}")
    print(f"\n合计 {len(rows)} 条；status 枚举: {', '.join(sorted(STATUS_ENUM))}")
    return 0


def cmd_data_stat(args: argparse.Namespace) -> int:
    from .registry import load_registry, probe

    try:
        reg = load_registry(args.registry)
        rec = reg.get(args.id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"读取登记表失败: {exc}", file=sys.stderr)
        return 4

    p = reg.data_root / str(rec["path"]).replace("\\", "/")
    pr = probe(p, full=not args.fast)
    consumers = reg.consumers_of(args.id)

    info = {
        "id": rec["id"],
        "path": rec["path"],
        "abs_path": str(p),
        "status": rec.get("status"),
        "kind": rec.get("kind"),
        "caliber": rec.get("caliber"),
        "registered": {
            "frequency": rec.get("frequency"),
            "pit": rec.get("pit"),
            "date_column": rec.get("date_column"),
            "symbol_column": rec.get("symbol_column"),
            "verified_at": rec.get("verified_at"),
        },
        "probed": pr.as_dict(),
        "consumers": consumers,
        "gaps": rec.get("gaps") or [],
        "lineage": rec.get("lineage") or [],
    }
    if args.as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f"id      {rec['id']}")
    print(f"path    {rec['path']}")
    print(f"status  {rec.get('status')}  ({rec.get('status_reason', '')})")
    print(f"kind    {rec.get('kind')} | frequency {rec.get('frequency')}")
    cal = rec.get("caliber")
    if isinstance(cal, dict):
        print("caliber " + "; ".join(f"{k}={v}" for k, v in list(cal.items())[:4]))
    elif cal:
        print(f"caliber {str(cal)[:120]}")
    print()
    if not pr.exists:
        print("现场探测: 文件不存在！")
    else:
        print(f"现场探测 ({pr.format})")
        print(f"  行数     {pr.rows:,}" if pr.rows is not None else "  行数     ?")
        print(f"  字段数   {pr.n_fields}")
        print(f"  体积     {pr.file_mb:.2f} MB" if pr.file_mb is not None else "  体积     ?")
        if pr.coverage:
            print(f"  覆盖     {json.dumps(pr.coverage, ensure_ascii=False)}")
        if pr.field_list:
            print(f"  字段     {', '.join(pr.field_list[:16])}" + (" ..." if len(pr.field_list) > 16 else ""))
        if pr.error:
            print(f"  错误     {pr.error}")
    print()
    print(f"被谁使用 ({len(consumers)}) " + (", ".join(consumers) if consumers else "无记录"))
    if rec.get("gaps"):
        print(f"\n已知缺口 ({len(rec['gaps'])})")
        for g in rec["gaps"][:6]:
            print(f"  - {g.get('text', g)}")
    return 0


def cmd_data_lint(args: argparse.Namespace) -> int:
    from .lint import lint_all, render, summarize
    from .registry import load_registry

    try:
        reg = load_registry(args.registry)
    except Exception as exc:  # noqa: BLE001
        print(f"读取登记表失败: {exc}", file=sys.stderr)
        return 4

    results = lint_all(
        reg, ids=args.ids, pattern=args.filter, max_rows=args.max_rows
    )
    if args.as_json:
        print(json.dumps(
            {
                "summary": summarize(results),
                "results": [
                    {
                        "dataset": r.dataset,
                        "path": r.path,
                        "rows_scanned": r.rows_scanned,
                        "sampled": r.sampled,
                        "worst": r.worst,
                        "findings": [
                            {"level": f.level, "code": f.code, "message": f.message}
                            for f in r.findings
                        ],
                        "skipped": r.skipped,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(render(results, level=args.level))

    if args.fail_on == "none":
        return 0
    n = sum(1 for r in results for f in r.findings if f.level == args.fail_on)
    return 5 if n else 0


def cmd_run_check(args: argparse.Namespace) -> int:
    from .run_check import check_all_runs, default_research_root, discover_runs, render_run_check

    root = Path(args.research) if args.research else default_research_root()
    n_runs = len(discover_runs(root))
    findings = check_all_runs(root)
    if args.json:
        print(json.dumps(
            [{"level": f.level, "code": f.code, "target": f.target, "message": f.message} for f in findings],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render_run_check(findings, n_runs))
    return 5 if any(f.level == "fatal" for f in findings) else 0


def cmd_data_check(args: argparse.Namespace) -> int:
    from .registry import check, load_registry, render_findings

    try:
        reg = load_registry(args.registry)
    except Exception as exc:  # noqa: BLE001
        print(f"读取登记表失败: {exc}", file=sys.stderr)
        return 4

    findings = check(reg, verify_paths=not args.no_verify_paths)
    if args.as_json:
        print(json.dumps(
            [{"level": f.level, "code": f.code, "target": f.target, "message": f.message} for f in findings],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render_findings(findings))
    return 5 if any(f.level == "fatal" for f in findings) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "clean":
        return cmd_clean(args)
    if args.command == "simulate":
        return cmd_simulate(args)
    if args.command == "cost":
        return cmd_cost(args)
    if args.command == "capacity":
        return cmd_capacity(args)
    if args.command == "tail":
        return cmd_tail(args)
    if args.command == "gate":
        return cmd_gate(args)
    if args.command == "run":
        return cmd_run_check(args)
    if args.command == "data":
        sub = {
            "init": cmd_data_init,
            "add": cmd_data_add,
            "consumers": cmd_data_consumers,
            "refs": cmd_data_refs,
            "orphans": cmd_data_orphans,
            "list": cmd_data_list,
            "stat": cmd_data_stat,
            "lint": cmd_data_lint,
            "check": cmd_data_check,
        }[args.data_command]
        return sub(args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
