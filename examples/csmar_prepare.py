"""用 CSMAR 原始表生成 qrg 输入（含北交所与 ST proxy）。

用法：
    python examples/csmar_prepare.py --csmar-root ".../external/csmar" \
        --data-root ".../数据源/data" --start 2026-09-01 --end 2026-09-30 --out examples/_real/csmar_sept

产出：
    sessions.csv     交易日（取窗口内 TRD_Dalyr 的交易日）
    panel.parquet    TRD_Dalyr：open/high/low/close/volume/amount（含 92x 北交所）
    security.json    listed（stock_basic 上市日）+ delisted（proxy）+ st_on/st_off（proxy，来自简称）
    factors.parquet  TRD_AdjustFactor 的 CumulateBwardFactor
    st_summary.json  ST proxy 统计（信息）
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

CODE_PREFIXES = ("60", "00", "30", "68", "92")
ST_RE = re.compile(r"^\s*(?:\*?ST|S\*?ST)")
COLUMNS = ["Stkcd", "Trddt", "Opnprc", "Hiprc", "Loprc", "Clsprc", "Dnshrtrd", "Dnvaltrd"]


def _code(series: pd.Series) -> pd.Series:
    return series.astype(str).str.zfill(6)


def _in_universe(code: str) -> bool:
    return len(code) == 6 and code[:2] in CODE_PREFIXES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csmar-root", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--st-lookback-days", type=int, default=400)
    args = ap.parse_args()

    csmar = Path(args.csmar_root)
    data_root = Path(args.data_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)

    # ---------------------------------------------------------------- 面板
    table = pq.read_table(
        csmar / "行情" / "TRD_Dalyr" / "data.parquet",
        columns=COLUMNS,
        filters=[("Trddt", ">=", start.date().isoformat()), ("Trddt", "<=", end.date().isoformat())],
    )
    bars = table.to_pandas()
    bars["code"] = _code(bars["Stkcd"])
    bars = bars.loc[bars["code"].map(_in_universe)].copy()
    panel = bars.rename(
        columns={
            "Trddt": "trade_date",
            "Opnprc": "open",
            "Hiprc": "high",
            "Loprc": "low",
            "Clsprc": "close",
            "Dnshrtrd": "volume",
            "Dnvaltrd": "amount",
        }
    )[["trade_date", "code", "open", "high", "low", "close", "volume", "amount"]]
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    panel = panel.dropna(subset=["trade_date", "code"]).sort_values(["trade_date", "code"])
    panel.to_parquet(out / "panel.parquet", index=False)

    sessions = pd.DataFrame({"trade_date": sorted(panel["trade_date"].unique())})
    sessions.to_csv(out / "sessions.csv", index=False)

    # ---------------------------------------------------------------- 主数据事件
    sb = pd.read_parquet(data_root / "ref" / "stock_basic" / "stock_basic.parquet")
    sb["code"] = _code(sb["SECURITY_CODE"])
    sb = sb.loc[sb["code"].map(_in_universe) & sb["LISTING_DATE"].notna()].copy()
    listed_map = dict(zip(sb["code"], pd.to_datetime(sb["LISTING_DATE"])))
    state_map = dict(
        zip(sb["code"], sb["LISTING_STATE"].fillna(0).astype(int))
    ) if "LISTING_STATE" in sb.columns else {}

    universe = sorted(set(panel["code"]))
    first_trade = panel.groupby("code")["trade_date"].min()

    events: list[dict] = []
    listing_proxy = 0
    for code in universe:
        if code in listed_map:
            listed_day = pd.Timestamp(listed_map[code])
        else:
            listed_day = pd.Timestamp(first_trade[code])
            listing_proxy += 1
        events.append(
            {
                "code": code,
                "event": "listed",
                "effective_date": listed_day.strftime("%Y-%m-%d"),
                "payload": {"proxy": code not in listed_map},
            }
        )

    # 退市：优先用 CSMAR 派生表（多源交叉），缺失才退回 last_trade+1d proxy
    delist_path = data_root / "ref" / "csmar" / "DERIVED_delist_dates.parquet"
    delist_map: dict[str, pd.Timestamp] = {}
    if delist_path.exists():
        dd = pd.read_parquet(delist_path).dropna(subset=["DelistedDate"])
        dd["code"] = _code(dd["Stkcd"])
        delist_map = {str(r.code): pd.Timestamp(r.DelistedDate) for r in dd.itertuples(index=False)}

    delisted_proxy = 0
    delisted_real = 0
    delisted_conflict = 0
    last_in_window = panel.groupby("code")["trade_date"].max()
    for code in universe:
        if code in delist_map:
            listed_day = listed_map.get(code)
            if listed_day is not None and delist_map[code] <= pd.Timestamp(listed_day):
                # stock_basic 只保留最近一次上市日；早期生命周期的退市日会早于它
                delisted_conflict += 1
                continue
            events.append(
                {
                    "code": code,
                    "event": "delisted",
                    "effective_date": delist_map[code].strftime("%Y-%m-%d"),
                    "payload": {"proxy": False, "source": "DERIVED_delist_dates"},
                }
            )
            delisted_real += 1
            continue
        if state_map.get(code) != 2:
            continue
        last_day = pd.Timestamp(last_in_window[code])
        if last_day < end.normalize():
            events.append(
                {
                    "code": code,
                    "event": "delisted",
                    "effective_date": (last_day + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    "payload": {"proxy": True, "source": "last_trade+1d"},
                }
            )
            delisted_proxy += 1

    # ---------------------------------------------------------------- ST：年度派生史 + 近期简称
    st_hist_path = data_root / "ref" / "csmar" / "DERIVED_st_history.parquet"
    annual_cutoff: pd.Timestamp | None = None
    st_annual_events = 0
    if st_hist_path.exists():
        hist = pd.read_parquet(st_hist_path)
        hist["code"] = _code(hist["Stkcd"])
        hist["EndDate"] = pd.to_datetime(hist["EndDate"])
        hist = hist.loc[hist["code"].isin(set(universe))].sort_values(["code", "EndDate"])
        if not hist.empty:
            annual_cutoff = hist["EndDate"].max()
        for code, grp in hist.groupby("code", sort=True):
            prev_state: bool | None = None
            listed_day = listed_map.get(code)
            for row in grp.itertuples(index=False):
                cur = bool(row.is_st)
                effective = pd.Timestamp(row.EndDate) + pd.Timedelta(days=1)
                if listed_day is not None and effective <= pd.Timestamp(listed_day):
                    prev_state = cur
                    continue
                if prev_state is None or cur != prev_state:
                    events.append(
                        {
                            "code": code,
                            "event": "st_on" if cur else "st_off",
                            "effective_date": effective.strftime("%Y-%m-%d"),
                            "payload": {"proxy": True, "source": "annual_st_history"},
                        }
                    )
                    st_annual_events += 1
                    prev_state = cur

    # ---------------------------------------------------------------- ST proxy（简称）
    st_table = pq.read_table(
        csmar / "行情" / "TRD_StockTrend" / "data.parquet",
        columns=["Symbol", "ShortName", "TradingDate"],
        filters=[("TradingDate", ">=", (start - pd.Timedelta(days=args.st_lookback_days)).date().isoformat())],
    ).to_pandas()
    st_table["code"] = _code(st_table["Symbol"])
    st_table = st_table.loc[st_table["code"].isin(set(universe))].copy()
    st_table["is_st_name"] = st_table["ShortName"].astype(str).str.contains(ST_RE)
    st_table["TradingDate"] = pd.to_datetime(st_table["TradingDate"])
    st_table = st_table.sort_values(["code", "TradingDate"])
    # 年度派生史已覆盖到 annual_cutoff；简称只用于其后的近期区间，避免同日双源打架
    if annual_cutoff is not None:
        st_table = st_table.loc[st_table["TradingDate"] > annual_cutoff].copy()

    st_events = 0
    st_codes = set()
    for code, grp in st_table.groupby("code", sort=True):
        prev: bool | None = None
        for row in grp.itertuples(index=False):
            cur = bool(row.is_st_name)
            if prev is None or cur != prev:
                events.append(
                    {
                        "code": code,
                        "event": "st_on" if cur else "st_off",
                        "effective_date": pd.Timestamp(row.TradingDate).strftime("%Y-%m-%d"),
                        "payload": {"proxy": True, "source": "short_name"},
                    }
                )
                st_events += 1
                if cur:
                    st_codes.add(code)
                prev = cur

    security = {
        "board_prefixes": {"main": ["60", "00"], "star": ["68"], "chinext": ["30"], "bse": ["92"]},
        "defaults": {"lot_size": 100, "tick_size": 0.01},
        "events": events,
        "dataset": "csmar TRD_Dalyr/TRD_StockTrend/stock_basic; listed/delisted/st=proxy",
    }
    (out / "security.json").write_text(json.dumps(security, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 复权因子
    af = pd.read_parquet(csmar / "行情" / "TRD_AdjustFactor" / "data.parquet")
    af["code"] = _code(af["Symbol"])
    af["trade_date"] = pd.to_datetime(af["TradingDate"])
    af = af.loc[af["code"].isin(set(universe)) & af["CumulateBwardFactor"].notna()].copy()
    factors = af.sort_values(["code", "trade_date"])[["code", "trade_date", "CumulateBwardFactor"]].rename(
        columns={"CumulateBwardFactor": "factor"}
    )
    # 没有公司行动的代码：锚定 1.0（无事件 → 因子恒定，不会造成跳变）
    missing_factor = sorted(set(universe) - set(factors["code"]))
    if missing_factor:
        factors = pd.concat(
            [
                factors,
                pd.DataFrame(
                    {"code": missing_factor, "trade_date": pd.Timestamp("2000-01-01"), "factor": 1.0}
                ),
            ],
            ignore_index=True,
        )
    # 首个因子日晚于首个交易日的代码：用首个因子值向前补齐，避免早期 NaN / 跳变
    first_panel = panel.groupby("code")["trade_date"].min()
    first_factor = factors.groupby("code").agg(trade_date=("trade_date", "min"), factor=("factor", "first"))
    backfill = []
    for code, row in first_factor.iterrows():
        if code in first_panel.index and row["trade_date"] > first_panel[code]:
            backfill.append(
                {"code": code, "trade_date": first_panel[code] - pd.Timedelta(days=1), "factor": row["factor"]}
            )
    if backfill:
        factors = pd.concat([factors, pd.DataFrame(backfill)], ignore_index=True)
    factors = factors.sort_values(["code", "trade_date"]).drop_duplicates(["code", "trade_date"], keep="last")
    factors.to_parquet(out / "factors.parquet", index=False)

    # ---------------------------------------------------------------- 行业 as-of
    ind = pd.read_parquet(data_root / "ref" / "industry" / "industry_pit_sw.parquet")
    ind = ind.loc[ind["effective_to"] > ind["effective_from"]].copy()
    versions = [
        {"version": str(v), "effective_from": g["effective_from"].min().strftime("%Y-%m-%d"), "effective_to": None}
        for v, g in ind.groupby("classification_version")
    ]
    ind_events = [
        {
            "code": str(r.code),
            "industry_code": str(r.sw_l1_code),
            "industry_name": str(r.sw_l1_name) if pd.notna(r.sw_l1_name) else f"SW{r.sw_l1_code}",
            "level": 1,
            "version": str(r.classification_version),
            "effective_from": pd.Timestamp(r.effective_from).strftime("%Y-%m-%d"),
            "effective_to": pd.Timestamp(r.effective_to).strftime("%Y-%m-%d"),
        }
        for r in ind.itertuples(index=False)
    ]
    (out / "industry.json").write_text(
        json.dumps({"versions": versions, "events": ind_events, "dataset": "local-industry_pit_sw"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ---------------------------------------------------------------- 无涨跌幅限制日 / 停复牌
    nl = pd.read_parquet(csmar / "行情" / "TRD_NoLimit" / "data.parquet")
    nl["code"] = _code(nl["Symbol"])
    nl["trade_date"] = pd.to_datetime(nl["TradingDate"])
    nl = nl.loc[nl["trade_date"].between(start, end) & nl["code"].isin(set(universe))]
    nl = nl[["code", "trade_date"]].drop_duplicates().sort_values(["trade_date", "code"])
    nl.to_parquet(out / "no_limit.parquet", index=False)

    ts = pd.read_parquet(csmar / "行情" / "TSR_Stkstat" / "data.parquet")
    ts["code"] = _code(ts["Stkcd"])
    ts["start_date"] = pd.to_datetime(ts["Suspdate"], errors="coerce")
    ts["end_date"] = pd.to_datetime(ts["Resmdate"], errors="coerce")
    ts = ts.loc[(ts["Type"] == 1) & ts["start_date"].notna()].copy()
    ts["end_date"] = ts["end_date"].fillna(end + pd.Timedelta(days=1))
    ts = ts.loc[
        (ts["start_date"] <= end) & (ts["end_date"] >= start) & ts["code"].isin(set(universe))
    ]
    suspensions = ts[["code", "start_date", "end_date"]].drop_duplicates().sort_values(["start_date", "code"])
    suspensions.to_parquet(out / "suspensions.parquet", index=False)

    st_summary = {
        "universe": len(universe),
        "bse_codes": int(sum(1 for c in universe if c.startswith("92"))),
        "panel_rows": int(len(panel)),
        "listing_proxy": listing_proxy,
        "delisted_real": delisted_real,
        "delisted_proxy": delisted_proxy,
        "st_annual_events": st_annual_events,
        "st_events_proxy": st_events,
        "st_codes_proxy": len(st_codes),
        "factor_rows": int(len(factors)),
        "no_limit_rows": int(len(nl)),
        "suspension_intervals": int(len(suspensions)),
    }
    (out / "st_summary.json").write_text(json.dumps(st_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(st_summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
