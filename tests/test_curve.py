from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from qrg.api import SpecError
from qrg.calendar.regime import RegimeCalendar
from qrg.capacity.curve import capacity_curve
from qrg.cost.impact import SqrtImpact

D = date(2024, 1, 2)
WEIGHTS = {"600001": 0.6, "600002": 0.4}
ADV = {"600001": 1e8, "600002": 5e7}


def run(**kwargs):
    return capacity_curve(
        kwargs.pop("weights", WEIGHTS),
        kwargs.pop("adv", ADV),
        kwargs.pop("impact", SqrtImpact(coef_bps=10, exponent=0.5)),
        RegimeCalendar(),
        D,
        gross_alpha_bps=kwargs.pop("gross_alpha_bps", 100.0),
        **kwargs,
    )


def test_curve_shape_and_monotonicity() -> None:
    curve, summary = run()
    assert list(curve.columns) == ["aum", "participation", "impact_bps", "fee_bps", "net_bps", "feasible"]
    assert curve["net_bps"].is_monotonic_decreasing
    assert curve["impact_bps"].is_monotonic_increasing
    assert summary["uncalibrated"] is True
    assert summary["capacity_aum"] is not None


def test_capacity_is_zero_crossing() -> None:
    curve, summary = run()
    feasible = curve.loc[curve["feasible"], "aum"]
    infeasible = curve.loc[~curve["feasible"], "aum"]
    assert summary["capacity_aum"] == pytest.approx(float(feasible.max()))
    if not infeasible.empty:
        assert summary["capacity_aum"] < float(infeasible.min())


def test_smaller_adv_reduces_capacity() -> None:
    _, big = run()
    _, small = run(adv={"600001": 1e7, "600002": 5e6})
    assert small["capacity_aum"] < big["capacity_aum"]


def test_weights_are_normalised() -> None:
    curve, _ = run(weights={"600001": 6.0, "600002": 4.0})
    assert curve["participation"].iloc[0] > 0


def test_missing_adv_rejected() -> None:
    with pytest.raises(SpecError):
        run(adv={"600001": 1e8})


def test_bad_parameters_rejected() -> None:
    with pytest.raises(SpecError):
        run(weights={})
    with pytest.raises(SpecError):
        run(weights={"600001": -1.0})
    with pytest.raises(SpecError):
        run(turnover=0.0)
    with pytest.raises(SpecError):
        run(gross_alpha_bps=0.0)


def test_high_alpha_has_no_capacity_limit_in_grid() -> None:
    curve, summary = run(gross_alpha_bps=10_000.0, aum_grid=np.logspace(6, 8, 5))
    assert curve["feasible"].all()
    assert summary["capacity_aum"] == pytest.approx(float(curve["aum"].max()))
