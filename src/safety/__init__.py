"""Safety Baseline 模块。

v1-mini 第一版只提供 "capability → permission → approval" 三层安全链：

- :class:`CapabilityPolicy` / :class:`CapabilitySet` / :class:`CapabilityCheck`
  —— 粗粒度能力门禁
- :class:`PermissionPolicy` / :class:`PermissionRule` / :class:`PermissionDecision`
  —— 细粒度规则判断
- :class:`SafetyGatedApproval` + :func:`build_safety_chain`
  —— 把三层串成实现 :class:`core.contracts.ApprovalProvider` Protocol 的复合对象

**调用方约束**：

- ``Tool Runtime`` / ``host`` / ``cli`` 不直接 import 内部 policy 组件；
  只通过 :func:`build_safety_chain` 装配出的高层 :class:`SafetyGatedApproval`
  消费最终判定结果。
- ``native_runtime.py``（执行运行时装配层）是唯一被允许跨层 import
  ``safety.*`` 的调用点，已在 ``.importlinter`` 中显式白名单化。
"""

from __future__ import annotations

from safety.capability_policy import (
    CapabilityCheck,
    CapabilityPolicy,
    CapabilitySet,
)
from safety.chain import (
    SafetyChainError,
    SafetyGatedApproval,
    build_safety_chain,
)
from safety.permission_policy import (
    PermissionDecision,
    PermissionOutcome,
    PermissionPolicy,
    PermissionRule,
)

__all__ = [
    "CapabilityCheck",
    "CapabilityPolicy",
    "CapabilitySet",
    "PermissionDecision",
    "PermissionOutcome",
    "PermissionPolicy",
    "PermissionRule",
    "SafetyChainError",
    "SafetyGatedApproval",
    "build_safety_chain",
]
