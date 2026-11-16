"""市值合并（可选输入）：把 total_mv/circ_mv 等按 (trade_date, code) 左连接到面板。

口径：
- 只做左连接，缺失保留 NaN，不前向填充；
- 重复键取最后一条；
- 不改变面板行数。
"""
from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from ..api import SpecError

DEFAULT_COLUMNS = ("total_mv", "circ_mv", "pe_ttm", "pb")


def merge_market_cap(
    panel: pd.DataFrame,
    market_cap: pd.DataFrame,
    columns: Sequence[str] = DEFAULT_COLUMNS,
) -> pd.DataFrame:
    if panel.empty:
        return panel.copy()
    for frame, name in ((panel, "panel"), (market_cap, "market_cap")):
        for col in ("trade_date", "code"):
            if col not in frame.columns:
                raise SpecError(f"{name} missing columns: {col}")

    mv = market_cap.copy()
    mv["code"] = mv["code"].astype("string").str.zfill(6)
    mv["trade_date"] = pd.to_datetime(mv["trade_date"]).astype("datetime64[ns]")
    keep = ["trade_date", "code"] + [c for c in columns if c in mv.columns]
    if len(keep) <= 2:
        raise SpecError("market_cap has none of the expected value columns")
    mv = mv.loc[:, keep].drop_duplicates(["trade_date", "code"], keep="last")

    out = panel.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).astype("datetime64[ns]")
    out["code"] = out["code"].astype("string").str.zfill(6)
    merged = out.merge(mv, on=["trade_date", "code"], how="left", suffixes=("", "_mv"))
    return merged.sort_values(["trade_date", "code"]).reset_index(drop=True)
