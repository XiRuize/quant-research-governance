from __future__ import annotations

import pandas as pd

from qrg.master.index_members import IndexConstituents


def _weights() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Indexcd": ["906", "906", "000906", "000906", "906"],
            "Enddt": ["2024-01-02", "2024-01-02", "2024-06-03", "2024-06-03", "2024-01-02"],
            "Stkcd": [1, 2, 1, 3, 2],
            "Weight": [60.0, 40.0, 55.0, 45.0, 40.0],
        }
    )


def test_normalizes_codes_and_dates() -> None:
    cal = IndexConstituents(_weights(), index_col="Indexcd", date_col="Enddt", code_col="Stkcd", weight_col="Weight")
    assert "000906" in cal.indices
    assert cal.members("000906", "2024-01-02") == {"000001", "000002"}


def test_as_of_does_not_look_ahead() -> None:
    cal = IndexConstituents(_weights(), index_col="Indexcd", date_col="Enddt", code_col="Stkcd", weight_col="Weight")
    before = cal.as_of("000906", "2024-05-31")
    assert list(before["code"]) == ["000001", "000002"]
    after = cal.as_of("000906", "2024-06-03")
    assert list(after["code"]) == ["000001", "000003"]
    assert cal.weight_of("000906", "2024-06-03", "1") == 55.0


def test_unknown_index_is_empty() -> None:
    cal = IndexConstituents(_weights(), index_col="Indexcd", date_col="Enddt", code_col="Stkcd", weight_col="Weight")
    assert cal.as_of("000300", "2024-06-03").empty
    assert cal.members("000300", "2024-06-03") == set()


def test_empty_frame() -> None:
    cal = IndexConstituents(pd.DataFrame(columns=["index_code", "trade_date", "code", "weight"]))
    assert cal.indices == []
    assert cal.as_of("000906", "2024-01-02").empty
