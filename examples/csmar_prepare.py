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


def _read_ref_or_raw(data_root: Path, csmar: Path, raw_parts: tuple[str, ...], name: str, **kwargs):
    """优先读 data/ref 的整合版（含更早历史），否则回退 external 原始表。"""
    ref = data_root / "ref" / "csmar" / f"{name}.parquet"
    if ref.exists():
        return pd.read_parquet(ref, **kwargs)
    return pq.read_table(csmar.joinpath(*raw_parts) / "data.parquet", **kwargs).to_pandas()


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
    bars = _read_ref_or_raw(
        data_root,
        csmar,
        ("行情", "TRD_Dalyr"),
        "TRD_Dalyr",
        columns=COLUMNS,
        filters=[("Trddt", ">=", start.date().isoformat()), ("Trddt", "<=", end.date().isoformat())],
    )
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

    # ---------------------------------------------------------------- ST：日频简称全历史（proxy）
    from qrg.master.derive import st_events_from_names

    trend_path = data_root / "ref" / "csmar" / "TRD_StockTrend.parquet"
    st_events_daily = 0
    st_annual_events = 0
    st_codes: set[str] = set()
    st_vintage: str | None = None
    if trend_path.exists():
        trend = _read_ref_or_raw(
            data_root, csmar, ("行情", "TRD_StockTrend"), "TRD_StockTrend",
            columns=["Symbol", "ShortName", "TradingDate"],
        )
        trend["code"] = _code(trend["Symbol"])
        trend = trend.loc[trend["code"].isin(set(universe))].rename(
            columns={"ShortName": "name", "TradingDate": "trade_date"}
        )
        trend = trend.loc[pd.to_datetime(trend["trade_date"]) <= end]
        if not trend.empty and pd.to_datetime(trend["trade_date"]).notna().any():
            st_vintage = pd.to_datetime(trend["trade_date"]).max().strftime("%Y-%m-%d")
            st_events = st_events_from_names(trend, vintage_date=st_vintage)
            events.extend(st_events)
            st_events_daily = len(st_events)
            st_codes = {e["code"] for e in st_events if e["event"] == "st_on"}
    if st_events_daily == 0:
        # 兜底：年度派生史（2019–2025）；窗口早于 2016 时两者都没有，ST 记 unknown
        st_hist_path = data_root / "ref" / "csmar" / "DERIVED_st_history.parquet"
        if st_hist_path.exists():
            hist = pd.read_parquet(st_hist_path)
            hist["code"] = _code(hist["Stkcd"])
            hist["EndDate"] = pd.to_datetime(hist["EndDate"])
            hist = hist.loc[hist["code"].isin(set(universe))].sort_values(["code", "EndDate"])
            for code, grp in hist.groupby("code", sort=True):
                prev_state: bool | None = None
                for row in grp.itertuples(index=False):
                    cur = bool(row.is_st)
                    effective = pd.Timestamp(row.EndDate) + pd.Timedelta(days=1)
                    if effective > end:
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

    security = {
        "board_prefixes": {"main": ["60", "00"], "star": ["68"], "chinext": ["30"], "bse": ["92"]},
        "defaults": {"lot_size": 100, "tick_size": 0.01},
        "events": events,
        "dataset": "csmar TRD_Dalyr/TRD_StockTrend/stock_basic; listed/delisted/st=daily short-name proxy",
        "st_vintage": st_vintage,
    }
    (out / "security.json").write_text(json.dumps(security, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 复权因子
    af = _read_ref_or_raw(data_root, csmar, ("行情", "TRD_AdjustFactor"), "TRD_AdjustFactor")
    af["code"] = _code(af["Symbol"])
    af["trade_date"] = pd.to_datetime(af["TradingDate"])
    af = af.loc[af["code"].isin(set(universe)) & af["CumulateBwardFactor"].notna()].copy()
    factors = af.sort_values(["code", "trade_date"])[["code", "trade_date", "CumulateBwardFactor"]].rename(
        columns={"CumulateBwardFactor": "factor"}
    )
    # 长历史里 5 只股票的后复权因子会出现下降（CSMAR 原始数据异常）；不修复、直接剔除，交给 base_proxy
    bad_factor_codes = sorted(
        str(code)
        for code, grp in factors.groupby("code", sort=True)
        if (grp["factor"].diff() < -1e-12).any()
    )
    if bad_factor_codes:
        factors = factors.loc[~factors["code"].isin(bad_factor_codes)].reset_index(drop=True)
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
    nl = _read_ref_or_raw(data_root, csmar, ("行情", "TRD_NoLimit"), "TRD_NoLimit")
    nl["code"] = _code(nl["Symbol"])
    nl["trade_date"] = pd.to_datetime(nl["TradingDate"])
    nl = nl.loc[nl["trade_date"].between(start, end) & nl["code"].isin(set(universe))]
    nl = nl[["code", "trade_date"]].drop_duplicates().sort_values(["trade_date", "code"])
    nl.to_parquet(out / "no_limit.parquet", index=False)

    ts = _read_ref_or_raw(data_root, csmar, ("行情", "TSR_Stkstat"), "TSR_Stkstat")
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

    # ---------------------------------------------------------------- 指数成分权重（IDX_Smprat）
    idx_path = data_root / "ref" / "csmar" / "IDX_Smprat.parquet"
    index_rows = 0
    index_codes = 0
    if idx_path.exists():
        iw = pd.read_parquet(idx_path)
        iw["index_code"] = iw["Indexcd"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        iw["trade_date"] = pd.to_datetime(iw["Enddt"], errors="coerce")
        iw["code"] = _code(iw["Stkcd"])
        iw = iw.loc[
            iw["trade_date"].notna()
            & (iw["trade_date"] >= start - pd.Timedelta(days=400))
            & (iw["trade_date"] <= end)
        ]
        iw = iw.rename(columns={"Weight": "weight"})[["index_code", "trade_date", "code", "weight"]]
        iw = iw.drop_duplicates(["index_code", "trade_date", "code"], keep="last").sort_values(
            ["index_code", "trade_date", "code"]
        )
        iw.to_parquet(out / "index_weights.parquet", index=False)
        index_rows = int(len(iw))
        index_codes = int(iw["index_code"].nunique())

    # ---------------------------------------------------------------- 财务 PIT / 市值 / vintage 清单
    fin_path = data_root / "ref" / "financial" / "financial_pit.parquet"
    financial_rows = 0
    fin_min = fin_max = None
    if fin_path.exists():
        fin = pd.read_parquet(fin_path)
        fin["code"] = _code(fin["SECURITY_CODE"])
        fin["notice_date"] = pd.to_datetime(fin["NOTICE_DATE"], errors="coerce")
        fin = fin.loc[fin["code"].isin(set(universe)) & fin["notice_date"].notna()].copy()
        mapping = {
            "TOTAL_OPERATE_INCOME": "revenue",
            "PARENT_NETPROFIT": "net_profit",
            "WEIGHTAVG_ROE": "roe",
            "BPS": "bps",
            "BASIC_EPS": "eps",
            "YSTZ": "revenue_yoy",
            "SJLTZ": "profit_yoy",
        }
        keep = [c for c in mapping if c in fin.columns]
        long = fin.melt(id_vars=["code", "notice_date"], value_vars=keep, var_name="item", value_name="value")
        long["item"] = long["item"].map(mapping)
        long = long.dropna(subset=["value"]).drop_duplicates(["code", "notice_date", "item"], keep="last")
        long = long.loc[:, ["code", "notice_date", "item", "value"]].sort_values(["code", "notice_date", "item"])
        long.to_parquet(out / "financial.parquet", index=False)
        financial_rows = int(len(long))
        fin_min = long["notice_date"].min()
        fin_max = long["notice_date"].max()

    mc_path = data_root / "ref" / "market_cap" / "market_cap.parquet"
    mc_rows = 0
    mc_min = mc_max = None
    if mc_path.exists():
        mc = pd.read_parquet(mc_path)
        mc["code"] = _code(mc["code"])
        mc["trade_date"] = pd.to_datetime(mc["date"], errors="coerce")
        mc_full_min, mc_full_max = mc["trade_date"].min(), mc["trade_date"].max()
        mc = mc.loc[
            mc["code"].isin(set(universe))
            & mc["trade_date"].between(start, end)
            & mc["trade_date"].notna()
        ]
        cols = [c for c in ("code", "trade_date", "total_mv", "circ_mv", "pe_ttm", "pb") if c in mc.columns]
        mc = mc.loc[:, cols].sort_values(["trade_date", "code"])
        mc.to_parquet(out / "market_cap.parquet", index=False)
        mc_rows = int(len(mc))
        mc_min, mc_max = mc_full_min, mc_full_max

    def _fmt(value):
        if value is None:
            return None
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")

    vintages = {
        "panel": {"source": "csmar TRD_Dalyr", "rows": int(len(panel)), "min": _fmt(panel["trade_date"].min()), "max": _fmt(panel["trade_date"].max())},
        "adjust_factor": {"source": "csmar TRD_AdjustFactor", "rows": int(len(af)), "min": _fmt(af["trade_date"].min()), "max": _fmt(af["trade_date"].max())},
        "stock_basic": {"snapshot_date": pd.to_datetime(sb["snapshot_date"], errors="coerce").max().strftime("%Y-%m-%d") if "snapshot_date" in sb.columns and sb["snapshot_date"].notna().any() else None, "rows": int(len(sb))},
        "st": {"source": "TRD_StockTrend daily short name (proxy)", "events": st_events_daily, "annual_fallback_events": st_annual_events, "vintage": st_vintage},
        "delist": {"source": "DERIVED_delist_dates", "rows": len(delist_map)},
        "industry": {"source": "industry_pit_sw", "rows": int(len(ind))},
        "financial": {"source": "financial_pit", "rows": financial_rows, "min": _fmt(fin_min), "max": _fmt(fin_max)},
        "market_cap": {"source": "market_cap", "rows": mc_rows, "min": _fmt(mc_min), "max": _fmt(mc_max)},
        "index_weights": {"source": "IDX_Smprat", "rows": index_rows, "indices": index_codes},
        "no_limit": {"source": "TRD_NoLimit", "rows": int(len(nl))},
        "suspensions": {"source": "TSR_Stkstat", "intervals": int(len(suspensions))},
    }
    (out / "vintages.json").write_text(json.dumps(vintages, ensure_ascii=False, indent=2), encoding="utf-8")

    st_summary = {
        "universe": len(universe),
        "bse_codes": int(sum(1 for c in universe if c.startswith("92"))),
        "panel_rows": int(len(panel)),
        "listing_proxy": listing_proxy,
        "delisted_real": delisted_real,
        "delisted_proxy": delisted_proxy,
        "st_events_daily": st_events_daily,
        "st_annual_events": st_annual_events,
        "st_codes_proxy": len(st_codes),
        "financial_rows": financial_rows,
        "market_cap_rows": mc_rows,
        "factor_rows": int(len(factors)),
        "factor_dropped_nonmonotonic": len(bad_factor_codes),
        "index_rows": index_rows,
        "index_codes": index_codes,
        "no_limit_rows": int(len(nl)),
        "suspension_intervals": int(len(suspensions)),
    }
    (out / "st_summary.json").write_text(json.dumps(st_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(st_summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
