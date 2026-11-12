"""F7 规范化（normalization）模块。

规范化必须在硬校验之前执行：上游的 ``手/千元``、百分号、空值令牌、
带空白/BOM 的证券名称与未补零的代码都会让后续阈值校验整体失真。

对外只有两个入口：

* :func:`normalize_panel` —— 返回 ``(DataFrame, AuditReport)``；
* :func:`pct_to_decimal` —— 百分比字符串/小数的标量或 Series 转换工具。
"""

from .normalize import (
    DEFAULT_TICK,
    NULL_TOKENS,
    normalize_panel,
    pct_to_decimal,
)

__all__ = [
    "DEFAULT_TICK",
    "NULL_TOKENS",
    "normalize_panel",
    "pct_to_decimal",
]
