from __future__ import annotations

from pathlib import Path

import pytest

from qrg.api import Side, SpecError
from qrg.cost.impact import SqrtImpact, load_impact, participation

ROOT = Path(__file__).resolve().parents[1]


def test_zero_participation_zero_impact() -> None:
    assert SqrtImpact().bps(0.0) == 0.0


def test_monotonic_in_participation() -> None:
    m = SqrtImpact(coef_bps=10, exponent=0.5)
    assert m.bps(0.05) < m.bps(0.2) < m.bps(0.5)


def test_sqrt_scaling() -> None:
    m = SqrtImpact(coef_bps=10, exponent=0.5)
    assert m.bps(0.04) == pytest.approx(2.0)
    assert m.bps(0.16) == pytest.approx(4.0)


def test_price_direction() -> None:
    m = SqrtImpact(coef_bps=100, exponent=0.5)
    assert m.price(10.0, Side.BUY, 1.0) == pytest.approx(10.1)
    assert m.price(10.0, Side.SELL, 1.0) == pytest.approx(9.9)


def test_participation_is_capped() -> None:
    assert SqrtImpact().bps(5.0) == pytest.approx(SqrtImpact().bps(1.0))


def test_invalid_parameters() -> None:
    with pytest.raises(SpecError):
        SqrtImpact(coef_bps=-1)
    with pytest.raises(SpecError):
        SqrtImpact(exponent=0)


def test_load_default_rules() -> None:
    m = load_impact(ROOT / "rules" / "impact.json")
    assert m.coef_bps == 10.0
    assert m.exponent == 0.5


def test_participation_helper() -> None:
    assert participation(100, 1000) == pytest.approx(0.1)
    assert participation(100, 0) == 0.0
