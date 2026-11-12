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
    clean.add_argument("--replay-step", type=int, default=1, help="回放审计抽样步长（1=每个交易日）")

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

    reports: list[Any] = []
    steps: list[str] = []

    normalize = _stage("normalize")
    validate = _stage("validate")
    adjust = _stage("adjust")
    grid_stage = _stage("grid")
    pit = _stage("pit")
    lookahead = _stage("lookahead")
    artifacts = _stage("artifacts")
    if not all([normalize, validate, adjust, grid_stage, pit, lookahead, artifacts]):
        missing = [n for n in PIPELINE_ORDER if _stage(n) is None]
        print(f"pipeline incomplete, missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    panel, r = normalize(panel)  # type: ignore[operator]
    reports.append(r)
    steps.extend(["normalize", "validate"])

    reports.append(validate(panel, spec))  # type: ignore[operator]

    if args.factors:
        factors = read_table(args.factors)
        panel, r = adjust(panel, factors, spec)  # type: ignore[operator]
        reports.append(r)
        steps.append("adjust")
    else:
        print("warn: --factors not given, skip adjust", file=sys.stderr)

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

    start = pd.to_datetime(panel["trade_date"]).min().date()
    end = pd.to_datetime(panel["trade_date"]).max().date()
    no_limit = read_table(args.no_limit) if args.no_limit else None
    suspensions = read_table(args.suspensions) if args.suspensions else None
    flags, r = grid_stage(  # type: ignore[operator]
        panel, calendar, master, regime, start, end, spec, no_limit=no_limit, suspensions=suspensions
    )
    reports.append(r)
    steps.append("grid")

    financial = read_table(args.financial) if args.financial else None
    industry = None
    if args.industry_rules:
        from .calendar.industry import IndustryCalendarImpl

        industry = IndustryCalendarImpl(path=args.industry_rules)
    panel, r = pit(panel, financial, industry, spec)  # type: ignore[operator]
    reports.append(r)
    steps.append("pit")

    def _ret(df: pd.DataFrame) -> pd.DataFrame:
        out = df.sort_values(["code", "trade_date"]).copy()
        out["ret"] = out.groupby("code", observed=True)["close"].pct_change()
        return out

    if {"close", "trade_date", "code"}.issubset(panel.columns):
        replay_dates = None
        if args.replay_step and args.replay_step > 1:
            all_days = sorted(pd.to_datetime(panel["trade_date"]).dt.date.unique())
            replay_dates = all_days[:: args.replay_step]
        reports.append(lookahead(panel, _ret, dates=replay_dates, feature_cols=["ret"]))  # type: ignore[operator]
        steps.append("lookahead")

    if args.scan:
        from .clean.lookahead import scan_source

        reports.append(scan_source(args.scan))

    manifest = artifacts(  # type: ignore[operator]
        args.out,
        spec,
        reports,
        flags,
        sensitivity={"steps": steps, "rows": int(len(panel))},
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
        "rows": int(len(panel)),
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
