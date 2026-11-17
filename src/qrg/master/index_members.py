"""指数成分与权重（as-of 查询）。

数据来源：CSMAR ``IDX_Smprat``（每日/定期成分权重快照）。口径：
- 只用查询日之前（含当日）最近一份快照，不向前看；
- 代码统一补足 6 位，权重按百分数保留原值；
- 查不到的指数返回空集合，不退回全市场。
"""
from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

DEFAULT_COLUMNS = ("index_code", "trade_date", "code", "weight")


class IndexConstituents:
    def __init__(
        self,
        weights: pd.DataFrame,
        *,
        index_col: str = "index_code",
        date_col: str = "trade_date",
        code_col: str = "code",
        weight_col: str = "weight",
    ) -> None:
        missing = [c for c in (index_col, date_col, code_col, weight_col) if c not in weights.columns]
        if missing:
            raise ValueError(f"weights missing columns: {missing}")
        frame = weights.loc[:, [index_col, date_col, code_col, weight_col]].copy()
        frame["index_code"] = frame[index_col].astype("string").str.strip().str.zfill(6)
        frame["code"] = frame[code_col].astype("string").str.strip().str.zfill(6)
        frame["trade_date"] = pd.to_datetime(frame[date_col]).astype("datetime64[ns]")
        frame["weight"] = pd.to_numeric(frame[weight_col], errors="coerce")
        frame = frame.dropna(subset=["index_code", "trade_date"]).drop_duplicates(
            ["index_code", "trade_date", "code"], keep="last"
        )
        self._frame = frame.sort_values(["index_code", "trade_date", "code"]).reset_index(drop=True)
        self._dates: dict[str, pd.Series] = {
            str(idx): pd.Series(sorted(grp["trade_date"].unique()))
            for idx, grp in self._frame.groupby("index_code", sort=False)
        }

    @property
    def indices(self) -> list[str]:
        return sorted(self._dates)

    def snapshot_dates(self, index: str) -> list[pd.Timestamp]:
        dates = self._dates.get(str(index))
        return [] if dates is None else list(pd.to_datetime(dates))

    def as_of(self, index: str, day) -> pd.DataFrame:
        dates = self._dates.get(str(index))
        if dates is None or dates.empty:
            return self._frame.iloc[0:0].loc[:, ["code", "weight"]]
        target = pd.Timestamp(day)
        position = int(dates.searchsorted(target, side="right")) - 1
        if position < 0:
            return self._frame.iloc[0:0].loc[:, ["code", "weight"]]
        snapshot = dates.iloc[position]
        part = self._frame
        part = part.loc[(part["index_code"] == str(index)) & (part["trade_date"] == snapshot)]
        return part.loc[:, ["code", "weight"]].reset_index(drop=True)

    def members(self, index: str, day) -> set[str]:
        return set(self.as_of(index, day)["code"])

    def weight_of(self, index: str, day, code: str) -> float | None:
        part = self.as_of(index, day)
        hit = part.loc[part["code"] == str(code).zfill(6), "weight"]
        return None if hit.empty else float(hit.iloc[0])

    @classmethod
    def from_parquet(cls, path, **kwargs) -> "IndexConstituents":
        return cls(pd.read_parquet(path), **kwargs)
