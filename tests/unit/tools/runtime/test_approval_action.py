"""unit：safety v0.1.4 M5.3 — :class:`tools.runtime.approval.InteractiveApproval`
对 :class:`core.contracts.ApprovalAction` 协议的支持。

覆盖：

1. 接受 ``PromptFn`` (旧 bool) 时 True/False → ACCEPT_ONCE/REJECT 兼容映射
2. 接受 ``PromptActionFn`` (action-aware) 时 4 种 action 全映射
3. ACCEPT_FOR_SESSION → metadata.grant_scope = "session"
4. ACCEPT_PERSIST → metadata.grant_scope = "config"
5. elevated policy_hint + confirm_token 透传到 metadata
6. ``mark_action_aware`` 装饰器与裸函数注解探测两种声明方式
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
)
from tools.runtime.approval import (
    InteractiveApproval,
    PromptActionFn,
    _is_action_aware,
    mark_action_aware,
)


def _make_request(
    *,
    metadata: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        session_id="sess-1",
        turn=1,
        call_id="call-1",
        tool_name="write_file",
        arguments={"path": "tests/x.py"},
        reason="needs approval",
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# bool 兼容路径
# ---------------------------------------------------------------------------


class TestBoolPromptCompat:
    async def test_true_maps_to_accept_once(self) -> None:
        async def prompt(req: ApprovalRequest) -> bool:
            return True

        provider = InteractiveApproval(prompt)
        decision = await provider.decide(_make_request())

        assert decision.outcome == "approved"
        assert "grant_scope" not in decision.metadata
        assert decision.metadata.get("source") == "interactive"

    async def test_false_maps_to_reject(self) -> None:
        async def prompt(req: ApprovalRequest) -> bool:
            return False

        provider = InteractiveApproval(prompt)
        decision = await provider.decide(_make_request())

        assert decision.outcome == "rejected"
        assert "grant_scope" not in decision.metadata


# ---------------------------------------------------------------------------
# ApprovalAction 路径
# ---------------------------------------------------------------------------


class TestActionPrompt:
    @pytest.mark.parametrize(
        ("action", "expected_outcome", "expected_grant_scope"),
        [
            (ApprovalAction.ACCEPT_ONCE, "approved", None),
            (ApprovalAction.ACCEPT_FOR_SESSION, "approved", "session"),
            (ApprovalAction.ACCEPT_PERSIST, "approved", "config"),
            (ApprovalAction.REJECT, "rejected", None),
        ],
    )
    async def test_action_to_decision_full_matrix(
        self,
        action: ApprovalAction,
        expected_outcome: str,
        expected_grant_scope: str | None,
    ) -> None:
        @mark_action_aware
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            return action

        provider = InteractiveApproval(prompt)
        decision = await provider.decide(_make_request())

        assert decision.outcome == expected_outcome
        if expected_grant_scope is None:
            assert "grant_scope" not in decision.metadata
        else:
            assert decision.metadata.get("grant_scope") == expected_grant_scope

    async def test_decorator_marks_action_aware(self) -> None:
        @mark_action_aware
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            return ApprovalAction.ACCEPT_ONCE

        assert _is_action_aware(prompt) is True

    async def test_annotation_only_also_action_aware(self) -> None:
        # 不用装饰器，但返回类型注解是 ApprovalAction → 也应被识别
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            return ApprovalAction.ACCEPT_ONCE

        assert _is_action_aware(prompt) is True

    async def test_unmarked_bool_prompt_is_not_action_aware(self) -> None:
        async def prompt(req: ApprovalRequest) -> bool:
            return True

        assert _is_action_aware(prompt) is False


# ---------------------------------------------------------------------------
# elevated metadata 透传
# ---------------------------------------------------------------------------


class TestElevatedMetadata:
    async def test_policy_hint_and_token_passthrough(self) -> None:
        captured: dict[str, Any] = {}

        @mark_action_aware
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            captured["policy_hint"] = req.metadata.get("policy_hint")
            captured["confirm_token"] = req.metadata.get("confirm_token")
            return ApprovalAction.ACCEPT_ONCE

        provider = InteractiveApproval(prompt)
        request = _make_request(metadata={"policy_hint": "elevated", "confirm_token": "abc12345"})
        decision = await provider.decide(request)

        # prompt_fn 收到原始 metadata
        assert captured == {"policy_hint": "elevated", "confirm_token": "abc12345"}
        # decision.metadata 也透传 elevated 字段，便于 trace 审计
        assert decision.metadata.get("policy_hint") == "elevated"
        assert decision.metadata.get("confirm_token") == "abc12345"

    async def test_standard_request_does_not_carry_elevated_keys(self) -> None:
        @mark_action_aware
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            return ApprovalAction.ACCEPT_ONCE

        provider = InteractiveApproval(prompt)
        decision = await provider.decide(_make_request())

        assert "policy_hint" not in decision.metadata
        assert "confirm_token" not in decision.metadata


# ---------------------------------------------------------------------------
# 错误 / 边界
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_none_prompt_fn_raises(self) -> None:
        with pytest.raises(ValueError, match="non-None prompt_fn"):
            InteractiveApproval(None)  # type: ignore[arg-type]

    async def test_decision_is_dataclass_instance(self) -> None:
        async def prompt(req: ApprovalRequest) -> bool:
            return True

        provider = InteractiveApproval(prompt)
        decision = await provider.decide(_make_request())
        assert isinstance(decision, ApprovalDecision)


# ---------------------------------------------------------------------------
# build_default_approval 接口路径（确保 PromptActionFn 也能注入）
# ---------------------------------------------------------------------------


class TestBuildDefaultApproval:
    async def test_action_aware_prompt_works_via_factory(self) -> None:
        from tools.runtime.approval import build_default_approval

        @mark_action_aware
        async def prompt(req: ApprovalRequest) -> ApprovalAction:
            return ApprovalAction.ACCEPT_FOR_SESSION

        provider = build_default_approval("interactive", prompt_fn=prompt)
        decision = await provider.decide(_make_request())

        assert decision.outcome == "approved"
        assert decision.metadata.get("grant_scope") == "session"

    def test_factory_rejects_interactive_without_prompt(self) -> None:
        from tools.runtime.approval import build_default_approval

        with pytest.raises(ValueError, match="prompt_fn"):
            build_default_approval("interactive")


# 兜住 PromptActionFn 类型 alias 不被当作未使用 import 删除
_PROMPT_ACTION_FN_REF: type = type(PromptActionFn)  # type: ignore[misc]
