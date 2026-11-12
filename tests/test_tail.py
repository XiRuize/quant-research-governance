from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qrg.api import AuditReport, Severity, SpecError
from qrg.cost.impact import SqrtImpact
from qrg.tail import Scenario, apply_scenario, bootstrap_bands, run_scenarios

IMPACT = SqrtImpact(coef_bps=10.0, exponent=0.5)


def _base() -> pd.DataFrame:
    """容量曲线：默认情景容量 300，vol*2 后交叉提前到 200。"""
    return pd.DataFrame(
        {
            "aum": [100.0, 200.0, 300.0],
            "participation": [0.01, 0.01, 0.01],
            "impact_bps": [1.0, 1.0, 1.0],
            "fee_bps": [0.0, 0.0, 0.0],
            "net_bps": [10.0, 0.5, -5.0],
        }
    )


def test_adv_shrink_doubles_participation_and_raises_impact() -> None:
    base = _base()
    out = apply_scenario(base, Scenario("adv_half", adv_multiplier=0.5), IMPACT)

    assert out["participation"].tolist() == pytest.approx([0.02, 0.02, 0.02])
    expected = IMPACT.bps(0.02, 1.0)
    assert out["impact_bps"].tolist() == pytest.approx([expected, expected, expected])
    assert (out["impact_bps"] > base["impact_bps"]).all()
    # 原表不被就地修改
    assert base["participation"].tolist() == pytest.approx([0.01, 0.01, 0.01])
    assert "scenario" not in base.columns


def test_scenario_column_is_name() -> None:
    out = apply_scenario(_base(), Scenario("base_case"), IMPACT)
    assert out["scenario"].tolist() == ["base_case"] * 3


def test_vol_multiplier_doubles_impact() -> None:
    base = _base()
    out = apply_scenario(base, Scenario("vol2", vol_multiplier=2.0), IMPACT)
    assert out["impact_bps"].tolist() == pytest.approx(
        (base["impact_bps"] * 2.0).tolist()
    )
    assert out["impact_bps"].tolist() == pytest.approx(
        [IMPACT.bps(p, 2.0) for p in base["participation"]]
    )


def test_locked_ratio_scales_participation_and_net() -> None:
    base = _base()
    out = apply_scenario(base, Scenario("locked_half", locked_ratio=0.5), IMPACT)

    assert out["participation"].tolist() == pytest.approx([0.005, 0.005, 0.005])
    gross = base["net_bps"] + base["impact_bps"] + base["fee_bps"]
    expected_net = gross * 0.5 - out["impact_bps"] - base["fee_bps"]
    assert out["net_bps"].tolist() == pytest.approx(expected_net.tolist())

    all_locked = apply_scenario(base, Scenario("locked_all", locked_ratio=1.0), IMPACT)
    assert all_locked["participation"].tolist() == pytest.approx([0.0, 0.0, 0.0])
    assert all_locked["impact_bps"].tolist() == pytest.approx([0.0, 0.0, 0.0])


def test_same_scenario_runs_are_identical() -> None:
    base = _base()
    scenario = Scenario("det", vol_multiplier=1.7, adv_multiplier=0.7, locked_ratio=0.2, seed=11)

    first, report_first = run_scenarios(base, [scenario, scenario], IMPACT)
    second, report_second = run_scenarios(base, [scenario, scenario], IMPACT)

    pd.testing.assert_frame_equal(first, second)
    assert first.to_csv(index=False) == second.to_csv(index=False)
    assert report_first.meta == report_second.meta
    assert [h.to_dict() for h in report_first.hits] == [h.to_dict() for h in report_second.hits]


def test_capacity_is_first_non_positive_net_aum() -> None:
    frame, report = run_scenarios(_base(), [Scenario("base_case")], IMPACT)
    assert frame["net_bps"].tolist() == pytest.approx([10.0, 0.5, -5.0])
    assert report.stage == "tail"
    assert report.meta["scenarios"][0]["name"] == "base_case"
    assert report.meta["scenarios"][0]["capacity"] == pytest.approx(300.0)

    frame_vol2, report_vol2 = run_scenarios(_base(), [Scenario("vol2", vol_multiplier=2.0)], IMPACT)
    assert frame_vol2["net_bps"].tolist() == pytest.approx([9.0, -0.5, -6.0])
    assert report_vol2.meta["scenarios"][0]["capacity"] == pytest.approx(200.0)


