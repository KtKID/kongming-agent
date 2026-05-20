"""safety — guards 子包。

包含决策链的运行时判定组件（四级从严到松）：

- :class:`HardBlockGuard`（M2）：命中 secrets / destructive / self-escalation 直接拒绝
- :class:`DestructiveForceAskGuard`：rm -r* / shred / srm → 无视 grant，强制 consent
- :class:`TrustResolver`（M6）：intrinsic / session / config 信任放行
- :class:`ConsentResolver`（M4）：standard / elevated 审批分流

本包内组件可互相 import；``tools/`` 不允许 import 本包（``.importlinter`` Contract 4）。
"""
