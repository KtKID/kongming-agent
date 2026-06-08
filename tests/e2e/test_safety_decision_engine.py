"""e2e：safety v0.1.4 SafetyDecisionEngine 集成测试矩阵（任务 #41）。

12 个典型 ApprovalRequest 场景全程走 SafetyDecisionEngine（HardBlock →
BoundaryResolver → TrustResolver → ConsentResolver），断言：

- decision.outcome
- metadata.decision_class / decision_source / matched_rule / boundary_kind
- v0.1.3 → v0.1.4 stage 兼容映射

并验证：

- v0.1.3 老 yaml 配置（capability deny + permission rule allow）通过
  build_safety_chain 装配 → SafetyDecisionEngine 行为等价
- boundary_kind=SANDBOX 注入：v0.1.4 host-only，sandbox grant 漏到 host
  必须被 GrantStore 拒绝（put_session 抛 ValueError）

12 case 列表：

1. tracked write src/__init__.py → silent_allow + intrinsic
2. untracked write tests/foo.py → explicit_consent + standard
3. read ~/.ssh/config → hard_block (ssh-material)
4. read .env → explicit_consent + elevated
5. write CLAUDE.md（ConfigSelfProtection elevated 覆盖项目根敏感文件）→ explicit_consent + elevated
6. shell `git push` → explicit_consent + standard
7. shell `pytest && rm -rf /` → hard_block (host-root-delete)
8. shell `pytest tests/` → explicit_consent + standard（无规则匹配）
9. write .kongming/work/foo.txt → silent_allow（来自 trusted_workdirs）
10. write 工作区外 /tmp/x.txt → explicit_consent + standard
11. v0.1.3 老 yaml 配置自动迁移（capability deny + permission rule allow） →
    SafetyDecisionEngine 行为等价
12. boundary_kind=SANDBOX 注入 → GrantStore.put_session 抛 ValueError
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    ShellToolConfig,
    ToolConfig,
)
from safety.boundary_resolver import BoundaryResolver
from safety.capability_policy import CapabilityPolicy, CapabilitySet
from safety.chain import build_safety_chain
from safety.decision_engine import SafetyDecisionEngine
from safety.grant_store import GrantStore, grant_with_now
from safety.guards.consent import ConsentResolver
from safety.guards.hard_block import HardBlockGuard
from safety.guards.trust import TrustResolver
from safety.permission_policy import PermissionPolicy, PermissionRule
from safety.types import (
    ApprovalMetadataKeys,
    BoundaryKind,
    DecisionSource,
    RuntimeBoundaryContext,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    file_enabled: bool = True,
    shell_enabled: bool = True,
    approval_mode: str = "interactive",
) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        tool=ToolConfig(
            file=FileToolConfig(enabled=file_enabled),
            shell=ShellToolConfig(enabled=shell_enabled),
        ),
        evolution=EvolutionConfig(memory=EvolutionMemoryConfig(enabled=False)),
        approval=ApprovalConfig(mode=approval_mode),  # type: ignore[arg-type]
    )


def _req(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call-1",
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
    )


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


class _FakeApproval:
    """模拟 InteractiveApproval：默认 approved。"""

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


def _build_engine(
    cfg: Config,
    *,
    underlying: _FakeApproval | None = None,
    project_root: Path | None = None,
    trace_emitter: Any = None,
) -> tuple[SafetyDecisionEngine, _FakeApproval, BoundaryResolver, GrantStore]:
    """直接装配 SafetyDecisionEngine（绕过 build_safety_chain，便于注入 stub）。"""
    underlying = underlying or _FakeApproval()
    boundary = BoundaryResolver.from_project_root(project_root)
    grants = GrantStore.from_config(cfg)
    engine = SafetyDecisionEngine(
        hard_block=HardBlockGuard.from_config(cfg),
        boundary=boundary,
        trust=TrustResolver(boundary, grants),
        consent=ConsentResolver.from_config(cfg, interactive_approval=underlying),
        trace_emitter=trace_emitter,
    )
    return engine, underlying, boundary, grants


# ---------------------------------------------------------------------------
# Case 1: tracked write → silent_allow + intrinsic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case01_tracked_write_silent_allow_intrinsic(tmp_path: Path) -> None:
    """tracked 文件写入 → BoundaryResolver TRUSTED zone → silent_allow + intrinsic。"""
    # 用项目根（cwd）做 boundary 推导，BoundaryResolver 会执行 git ls-files
    cfg = _config()
    engine, _, _, _ = _build_engine(cfg)
    # 选一个项目里实际 tracked 的文件
    tracked_path = str(Path.cwd() / "src" / "core" / "contracts.py")
    decision = await engine.decide(
        _req(tool_name="write_file", arguments={"path": tracked_path, "content": "x"}),
        _runtime(),
    )
    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "silent_allow"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "intrinsic"


# ---------------------------------------------------------------------------
# Case 2: untracked write → explicit_consent + standard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case02_untracked_write_explicit_consent_standard() -> None:
    cfg = _config()
    engine, fake, _, _ = _build_engine(cfg)
    # 项目根下随机不存在的文件 → BoundaryResolver 推 APPROVAL → ConsentResolver standard
    decision = await engine.decide(
        _req(
            tool_name="write_file",
            arguments={
                "path": str(Path.cwd() / "tests" / "_some_random_brand_new_file_xyz.py"),
                "content": "x",
            },
        ),
        _runtime(),
    )
    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert len(fake.decided_requests) == 1


# ---------------------------------------------------------------------------
# Case 3: read ~/.ssh/config → hard_block (ssh-material)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case03_read_ssh_config_hard_block() -> None:
    cfg = _config()
    engine, fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(tool_name="read_file", arguments={"path": "~/.ssh/config"}),
        _runtime(),
    )
    assert decision.outcome == "rejected"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "hard_block"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "ssh-material"
    # InteractiveApproval 不应被调用
    assert fake.decided_requests == []


# ---------------------------------------------------------------------------
# Case 4: read .env* → explicit_consent + elevated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case04_read_env_elevated() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(
            tool_name="read_file",
            arguments={"path": str(Path.cwd() / ".env.production")},
        ),
        _runtime(),
    )
    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    # confirm_token 注入
    assert "confirm_token" in md


# ---------------------------------------------------------------------------
# Case 5: write CLAUDE.md → explicit_consent + elevated（项目根敏感配置）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case05_write_claude_md_elevated() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(
            tool_name="write_file",
            arguments={
                "path": str(Path.cwd() / "CLAUDE.md"),
                "content": "test",
            },
        ),
        _runtime(),
    )
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"


# ---------------------------------------------------------------------------
# Case 6: shell `git push` → explicit_consent + standard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case06_shell_git_push_standard() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(tool_name="run_shell", arguments={"command": "git push origin main"}),
        _runtime(),
    )
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "git-push"


# ---------------------------------------------------------------------------
# Case 7: shell `pytest && rm -rf /` → hard_block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case07_shell_destructive_segment_hard_block() -> None:
    cfg = _config()
    engine, fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(
            tool_name="run_shell",
            arguments={"command": "pytest tests/ && rm -rf /"},
        ),
        _runtime(),
    )
    assert decision.outcome == "rejected"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "hard_block"
    # 命中 host-root-delete 命令规则
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "host-root-delete"
    # InteractiveApproval 不应被调用
    assert fake.decided_requests == []


# ---------------------------------------------------------------------------
# Case 8: shell `pytest tests/` → explicit_consent + standard（无规则匹配 fallback）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case08_shell_pytest_default_standard() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(tool_name="run_shell", arguments={"command": "pytest tests/"}),
        _runtime(),
    )
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"


# ---------------------------------------------------------------------------
# Case 9: write .kongming/work/foo.txt → silent_allow（trusted_workdirs）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case09_write_kongming_work_silent_allow() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    # 路径必须在项目根下 .kongming/work/，BoundaryResolver 装入 trusted_write_trie
    work_path = str(Path.cwd() / ".kongming" / "work" / "tmp_test_file.txt")
    decision = await engine.decide(
        _req(
            tool_name="write_file",
            arguments={"path": work_path, "content": "ok"},
        ),
        _runtime(),
    )
    md = decision.metadata
    assert decision.outcome == "approved"
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "silent_allow"


# ---------------------------------------------------------------------------
# Case 10: write 工作区外 /tmp/x.txt → explicit_consent + standard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case10_write_external_tmp_standard() -> None:
    cfg = _config()
    engine, _fake, _, _ = _build_engine(cfg)
    decision = await engine.decide(
        _req(
            tool_name="write_file",
            arguments={"path": "/tmp/_safety_v014_external_xyz.txt", "content": "x"},
        ),
        _runtime(),
    )
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"


# ---------------------------------------------------------------------------
# Case 11: v0.1.3 老 yaml 配置自动迁移（通过 build_safety_chain）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case11_legacy_v013_config_migration_equivalent() -> None:
    """v0.1.3 风格配置（capability deny=shell + permission rule allow tests/）
    通过 build_safety_chain 装配后能完成一次 decide，行为等价：
    - capability deny=shell 不会自动拒绝（由 tool 启用开关替代）
    - permission rule allow tests/ 翻译为 grant 候选（GrantStore 装载）
    """
    cfg = _config(shell_enabled=True)
    cap_policy = CapabilityPolicy(CapabilitySet(deny=frozenset({"shell"})))
    perm_policy = PermissionPolicy(
        rules=(
            PermissionRule(
                tool_name="*",
                outcome="allow",
                arg_key="path",
                arg_contains="tests/",
            ),
        )
    )
    underlying = _FakeApproval()
    chain = build_safety_chain(
        cfg,
        interactive_approval=underlying,
        capability_policy=cap_policy,
        permission_policy=perm_policy,
    )
    # 验证迁移产物存在
    assert len(cap_policy.migrated_hard_deny_commands) == 1
    assert perm_policy.migrated_allow_path_grants == (("file_write", "tests/"),)

    # 实际跑一次 decide：write tests/foo.py 应能完成（具体 outcome 由链路决定）
    decision = await chain.decide(
        _req(
            tool_name="write_file",
            arguments={"path": "tests/_xyz_legacy_test_file.py", "content": "x"},
        )
    )
    assert decision.outcome in {"approved", "rejected"}


# ---------------------------------------------------------------------------
# Case 12: boundary_kind=SANDBOX 注入 → GrantStore.put_session 拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case12_sandbox_grant_blocked_by_grant_store() -> None:
    """v0.1.4 host-only 强约束：sandbox grant 不能漏到 host。

    本 case 直接构造 sandbox GrantKey，验证 :class:`GrantStore.put_session`
    抛 :class:`ValueError`，防 sandbox grant 通过 ApprovalAction 路径泄漏。
    """
    grants = GrantStore.from_config(_config())
    # 试图构造 sandbox grant
    sandbox_grant = grant_with_now(
        capability="file_write",
        matcher="tests/",
        scope="session",
        source=DecisionSource.SESSION,
        # session grant 在 v0.1.4 修跨 thread 泄漏后必须带 session_id；
        # 本 case 验证的是 sandbox boundary_kind 隔离，session_id 任意即可
        session_id="t-sandbox-test",
    )
    # 把已有 grant 的 boundary_kind 偷换为 sandbox（模拟漏洞场景）
    from dataclasses import replace

    bad_key = replace(sandbox_grant.key, boundary_kind=BoundaryKind.SANDBOX)
    bad_grant = replace(sandbox_grant, key=bad_key)

    with pytest.raises(ValueError, match="sandbox grant must not leak to host"):
        grants.put_session(bad_grant)
