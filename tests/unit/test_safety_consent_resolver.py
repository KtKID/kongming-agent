"""unit：safety v0.1.4 ConsentResolver（M4）。

覆盖任务 #22 要求的 12+ 用例：

1. standard ask 路径：write tests/integration/foo.py（untracked） → standard
2. elevated path - .env 读取：read .env.production → elevated + confirm_token
3. elevated path - CLAUDE.md 写入 → matched_rule="config-self-protection-CLAUDE.md"
4. elevated path - .kongming/config.yaml 写入 →
   matched_rule="config-self-protection-config.yaml"
5. elevated path - pyproject.toml [tool.kongming.safety] 段 → elevated
6. standard ask - pyproject.toml 普通段 → standard
7. command path - git push → standard, matched_rule="git-push"
8. command path - git reset --hard → standard, matched_rule="git-reset-hard"
9. standard - 工作区外 write → standard
10. command unknown - 默认 standard fallback（"uv sync"）
11. 元数据透传：interactive_approval 返回 grant_scope=session 应透传
12. decision_source 必填：所有 explicit_consent 决策的 metadata.decision_source
    必须是 "standard" 或 "elevated"

强约束：
- 不修改 src/tools/runtime/approval.py
- 用 FakeApproval 模拟 InteractiveApproval，记录 enriched request 用于断言
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import ApprovalConfig, Config, ModelConfig
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryDecision,
    BoundaryKind,
    BoundaryZone,
    RuntimeBoundaryContext,
)
from safety.guards.consent import ConsentResolver

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        approval=ApprovalConfig(mode="interactive"),
    )


def _runtime() -> RuntimeBoundaryContext:
    return RuntimeBoundaryContext(boundary_kind=BoundaryKind.HOST)


def _approval_zone() -> BoundaryDecision:
    return BoundaryDecision(zone=BoundaryZone.APPROVAL, matched_rule=None, reason="approval")


def _trusted_zone() -> BoundaryDecision:
    return BoundaryDecision(zone=BoundaryZone.TRUSTED, matched_rule=None, reason="trusted")


def _blocked_zone() -> BoundaryDecision:
    return BoundaryDecision(zone=BoundaryZone.BLOCKED, matched_rule=None, reason="blocked")


def _req(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call-1",
    metadata: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        metadata=dict(metadata or {}),
    )


class FakeApproval:
    """模拟 InteractiveApproval：记录 enriched request 并按预设返回。"""

    def __init__(
        self,
        *,
        outcome: str = "approved",
        downstream_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._outcome = outcome
        self._downstream_metadata: dict[str, Any] = dict(downstream_metadata or {})
        self.decided_requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.decided_requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,  # type: ignore[arg-type]
            reason="fake decided",
            metadata=dict(self._downstream_metadata),
        )


def _make_resolver(
    *,
    fake: FakeApproval | None = None,
) -> tuple[ConsentResolver, FakeApproval]:
    fake = fake or FakeApproval()
    resolver = ConsentResolver.from_config(_cfg(), interactive_approval=fake)
    return resolver, fake


# ---------------------------------------------------------------------------
# §1 standard ask 路径
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standard_ask_untracked_test_write() -> None:
    """剧本 1：write tests/integration/foo.py（untracked） → severity=standard。"""
    resolver, fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "tests/integration/foo.py", "content": "print(1)"},
        ),
        _approval_zone(),
        _runtime(),
    )

    assert decision.outcome == "approved"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.STAGE] == "permission"
    assert md[ApprovalMetadataKeys.BOUNDARY_KIND] == "host"
    # standard 不携带 confirm_token
    assert "confirm_token" not in md

    # InteractiveApproval 收到 enriched request
    assert len(fake.decided_requests) == 1
    enriched = fake.decided_requests[0]
    assert enriched.metadata["policy_hint"] == "standard"
    assert enriched.metadata["severity"] == "standard"
    assert "confirm_token" not in enriched.metadata


# ---------------------------------------------------------------------------
# §2 elevated path - .env read
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_env_read_carries_confirm_token() -> None:
    """剧本 2：read .env.production → elevated + confirm_token。"""
    resolver, fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="read_file",
            arguments={"path": ".env.production"},
            call_id="call-env-1",
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.STAGE] == "approval"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "project-env"
    # confirm_token：8 位 hex
    token = md["confirm_token"]
    assert isinstance(token, str)
    assert len(token) == 8
    assert all(c in "0123456789abcdef" for c in token)

    # downstream 收到的 enriched request 也含 confirm_token
    enriched = fake.decided_requests[0]
    assert enriched.metadata["confirm_token"] == token
    assert enriched.metadata["policy_hint"] == "elevated"


# ---------------------------------------------------------------------------
# §3 elevated path - CLAUDE.md write
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_claude_md_write() -> None:
    """剧本 3：write CLAUDE.md → matched_rule=config-self-protection-CLAUDE.md。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "CLAUDE.md", "content": "# updated"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "config-self-protection-CLAUDE.md"
    assert "confirm_token" in md


