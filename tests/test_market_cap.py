from __future__ import annotations

import pandas as pd
import pytest

from qrg.api import SpecError
from qrg.clean.market import merge_market_cap


def _panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-02", "2024-01-03"]),
            "code": ["000001", "000002", "000001"],
            "close": [10.0, 20.0, 10.5],
        }
    )


def test_merge_adds_values_and_keeps_rows() -> None:
    mv = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02"]),
            "code": ["000001", "000001", "000001"],
            "total_mv": [1e11, 1.1e11, 9e10],
            "circ_mv": [9e10, 9.9e10, 8e10],
        }
    )
    out = merge_market_cap(_panel(), mv)
    assert len(out) == 3
    first = out.loc[(out["code"] == "000001") & (out["trade_date"] == "2024-01-02"), "total_mv"]
    assert first.iloc[0] == 9e10  # 重复键取最后一条
    missing = out.loc[out["code"] == "000002", "total_mv"]
    assert missing.isna().all()


def test_merge_normalizes_dtypes_and_codes() -> None:
    mv = pd.DataFrame({"trade_date": ["2024-01-02"], "code": [1], "total_mv": [1.0]})
    out = merge_market_cap(_panel(), mv)
    assert str(out["trade_date"].dtype) == "datetime64[ns]"
    assert out.loc[out["code"] == "000001", "total_mv"].notna().any()


def test_merge_rejects_missing_columns() -> None:
    with pytest.raises(SpecError):
        merge_market_cap(_panel(), pd.DataFrame({"code": ["000001"], "total_mv": [1.0]}))
    with pytest.raises(SpecError):
        merge_market_cap(_panel(), pd.DataFrame({"trade_date": ["2024-01-02"], "code": ["000001"]}))
