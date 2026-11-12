"""把本地真实数据转成 qrg 的输入（examples 用，不属于库本身）。

用法：
    python examples/real_data_prepare.py --data-root "C:/.../量化研究/数据源/data" \
        --start 2026-09-01 --end 2026-09-30 --out examples/_real/sept

产出：
    sessions.csv     交易日（来自 calendar.parquet）
    security.json    证券主数据事件（listed，日期来自 stock_basic）
    industry.json    行业 as-of 规则（来自 industry_pit_sw.parquet）
    panel.parquet    行情（new_bars 切片，列名对齐 qrg）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

A_PREFIXES = ("60", "68", "00", "30", "8", "4", "920")


def is_a_share(code: str) -> bool:
    """只保留沪深 A 股/北交所，过滤 B 股、CDR 等。"""
    return len(code) == 6 and (code[:2] in A_PREFIXES or code[:3] == "920")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = Path(args.data_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)

    cal = pd.read_parquet(root / "ref" / "calendar" / "calendar.parquet")
    sessions = cal.loc[(cal["is_trading_day"]) & (cal["trade_date"].between(start, end)), ["trade_date"]]
    sessions.sort_values("trade_date").to_csv(out / "sessions.csv", index=False)

    sb = pd.read_parquet(root / "ref" / "stock_basic" / "stock_basic.parquet")
    sb = sb.dropna(subset=["SECURITY_CODE", "LISTING_DATE"]).copy()
    sb["SECURITY_CODE"] = sb["SECURITY_CODE"].astype(str).str.zfill(6)
    sb = sb.loc[sb["SECURITY_CODE"].map(is_a_share)].copy()
    events = []
    for row in sb.itertuples(index=False):
        events.append(
            {
                "code": row.SECURITY_CODE,
                "event": "listed",
                "effective_date": pd.Timestamp(row.LISTING_DATE).strftime("%Y-%m-%d"),
                "payload": {"name": str(row.SECURITY_NAME_ABBR)},
            }
        )
    security = {
        "board_prefixes": {"main": ["60", "00"], "star": ["68"], "chinext": ["30"], "bse": ["8", "4", "920"]},
        "defaults": {"lot_size": 100, "tick_size": 0.01},
        "events": events,
        "dataset": "local-stock_basic-snapshot",
    }
    (out / "security.json").write_text(json.dumps(security, ensure_ascii=False, indent=2), encoding="utf-8")

    ind = pd.read_parquet(root / "ref" / "industry" / "industry_pit_sw.parquet")
    # 真实数据存在零长度区间（from == to），对 as-of 无意义，统计后剔除
    bad_interval = ind["effective_to"] <= ind["effective_from"]
    dropped_intervals = int(bad_interval.sum())
    ind = ind.loc[~bad_interval].copy()
    versions = []
    for version, grp in ind.groupby("classification_version"):
        versions.append(
            {
                "version": str(version),
                "effective_from": grp["effective_from"].min().strftime("%Y-%m-%d"),
                "effective_to": None,
            }
        )
    events = []
    for row in ind.itertuples(index=False):
        events.append(
            {
                "code": str(row.code),
                "industry_code": str(row.sw_l1_code),
                "industry_name": str(row.sw_l1_name) if pd.notna(row.sw_l1_name) else f"SW{row.sw_l1_code}",
                "level": 1,
                "version": str(row.classification_version),
                "effective_from": pd.Timestamp(row.effective_from).strftime("%Y-%m-%d"),
                "effective_to": pd.Timestamp(row.effective_to).strftime("%Y-%m-%d"),
            }
        )
    industry = {"versions": versions, "events": events, "dataset": "local-industry_pit_sw"}
    (out / "industry.json").write_text(json.dumps(industry, ensure_ascii=False, indent=2), encoding="utf-8")

    bars = pd.read_parquet(root / "ref" / "new_bars" / "new_bars.parquet")
    bars["date"] = pd.to_datetime(bars["date"])
    bars = bars.loc[bars["date"].between(start, end)].copy()
    bars = bars.rename(columns={"date": "trade_date"})
    bars["code"] = bars["code"].astype(str).str.zfill(6)
    bars = bars.loc[bars["code"].map(is_a_share)].copy()
    bars = bars[["trade_date", "code", "open", "high", "low", "close", "volume", "amount"]]
    bars.to_parquet(out / "panel.parquet", index=False)

    print(
        json.dumps(
            {
                "sessions": int(len(sessions)),
                "security_events": len(security["events"]),
                "industry_events": len(industry["events"]),
                "dropped_zero_length_intervals": dropped_intervals,
                "panel_rows": int(len(bars)),
                "codes": int(bars["code"].nunique()),
                "range": [str(bars["trade_date"].min().date()), str(bars["trade_date"].max().date())],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
