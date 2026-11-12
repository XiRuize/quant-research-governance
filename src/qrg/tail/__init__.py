"""qrg 尾部模拟子包（D3）。

对外暴露情景模拟与块 bootstrap 估计；所有结论都是**假设驱动**的，
参数未经校准，不构成收益/风险预测。
"""
from .extremes import bootstrap_bands
from .scenarios import Scenario, apply_scenario, run_scenarios

__all__ = ["Scenario", "apply_scenario", "run_scenarios", "bootstrap_bands"]
