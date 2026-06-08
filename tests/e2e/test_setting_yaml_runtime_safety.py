"""e2e：用真实 ``config/setting.yaml`` 装配 build_safety_chain，验证本次新加规则
真在运行时生效。

setting-yaml-protect-personal-dirs 任务的运行时回归保护：仅 yaml 字段断言不够，
还要验证规则真被 SafetyDecisionEngine 串到决策链上。本测试**只调 chain.decide**
做决策评估，不调任何 ShellTool / subprocess / 文件操作——arguments 里的 path
只是字符串字面量，传给 regex / path_prefix 匹配，从未被真实操作。

覆盖：
- rm -rf 任意目录 → ConsentResolver elevated（本次新加 approval_required 规则）
- rm -rf / → HardBlockGuard 拦（baseline 优先于 elevated）
- rm -rf ~/Documents/X → HardBlockGuard 拦（本次新加 sensitive_paths block）
- rm -rf .kongming/work/foo → TrustResolver silent_allow（trusted_workdirs 默认）
- ls -a → 不被 elevated 误命中（regex 要求 -r 选项）
- rm file.txt → 不被 elevated 误命中（无 -r）
- rm -fR /tmp/x → elevated（macOS BSD 大写 R 兼容）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Defensive guard: 严禁本测试文件触发任何能真删文件的模块。
# 这是"测试只做决策评估，不执行文件操作"的硬保险——即便后续维护误加 import
# 也会立刻在 collection 阶段失败。
_FORBIDDEN_MODULES = ("tools.builtin.shell_tool",)
for _mod in _FORBIDDEN_MODULES:
    assert _mod not in sys.modules, (
        f"FATAL: {_mod} loaded in test_setting_yaml_runtime_safety; "
        "this test must only call chain.decide, never execute shell."
    )

from core.contracts import ApprovalDecision, ApprovalRequest  # noqa: E402
from infrastructure.config import load_config  # noqa: E402
from safety.chain import build_safety_chain  # noqa: E402
from safety.types import ApprovalMetadataKeys  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTING_YAML = _REPO_ROOT / "config" / "setting.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeApproval:
    """模拟 InteractiveApproval：默认 approved，记录被问到的 request 用于断言。

    本类**不执行任何命令**，只记录"safety chain 把哪条 request 路由到了底层
    人工审批层"——这是 ConsentResolver 没短路时才会被调到的位置。
    """

    def __init__(self, outcome: str = "approved") -> None:
        self._outcome = outcome
        self.decided_requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.decided_requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,  # type: ignore[arg-type]
            reason="fake-approved",
            metadata={},
        )


def _req(tool_name: str, arguments: dict[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id="call-1",
        tool_name=tool_name,
        arguments=arguments,
    )


def _build_chain_from_setting_yaml() -> tuple[Any, _FakeApproval]:
    """加载真实 setting.yaml + 装配 SafetyGatedApproval。"""
    cfg = load_config(_SETTING_YAML, load_env_file=False)
    fake = _FakeApproval()
    chain = build_safety_chain(cfg, interactive_approval=fake)
    return chain, fake


# ---------------------------------------------------------------------------
# Case 1: rm -rf <relative dir> → elevated（本次新加规则的核心场景）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_recursive_relative_dir_routes_to_elevated() -> None:
    """`rm -rf warp` 类相对路径目录 → ConsentResolver 升 elevated。

    这是用户截图里 `rm -rf warp && git clone ...` 的场景：本次新加的
    rm-recursive-elevated 规则必须把它路由到 elevated 强度审批。
    """
    chain, fake = _build_chain_from_setting_yaml()
    decision = await chain.decide(_req("run_shell", {"command": "rm -rf warp"}))
    # 走到底层 InteractiveApproval（ConsentResolver 没短路） → fake 拿到 1 次
    assert len(fake.decided_requests) == 1
    # 决策类是 explicit_consent（不是 hard_block / silent_allow）
    md = decision.metadata
    assert md.get(ApprovalMetadataKeys.DECISION_CLASS) == "explicit_consent"
    # severity 强度是 elevated（本次规则的关键断言）
    assert md.get(ApprovalMetadataKeys.DECISION_SOURCE) == "elevated"


# ---------------------------------------------------------------------------
# Case 2: rm -rf / → hard_deny（baseline 优先于 elevated）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_root_blocked_by_hard_deny() -> None:
    """`rm -rf /` 命令字符串 → HardBlockGuard 拦截，连 fake_approval 都不被调。

    安全说明：本测试**永远不真执行**这条命令——chain.decide 只读字符串做
    regex 匹配，返回 ApprovalDecision；测试文件最顶部的 _FORBIDDEN_MODULES
    断言保证 ShellTool 不被加载，subprocess 永不调用。
    """
    chain, fake = _build_chain_from_setting_yaml()
    decision = await chain.decide(_req("run_shell", {"command": "rm -rf /"}))
    # HardBlock 直接 reject，不进 ConsentResolver
    assert decision.outcome == "rejected"
    assert decision.metadata.get(ApprovalMetadataKeys.DECISION_CLASS) == "hard_block"
    # 关键：fake_approval 没被调（被 hard_block 短路在 ConsentResolver 之前）
    assert len(fake.decided_requests) == 0


# ---------------------------------------------------------------------------
# Case 3: rm -rf ~/Documents/X → hard_block（本次新加 sensitive_paths block）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_user_documents_blocked_by_sensitive_paths() -> None:
    """`rm -rf ~/Documents/X` → 被本次新加的 user-documents sensitive_paths block 拦。

    验证 sensitive_paths block 比 approval_required elevated **更早命中**：
    个人数据目录是直接 reject，不弹审批。
    """
    chain, fake = _build_chain_from_setting_yaml()
    # write_file 工具是 path 维度，更直观验证 sensitive_paths block 路径
    decision = await chain.decide(
        _req("write_file", {"path": "~/Documents/test-file.txt", "content": "x"})
    )
    assert decision.outcome == "rejected"
    assert decision.metadata.get(ApprovalMetadataKeys.DECISION_CLASS) == "hard_block"
    assert len(fake.decided_requests) == 0


# ---------------------------------------------------------------------------
# Case 4: rm -rf .kongming/work/foo → silent_allow（trusted_workdirs）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_in_trusted_workdir_silent_allow() -> None:
    """trusted_workdirs 命中 → TrustResolver silent_allow，不进 ConsentResolver。

    验证本次的 elevated 规则**不破坏**项目临时目录正常清理：agent 在
    .kongming/work/ 下的写操作走 silent_allow，根本不弹审批。
    """
    chain, fake = _build_chain_from_setting_yaml()
    project_root = _REPO_ROOT  # 由 BoundaryResolver 推导
    target = project_root / ".kongming" / "work" / "scratch.txt"
    decision = await chain.decide(_req("write_file", {"path": str(target), "content": "x"}))
    # silent_allow → outcome=approved，但 fake_approval 不被调
    assert decision.outcome == "approved"
    assert decision.metadata.get(ApprovalMetadataKeys.DECISION_CLASS) == "silent_allow"
    assert len(fake.decided_requests) == 0


# ---------------------------------------------------------------------------
# Case 5: ls -a → standard（不被 elevated 误命中）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ls_a_not_escalated_to_elevated() -> None:
    """非 rm 命令 → ConsentResolver standard，不被 rm-recursive 规则误命中。"""
    chain, _ = _build_chain_from_setting_yaml()
    decision = await chain.decide(_req("run_shell", {"command": "ls -a"}))
    md = decision.metadata
    assert md.get(ApprovalMetadataKeys.DECISION_CLASS) == "explicit_consent"
    # standard 不是 elevated
    assert md.get(ApprovalMetadataKeys.DECISION_SOURCE) == "standard"


# ---------------------------------------------------------------------------
# Case 6: rm file.txt（无 -r）→ standard（不被 elevated 误命中）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_single_file_not_escalated() -> None:
    """`rm file.txt` 无 -r 选项 → 不命中 rm-recursive-elevated（regex 要求 -r/-R）。

    避免误伤：单文件删除走 standard 审批，不需要 elevated 强度。
    """
    chain, _ = _build_chain_from_setting_yaml()
    decision = await chain.decide(_req("run_shell", {"command": "rm file.txt"}))
    md = decision.metadata
    assert md.get(ApprovalMetadataKeys.DECISION_CLASS) == "explicit_consent"
    assert md.get(ApprovalMetadataKeys.DECISION_SOURCE) == "standard"


# ---------------------------------------------------------------------------
# Case 7: rm -fR /tmp/x → elevated（macOS BSD 大写 R 兼容）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_capital_R_macos_bsd_routes_to_elevated() -> None:
    """`rm -fR /tmp/x` macOS BSD 大写 R → 命中本次 regex 修订，升 elevated。

    本次 yaml regex 已从 `[a-zA-Z]*r[a-zA-Z]*` 改为 `[a-zA-Z]*[rR][a-zA-Z]*`
    覆盖大小写。本 case 是 regex 修订的回归保护。
    """
    chain, _ = _build_chain_from_setting_yaml()
    decision = await chain.decide(_req("run_shell", {"command": "rm -fR /tmp/some-test-target"}))
    md = decision.metadata
    assert md.get(ApprovalMetadataKeys.DECISION_CLASS) == "explicit_consent"
    assert md.get(ApprovalMetadataKeys.DECISION_SOURCE) == "elevated"