# ---------------------------------------------------------------------------
# §4 elevated path - .kongming/config.yaml write
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_kongming_config_yaml_write() -> None:
    """剧本 4：write .kongming/config.yaml → matched_rule=config-self-protection-config.yaml。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={
                "path": ".kongming/config.yaml",
                "content": "safety:\n  trusted_workdirs:\n    - .kongming/work/\n",
            },
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "config-self-protection-config.yaml"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_kongming_home_config_write_is_standard(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写用户级 kongming_home/config.yaml → standard，支持默认同意审批 lane。"""
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    resolver, fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={
                "path": str(tmp_path / ".kongming" / "config.yaml"),
                "content": "session:\n  backend: file\n",
            },
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "write-default"
    assert "confirm_token" not in md
    assert fake.decided_requests[0].metadata["severity"] == "standard"


# ---------------------------------------------------------------------------
# §5 elevated path - pyproject.toml [tool.kongming.safety] 段
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_pyproject_safety_section_write() -> None:
    """剧本 5：write pyproject.toml + 含 tool.kongming.safety → elevated。"""
    resolver, _fake = _make_resolver()
    content = (
        "[project]\nname = 'foo'\n\n[tool.kongming.safety]\nallow_writes = ['.kongming/work/']\n"
    )
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "pyproject.toml", "content": content},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "elevated"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "pyproject-safety-section"


# ---------------------------------------------------------------------------
# §6 standard - pyproject.toml 普通段
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standard_pyproject_normal_section_write() -> None:
    """剧本 6：write pyproject.toml + 不含 tool.kongming.safety → standard。"""
    resolver, _fake = _make_resolver()
    content = "[project]\nname = 'foo'\nversion = '0.1.0'\n"
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "pyproject.toml", "content": content},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    # 普通段没有命中 ConfigSelfProtection 也没有命中 sensitive_paths
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "write-default"


