from __future__ import annotations

from datetime import date

import pytest

from qrg.api import Fill, Side, SpecError, SlippageBreakdown
from qrg.exec.slippage import decompose, decompose_many, signed_slippage_bps

D = date(2024, 1, 2)


def fill(side: Side = Side.BUY, price: float = 10.1, qty: float = 100.0, oid: str = "o1") -> Fill:
    return Fill(oid, D, "600001", side, qty, price)


def test_buy_slippage_positive_when_paying_more() -> None:
    assert signed_slippage_bps(fill(Side.BUY, 10.1), 10.0) == pytest.approx(100.0)


def test_sell_slippage_positive_when_receiving_less() -> None:
    assert signed_slippage_bps(fill(Side.SELL, 9.9), 10.0) == pytest.approx(100.0)


def test_components_sum_to_total() -> None:
    part = decompose(fill(), 10.0, prev_close=9.9, delay_price=10.02, impact_bps=10.0, spread_bps=2.0)
    assert part.total == pytest.approx(100.0)
    assert part.impact + part.delay + part.spread + part.overnight + part.residual == pytest.approx(100.0)


def test_zero_qty_has_no_slippage() -> None:
    part = decompose(fill(qty=0.0), 10.0)
    assert part == SlippageBreakdown()


def test_bad_arrival_rejected() -> None:
    with pytest.raises(SpecError):
        signed_slippage_bps(fill(), 0.0)


def test_batch_and_residual_warning() -> None:
    frames, report = decompose_many(
        [fill(), fill(oid="o2", qty=0.0)],
        {"o1": 10.0, "o2": 10.0},
        impact_bps={"o1": 5.0},
        residual_tol_bps=1.0,
    )
    assert len(frames) == 2
    assert frames.loc[frames["order_id"] == "o1", "residual_bps"].iloc[0] == pytest.approx(95.0)
    ids = {h.rule_id for h in report.hits}
    assert "SLIP.unexplained_residual" in ids
    assert "SLIP.no_fill" in ids
