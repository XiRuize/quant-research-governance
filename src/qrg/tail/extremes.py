"""尾部估计（D3）：块 bootstrap 分位数与 CVaR。

与 :mod:`qrg.tail.scenarios` 一样，本模块输出是**未校准的统计占位**
（uncalibrated）：块长、重采样次数与 alpha 都是研究假设，用于暴露样本内
尾部形态，不构成对未来收益/风险的预测。唯一随机源是
``numpy.random.default_rng(seed)``，禁止全局 random / 时间。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..api import SpecError

#: p01 固定按 1% 分位报告；alpha 控制 p05 与 CVaR 的尾部比例。
P01_QUANTILE = 0.01


def _validate(returns: pd.Series, n: int, block: int, alpha: float) -> np.ndarray:
    if not isinstance(returns, pd.Series):
        raise SpecError(f"returns must be a pandas.Series, got {type(returns).__name__}")
    if not isinstance(n, (int, np.integer)) or int(n) <= 0:
        raise SpecError(f"n must be a positive integer, got {n!r}")
    if not isinstance(block, (int, np.integer)) or int(block) <= 0:
        raise SpecError(f"block must be a positive integer, got {block!r}")
    if not 0.0 < float(alpha) < 1.0:
        raise SpecError(f"alpha must be in (0, 1), got {alpha!r}")
    values = returns.to_numpy(dtype="float64", copy=True)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise SpecError("returns has no finite observations")
    if values.size < int(block):
        raise SpecError(
            f"returns has {values.size} finite observations, fewer than block={int(block)}"
        )
    return values


def bootstrap_bands(
    returns: pd.Series,
    *,
    n: int = 200,
    block: int = 5,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, float]:
    """用块 bootstrap 重采样 ``n`` 个观测，返回尾部统计。

    返回键：``p05``（alpha 分位）、``p01``（1% 分位）、``cvar05``
    （<= p05 的样本均值，即尾部平均损失）、``n``（重采样样本数）。

    同 ``seed``、同输入的结果逐值一致。
    """
    values = _validate(returns, n, block, alpha)
    m = int(values.size)
    b = int(block)
    rng = np.random.default_rng(int(seed))
    n_blocks = int(np.ceil(int(n) / b))
    starts = rng.integers(0, m - b + 1, size=n_blocks)
    chunks = [values[s : s + b] for s in starts]
    sample = np.concatenate(chunks)[: int(n)]
    if sample.size == 0:  # pragma: no cover - 由参数校验兜底
        raise SpecError("bootstrap produced empty sample")

    p05 = float(np.quantile(sample, float(alpha)))
    p01 = float(np.quantile(sample, P01_QUANTILE))
    tail = sample[sample <= p05]
    cvar05 = float(tail.mean()) if tail.size else p05
    return {"p05": p05, "p01": p01, "cvar05": cvar05, "n": float(sample.size)}
