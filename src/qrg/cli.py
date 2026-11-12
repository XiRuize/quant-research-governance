"""qrg command line.

阶段约定（按依赖顺序）：
    normalize -> validate -> adjust -> grid -> pit -> lookahead -> artifacts

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
    clean.add_argument("--scan", help="静态未来函数扫描目录/文件")
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

        calendar = SessionCalendar.from_parquet(args.sessions) if args.sessions.endswith(".parquet") else SessionCalendar.from_csv(args.sessions)
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
    flags, r = grid_stage(panel, calendar, master, regime, start, end, spec)  # type: ignore[operator]
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
        reports.append(lookahead(panel, _ret, feature_cols=["ret"]))  # type: ignore[operator]
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

    summary = {
        "ok": all(r.ok for r in reports),
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "clean":
        return cmd_clean(args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
