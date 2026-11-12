"""qrg —— 量化研究数据清洗/治理标准库。

设计约束（见 spec/ 与蓝图）：
1. 代码硬边界：复权、网格、PIT 时点、费用/冲击/容量、hash、门禁，一律确定性实现。
2. AI 只在入库前出现；回测只读冻结表。
3. 所有口径可版本化、可回溯、可哈希。
"""

__version__ = "0.2.0"

from .api import (  # noqa: F401
    AuditReport,
    GateBlocked,
    PanelSpec,
    RuleHit,
    Severity,
    SpecError,
)