def test_capacity_none_when_never_crosses() -> None:
    base = _base().iloc[[0]].copy()
    _, report = run_scenarios(base, [Scenario("safe")], IMPACT)
    assert report.meta["scenarios"][0]["capacity"] is None


def test_invalid_base_and_parameters_raise_spec_error() -> None:
    missing = _base().drop(columns=["net_bps"])
    with pytest.raises(SpecError):
        apply_scenario(missing, Scenario("x"), IMPACT)
    with pytest.raises(SpecError):
        run_scenarios(missing, [Scenario("x")], IMPACT)

    for kwargs in (
        {"vol_multiplier": 0.0},
        {"vol_multiplier": -1.0},
        {"adv_multiplier": 0.0},
        {"adv_multiplier": -0.5},
        {"locked_ratio": -0.01},
        {"locked_ratio": 1.01},
    ):
        with pytest.raises(SpecError):
            Scenario("bad", **kwargs)

    with pytest.raises(SpecError):
        run_scenarios(_base(), [Scenario("bad", adv_multiplier=-1.0)], IMPACT)


def test_stress_assumption_warns() -> None:
    _, quiet = run_scenarios(
        _base(),
        [Scenario("at_threshold", adv_multiplier=0.5, locked_ratio=0.3)],
        IMPACT,
    )
    assert not [h for h in quiet.hits if h.rule_id == "TAIL.stress_assumption"]

    frame, stressed = run_scenarios(
        _base(),
        [Scenario("deep_adv", adv_multiplier=0.49), Scenario("locked", locked_ratio=0.31)],
        IMPACT,
    )
    hits = [h for h in stressed.hits if h.rule_id == "TAIL.stress_assumption"]
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARN
    assert hits[0].scope["scenarios"] == ["deep_adv", "locked"]
    assert set(frame["scenario"].unique()) == {"deep_adv", "locked"}
    assert [e["capacity"] for e in stressed.meta["scenarios"]] == [300.0, 300.0]


def test_empty_scenarios_returns_valid_report() -> None:
    frame, report = run_scenarios(_base(), [], IMPACT)
    assert frame.empty
    assert "scenario" in frame.columns
    assert report.meta["scenarios"] == []
    assert isinstance(report, AuditReport)


def test_bootstrap_bands_reproducible_and_ordered() -> None:
    returns = pd.Series(np.random.default_rng(7).normal(0.0, 0.01, 500))
    first = bootstrap_bands(returns, n=200, block=5, seed=42)
    second = bootstrap_bands(returns, n=200, block=5, seed=42)

    assert first == second
    assert set(first) == {"p05", "p01", "cvar05", "n"}
    assert first["n"] == 200
    assert first["p01"] <= first["p05"]
    assert first["cvar05"] <= first["p05"]


def test_bootstrap_bands_different_seed_changes_draw() -> None:
    returns = pd.Series(np.random.default_rng(11).normal(0.0, 0.01, 300))
    a = bootstrap_bands(returns, seed=1)
    b = bootstrap_bands(returns, seed=2)
    assert a["p05"] != b["p05"] or a["cvar05"] != b["cvar05"]


def test_bootstrap_bands_invalid_inputs() -> None:
    returns = pd.Series([0.01, -0.02, 0.03])
    with pytest.raises(SpecError):
        bootstrap_bands(pd.Series([], dtype="float64"))
    with pytest.raises(SpecError):
        bootstrap_bands(returns, block=10)
    with pytest.raises(SpecError):
        bootstrap_bands(returns, n=0)
    with pytest.raises(SpecError):
        bootstrap_bands(returns, alpha=1.0)
