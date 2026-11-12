"""证券主数据模块（F1）。

对外只暴露实现类；契约类型仍从 :mod:`qrg.api` 引用，避免实现细节泄漏。
"""

from .security import SecurityMasterImpl

__all__ = ["SecurityMasterImpl"]
