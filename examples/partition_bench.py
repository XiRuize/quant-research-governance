"""分区跑 vs 单次跑的内存/耗时对比。

用法：python examples/partition_bench.py --codes 3200 --days 1000
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "src"))

from scale_bench import make_panel  # noqa: E402

from qrg.calendar.regime import RegimeCalendar  # noqa: E402
from qrg.calendar.session import SessionCalendar  # noqa: E402
from qrg.master.security import SecurityMasterImpl  # noqa: E402
from qrg.pipeline import clean_panel, clean_partitioned  # noqa: E402
from qrg.spec import load_spec  # noqa: E402


def peak_mb() -> float:
    import psutil

    mem = psutil.Process().memory_info()
    return round(getattr(mem, "peak_wset", mem.rss) / 1e6, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=1600)
    ap.add_argument("--days", type=int, default=1000)
    ap.add_argument("--partition", action="store_true")
    args = ap.parse_args()

    spec = load_spec(ROOT / "spec" / "cleaning.spec.json").to_panel_spec()
    panel = make_panel(args.codes, args.days)
    dates = sorted(pd.to_datetime(panel["trade_date"]).dt.normalize().unique())
    calendar = SessionCalendar([d.date() for d in dates])
    master = SecurityMasterImpl(
        events=[
            {"code": c, "event": "listed", "effective_date": "2010-01-04", "payload": {"name": c}}
            for c in sorted(panel["code"].unique())
        ]
    )
    regime = RegimeCalendar()

    from pathlib import Path

    t0 = time.perf_counter()
    runner = clean_partitioned if args.partition else clean_panel
    flags_dir = Path(".qrg_parts_bench") if args.partition else None
    if flags_dir is not None:
        flags_dir.mkdir(parents=True, exist_ok=True)
    result = runner(
        panel,
        spec=spec,
        calendar=calendar,
        master=master,
        regime=regime,
        replay_step=0,
        replay_budget=10,
        flags_out_dir=flags_dir,
    )
    elapsed = time.perf_counter() - t0
    print(
        {
            "rows": len(panel),
            "partition": args.partition,
            "seconds": round(elapsed, 1),
            "peak_mb": peak_mb(),
            "flags": len(result.flags),
            "panel": len(result.panel),
        }
    )


if __name__ == "__main__":
    main()
