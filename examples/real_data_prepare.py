"""把本地真实数据转成 qrg 的输入（examples 用，不属于库本身）。

用法：
    python examples/real_data_prepare.py --data-root "C:/.../量化研究/数据源/data" \
        --start 2026-09-01 --end 2026-09-30 --out examples/_real/sept [--with-factors]

产出：
    sessions.csv     交易日（来自 calendar.parquet）
    security.json    证券主数据事件（listed + 退市 proxy）
    industry.json    行业 as-of 规则（来自 industry_pit_sw.parquet）
    panel.parquet    行情（new_bars 切片，列名对齐 qrg）
    factors.parquet  复权因子（可选，由 daily_factors / new_bars 反推）
    st_snapshot.json ST 快照计数（仅信息，不作为历史事件）
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
    ap.add_argument("--with-factors", action="store_true")
    args = ap.parse_args()

    root = Path(args.data_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)

    # ---------------------------------------------------------------- 日历
    cal = pd.read_parquet(root / "ref" / "calendar" / "calendar.parquet")
    sessions = cal.loc[(cal["is_trading_day"]) & (cal["trade_date"].between(start, end)), ["trade_date"]]
    sessions.sort_values("trade_date").to_csv(out / "sessions.csv", index=False)

    # ---------------------------------------------------------------- 行情（全量，用于退市推断）
    bars_all = pd.read_parquet(root / "ref" / "new_bars" / "new_bars.parquet")
    bars_all["date"] = pd.to_datetime(bars_all["date"])
    bars_all["code"] = bars_all["code"].astype(str).str.zfill(6)
    bars_all = bars_all.loc[bars_all["code"].map(is_a_share)].copy()
    last_trade = bars_all.groupby("code")["date"].max()

    # ---------------------------------------------------------------- 主数据
    sb = pd.read_parquet(root / "ref" / "stock_basic" / "stock_basic.parquet")
    sb = sb.dropna(subset=["SECURITY_CODE", "LISTING_DATE"]).copy()
    sb["SECURITY_CODE"] = sb["SECURITY_CODE"].astype(str).str.zfill(6)
    sb = sb.loc[sb["SECURITY_CODE"].map(is_a_share)].copy()

    events: list[dict] = []
    delisted_proxy = 0
    excluded_no_bar = 0
    for row in sb.itertuples(index=False):
        code = row.SECURITY_CODE
        listed = pd.Timestamp(row.LISTING_DATE).strftime("%Y-%m-%d")
        state = int(row.LISTING_STATE) if pd.notna(row.LISTING_STATE) else 0
        if state == 2:  # 已退市
            if code in last_trade.index:
                # 退市日期不可得：用最后交易日 + 1 天作为 proxy，并在 dataset 里标明
                delist_day = (last_trade[code] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                events.append(
                    {"code": code, "event": "listed", "effective_date": listed, "payload": {"name": str(row.SECURITY_NAME_ABBR)}}
                )
                events.append(
                    {
                        "code": code,
                        "event": "delisted",
                        "effective_date": delist_day,
                        "payload": {"proxy": True, "source": "last_bar+1d"},
                    }
                )
                delisted_proxy += 1
            else:
                # 数据起点前就已无成交：无法定位退市日，整只剔除并计数
                excluded_no_bar += 1
            continue
        events.append({"code": code, "event": "listed", "effective_date": listed, "payload": {"name": str(row.SECURITY_NAME_ABBR)}})

    security = {
        "board_prefixes": {"main": ["60", "00"], "star": ["68"], "chinext": ["30"], "bse": ["8", "4", "920"]},
        "defaults": {"lot_size": 100, "tick_size": 0.01},
        "events": events,
        "dataset": "local-stock_basic-snapshot; delist_date=proxy(last_bar+1d)",
    }
    (out / "security.json").write_text(json.dumps(security, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 行业
    ind = pd.read_parquet(root / "ref" / "industry" / "industry_pit_sw.parquet")
    bad_interval = ind["effective_to"] <= ind["effective_from"]
    dropped_intervals = int(bad_interval.sum())
    ind = ind.loc[~bad_interval].copy()
    versions = []
    for version, grp in ind.groupby("classification_version"):
        versions.append(
            {"version": str(version), "effective_from": grp["effective_from"].min().strftime("%Y-%m-%d"), "effective_to": None}
        )
    ind_events = []
    for row in ind.itertuples(index=False):
        ind_events.append(
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
    industry = {"versions": versions, "events": ind_events, "dataset": "local-industry_pit_sw"}
    (out / "industry.json").write_text(json.dumps(industry, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 面板
    bars = bars_all.loc[bars_all["date"].between(start, end)].copy()
    bars = bars.rename(columns={"date": "trade_date"})
    bars = bars[["trade_date", "code", "open", "high", "low", "close", "volume", "amount"]]
    bars.to_parquet(out / "panel.parquet", index=False)

    # ---------------------------------------------------------------- ST 快照（信息）
    st = pd.read_parquet(root / "ref" / "st" / "st_status.parquet")
    st_info = {
        "rows": int(len(st)),
        "is_st": int(st["is_st"].sum()),
        "is_delisted": int(st["is_delisted"].sum()),
        "snapshot_date": str(st["snapshot_date"].max())[:10] if "snapshot_date" in st.columns else None,
        "note": "仅快照，无 st_on/st_off 历史；不得回填历史",
    }
    (out / "st_snapshot.json").write_text(json.dumps(st_info, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 复权因子
    factor_rows = 0
    if args.with_factors:
        from qrg.master.actions import build_factor

        actions = pd.read_parquet(root / "ref" / "adjust" / "fhps_events.parquet")
        actions = actions.dropna(subset=["ex_date"]).copy()
        actions["code"] = actions["code"].astype(str).str.zfill(6)
        actions["ex_date"] = pd.to_datetime(actions["ex_date"])

        prices = bars_all[["code", "date", "close"]].rename(columns={"date": "trade_date"})
        prices = prices.sort_values(["code", "trade_date"])
        first_price = prices.groupby("code")["trade_date"].min()
        actions = actions.loc[actions["code"].isin(first_price.index)].copy()
        # 事件必须有 ex_date 之前的收盘价，否则跳过并计数
        actions = actions.loc[
            actions.apply(lambda r: r["ex_date"] > first_price[r["code"]] + pd.Timedelta(days=3), axis=1)
        ].copy()

        events = []
        for row in actions.itertuples(index=False):
            ex = pd.Timestamp(row.ex_date).strftime("%Y-%m-%d")
            if pd.notna(row.cash_per_share) and row.cash_per_share > 0:
                events.append(
                    {"code": row.code, "ex_date": ex, "event": "cash_dividend", "payload": {"dividend": float(row.cash_per_share)}}
                )
            if pd.notna(row.share_ratio) and row.share_ratio > 0:
                events.append(
                    {"code": row.code, "ex_date": ex, "event": "stock_dividend", "payload": {"shares": float(row.share_ratio)}}
                )
        factors = build_factor(events, prices=prices)
        factors = factors.loc[factors["trade_date"].between(start, end)]
        factors.to_parquet(out / "factors.parquet", index=False)
        factor_rows = int(len(factors))

    print(
        json.dumps(
            {
                "sessions": int(len(sessions)),
                "security_events": len(security["events"]),
                "delisted_proxy": delisted_proxy,
                "excluded_no_bar": excluded_no_bar,
                "industry_events": len(industry["events"]),
                "dropped_zero_length_intervals": dropped_intervals,
                "panel_rows": int(len(bars)),
                "codes": int(bars["code"].nunique()),
                "factor_rows": factor_rows,
                "st_snapshot": st_info,
                "range": [str(bars["trade_date"].min().date()), str(bars["trade_date"].max().date())],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