# ---------------------------------------------------------------------------
# §7 command path - git push
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_command_git_push_standard() -> None:
    """剧本 7：run_shell 'git push origin main' → standard, matched_rule=git-push。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="run_shell",
            arguments={"command": "git push origin main"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "git-push"


# ---------------------------------------------------------------------------
# §8 command path - git reset --hard
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_command_git_reset_hard_standard() -> None:
    """剧本 8：git reset --hard → standard, matched_rule=git-reset-hard。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="run_shell",
            arguments={"command": "git reset --hard HEAD~1"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "git-reset-hard"


# ---------------------------------------------------------------------------
# §9 standard - 工作区外 write
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standard_external_write() -> None:
    """剧本 9：write /tmp/xxx.txt → standard。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "/tmp/scratch.txt", "content": "x"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "write-default"


# ---------------------------------------------------------------------------
# §10 command unknown - 默认 standard
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_command_unknown_default_standard() -> None:
    """剧本 10：run_shell 'uv sync' → standard fallback。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="run_shell",
            arguments={"command": "uv sync"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "shell-command-default"


# ---------------------------------------------------------------------------
# §10b agent workflow 工具 - 已知 workflow 编排工具
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_workflow_tool_standard() -> None:
    """剧本 10b：run_agent_workflow → standard, matched_rule=agent-workflow-default。"""
    resolver, fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(
            tool_name="run_agent_workflow",
            arguments={
                "mode": "map_reduce",
                "payload": {"objective": "分析 src/executors"},
            },
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "agent-workflow-default"
    assert "未知工具" not in md[ApprovalMetadataKeys.REASON]
    enriched = fake.decided_requests[0]
    assert enriched.metadata["severity"] == "standard"


# ---------------------------------------------------------------------------
# §11 元数据透传 grant_scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grant_scope_session_metadata_passthrough() -> None:
    """剧本 11：interactive_approval 返回 grant_scope=session 应透传到最终 metadata。"""
    fake = FakeApproval(
        outcome="approved",
        downstream_metadata={"grant_scope": "session", "source": "interactive"},
    )
    resolver, _ = _make_resolver(fake=fake)
    decision = await resolver.evaluate(
        _req(
            tool_name="write_file",
            arguments={"path": "tests/integration/new_test.py", "content": "x"},
        ),
        _approval_zone(),
        _runtime(),
    )

    md = decision.metadata
    # downstream 的 grant_scope 透传
    assert md["grant_scope"] == "session"
    # downstream 的 source 字段也透传
    assert md["source"] == "interactive"
    # v0.1.4 标准字段仍然在
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"


# ---------------------------------------------------------------------------
# §12 decision_source 必填
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decision_source_always_present() -> None:
    """剧本 12：所有 explicit_consent 决策的 decision_source 必须是 standard 或 elevated。"""
    resolver, _fake = _make_resolver()

    test_cases: list[ApprovalRequest] = [
        _req(tool_name="write_file", arguments={"path": "src/foo.py", "content": "x"}),
        _req(tool_name="read_file", arguments={"path": ".env"}),
        _req(tool_name="run_shell", arguments={"command": "git push"}),
        _req(tool_name="run_shell", arguments={"command": "ls -la"}),
        _req(tool_name="write_file", arguments={"path": "AGENTS.md", "content": "x"}),
        _req(tool_name="list_dir", arguments={"path": "/tmp"}),
        _req(
            tool_name="write_file", arguments={"path": ".kongming/safety/extra.yaml", "content": ""}
        ),
    ]

    for req in test_cases:
        decision = await resolver.evaluate(req, _approval_zone(), _runtime())
        md = decision.metadata
        assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent", req
        source = md[ApprovalMetadataKeys.DECISION_SOURCE]
        assert source in {"standard", "elevated"}, (
            f"unexpected decision_source={source!r} for {req.tool_name} {req.arguments}"
        )
        # boundary_kind 恒 host
        assert md[ApprovalMetadataKeys.BOUNDARY_KIND] == "host"
        # stage 兼容字段必须根据 source 映射
        if source == "standard":
            assert md[ApprovalMetadataKeys.STAGE] == "permission"
        else:
            assert md[ApprovalMetadataKeys.STAGE] == "approval"


# ---------------------------------------------------------------------------
# §13 BLOCKED zone 不应到达（HardBlock 负责拦截）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocked_zone_assertion_fires() -> None:
    """BLOCKED zone 到达 ConsentResolver 应触发 assert（HardBlock 应已拦截）。

    AssertionError 由 evaluate 内 ``assert False`` 抛出；这是 defensive 设计。
    """
    resolver, _fake = _make_resolver()
    with pytest.raises(AssertionError):
        await resolver.evaluate(
            _req(tool_name="write_file", arguments={"path": "~/.ssh/id_rsa", "content": "x"}),
            _blocked_zone(),
            _runtime(),
        )


# ---------------------------------------------------------------------------
# §14 reject 透传
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reject_outcome_passthrough() -> None:
    """InteractiveApproval 返回 rejected → ConsentResolver 也返回 rejected，但 metadata 装饰齐全。"""
    fake = FakeApproval(outcome="rejected", downstream_metadata={"source": "user"})
    resolver, _ = _make_resolver(fake=fake)
    decision = await resolver.evaluate(
        _req(tool_name="run_shell", arguments={"command": "git push origin main"}),
        _approval_zone(),
        _runtime(),
    )

    assert decision.outcome == "rejected"
    md = decision.metadata
    assert md[ApprovalMetadataKeys.DECISION_CLASS] == "explicit_consent"
    assert md[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
    assert md[ApprovalMetadataKeys.MATCHED_RULE] == "git-push"


# ---------------------------------------------------------------------------
# §15 confirm_token 是确定性派生（同 call_id + 同 rule 名 → 同 token）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_token_deterministic_per_call_id() -> None:
    """同 call_id + 同规则名应派生同一个 confirm_token；不同 call_id 应不同。"""
    resolver, _fake = _make_resolver()

    decision_a = await resolver.evaluate(
        _req(tool_name="read_file", arguments={"path": ".env"}, call_id="call-A"),
        _approval_zone(),
        _runtime(),
    )
    decision_b = await resolver.evaluate(
        _req(tool_name="read_file", arguments={"path": ".env"}, call_id="call-A"),
        _approval_zone(),
        _runtime(),
    )
    decision_c = await resolver.evaluate(
        _req(tool_name="read_file", arguments={"path": ".env"}, call_id="call-B"),
        _approval_zone(),
        _runtime(),
    )

    assert decision_a.metadata["confirm_token"] == decision_b.metadata["confirm_token"]
    assert decision_a.metadata["confirm_token"] != decision_c.metadata["confirm_token"]


# ---------------------------------------------------------------------------
# §16 trusted zone 仍可被 ConsentResolver 调用（理论上 TrustResolver 已捕获，
# 但 evaluate 不应崩；保守 standard）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trusted_zone_also_works() -> None:
    """TRUSTED zone 调用 evaluate 应正常返回 standard（不抛异常）。"""
    resolver, _fake = _make_resolver()
    decision = await resolver.evaluate(
        _req(tool_name="write_file", arguments={"path": "src/foo.py", "content": "x"}),
        _trusted_zone(),
        _runtime(),
    )
    assert decision.outcome == "approved"
    assert decision.metadata[ApprovalMetadataKeys.DECISION_SOURCE] == "standard"
