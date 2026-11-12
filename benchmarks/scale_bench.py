"""规模基准：合成 N 只 × D 天的面板，逐阶段计时（不启用 tracemalloc，避免拖慢）。

用法：
    python benchmarks/scale_bench.py --codes 800 --days 2500
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from qrg.calendar.regime import RegimeCalendar
from qrg.calendar.session import SessionCalendar
from qrg.clean.grid import build_grid
from qrg.clean.pit import pit_merge
from qrg.clean.lookahead import replay_check
from qrg.clean.validate import validate_panel
from qrg.master.security import SecurityMasterImpl
from qrg.quality.normalize import normalize_panel
from qrg.spec import load_spec

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def make_panel(codes: int, days: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=days)
    code_arr = np.array([f"{600000 + i:06d}" for i in range(codes)])
    base = rng.uniform(5, 50, size=codes)
    ret = rng.normal(0, 0.02, size=(days, codes))
    close = base * np.exp(np.cumsum(ret, axis=0))
    grid_d, grid_c = np.meshgrid(dates, code_arr, indexing="ij")
    flat_close = close.ravel()
    return pd.DataFrame(
        {
            "trade_date": grid_d.ravel(),
            "code": grid_c.ravel(),
            "open": flat_close,
            "high": flat_close * 1.01,
            "low": flat_close * 0.99,
            "close": flat_close,
            "volume": rng.integers(1e5, 1e7, size=days * codes).astype("float64"),
            "amount": flat_close * rng.integers(1e5, 1e7, size=days * codes),
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=400)
    ap.add_argument("--days", type=int, default=500)
    args = ap.parse_args()

    spec = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()
    panel = make_panel(args.codes, args.days)
    print(f"rows={len(panel):,} codes={args.codes} days={args.days}")
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    panel, _ = normalize_panel(panel)
    timings["normalize"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    validate_panel(panel, spec)
    timings["validate"] = time.perf_counter() - t0

    dates = sorted(pd.to_datetime(panel["trade_date"]).dt.normalize().unique())
    calendar = SessionCalendar([d.date() for d in dates])
    master = SecurityMasterImpl(
        events=[
            {"code": c, "event": "listed", "effective_date": "2010-01-04", "payload": {"name": c}}
            for c in sorted(panel["code"].unique())
        ]
    )
    regime = RegimeCalendar()
    start, end = dates[0].date(), dates[-1].date()

    t0 = time.perf_counter()
    build_grid(panel, calendar, master, regime, start, end, spec)
    timings["grid"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    pit_merge(panel, None, None, spec)
    timings["pit"] = time.perf_counter() - t0

    def _ret(df: pd.DataFrame) -> pd.DataFrame:
        out = df.sort_values(["code", "trade_date"]).copy()
        out["ret"] = out.groupby("code", observed=True)["close"].pct_change()
        return out

    t0 = time.perf_counter()
    replay_check(panel, _ret, feature_cols=["ret"], max_dates=10)
    timings["lookahead10"] = time.perf_counter() - t0

    try:
        import psutil

        mem = psutil.Process().memory_info()
        peak_mb = round(getattr(mem, "peak_wset", mem.rss) / 1e6, 1)
    except Exception:  # pragma: no cover
        peak_mb = None
    timings["peak_mb"] = peak_mb or 0.0

    for name, dt in timings.items():
        print(f"{name:12s} {dt:8.2f}s")
    print(json.dumps({"rows": len(panel), "timings": {k: round(v, 2) for k, v in timings.items()}}))


if __name__ == "__main__":
    main()
