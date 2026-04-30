"""safety v0.1.4 — guards 子包。

包含三段式决策模型的运行时判定组件：

- :class:`HardBlockGuard`（M2）：命中 secrets / destructive / self-escalation 直接拒绝
- :class:`ConsentResolver`（M4）：standard / elevated 审批分流
- :class:`TrustResolver`（M6）：intrinsic / session / config 信任放行

本包内组件可互相 import；``tools/`` 不允许 import 本包（``.importlinter`` Contract 4）。
"""
