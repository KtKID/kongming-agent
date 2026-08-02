"""unit：Disposition 分类器（task-2，模块 D）全覆盖测试。

覆盖 classify_result 纯函数对每个 Result.status × metadata 组合的输出断言：
- completed → Disposition("deliver_up", "completed")；
  completed + MaxTurnsExceededError → Disposition("deliver_up", "max_turns")
- cancelled + cancel_reason∈{user_interrupt, hook_blocked, parent_cascade, watchdog}
  → Disposition(action 二选一, reason, tree_wide 推荐值)；未知/空 reason →
  Disposition("emit_only", "user_interrupt", tree_wide=True)
- failed + ProviderError → Disposition("deliver_up", llm.*) 按 status_code 细分；
  failed + ToolError → Disposition("deliver_up", "tool.execution")；
  failed + error=None → Disposition("deliver_up", "internal.bug")
- 兜底：非法 status、空 metadata、classify_result 永不抛异常
- 幂等：同输入两次调用返回相等 Disposition
- build_mail 助手：按 reason 构造 child_result Mail（payload 区分 completed /
  exhausted / failed / 局部 cancel）

老结构（Outcome 基类 + Completed/Exhausted/Cancelled/Failed 4 子类 + classify +
deliver_up/deliver_failure_up/deliver_partial_up/deliver_cancelled_up 4 助手）已
删除，替换为单 Disposition dataclass（action + reason + tree_wide）+ classify_result
+ build_mail 两个纯函数式 API。消费侧不再 isinstance 分发，只按 action 二选一。

事实源：
- docs/spec/agent-tree-v0.1/04-data-and-state.md（Disposition 字段定义）
- docs/research/claude错误分类器.md（query.ts reason 表 → Reason 映射）
- dev-pipeline/tasks/agent-tree-outcome-classifier/README.md（reason 映射表 +
  tree_wide 推荐值）
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from core.errors import (
    AgentError,
    MaxTurnsExceededError,
    ProviderError,
    ToolError,
)
from core.message import Message
from core.outcome import (
    Disposition,
    Reason,
    build_mail,
    classify_result,
)
from core.result import Result, ResultStatus

EPOCH = 7  # current_tree_epoch 仅用于日志，推荐值不依赖它；用任意值验证幂等。


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------


def _result(
    *,
    status: ResultStatus,
    error: AgentError | None = None,
    metadata: dict[str, object] | None = None,
    final_message: Message | None = None,
    turn_count: int = 0,
) -> Result:
    """构造测试用 Result（绕过 frozen 默认值不便的写法）。"""
    return Result(
        run_id="run-test",
        session_id="sess-test",
        status=status,
        final_message=final_message,
        turn_count=turn_count,
        error=error,
        metadata=metadata if metadata is not None else {},
    )


def _provider(status_code: int | None = None, **details: Any) -> ProviderError:
    """构造 ProviderError，按需带 status_code（模拟 provider 现状）。"""
    if status_code is not None:
        details["status_code"] = status_code
    return ProviderError(f"provider err {details}", details=details)


def _disp(
    action: str = "deliver_up",
    reason: Reason = "completed",
    tree_wide: bool = False,
) -> Disposition:
    """构造 Disposition 便捷包装（cast action/reason 避开 mypy Literal 收窄噪声）。"""
    return Disposition(
        action=cast(Any, action),  # type: ignore[arg-type]
        reason=reason,
        tree_wide=tree_wide,
    )


# ---------------------------------------------------------------------------
# 契约：Disposition 类型定义（DoD #1）
# ---------------------------------------------------------------------------


class TestDispositionContract:
    """单 frozen dataclass（不再 4 子类）：action / reason / tree_wide 三字段。"""

    def test_disposition_is_frozen_dataclass(self) -> None:
        """Disposition 是 frozen dataclass，三字段齐全（替代 4 子类）。"""
        from dataclasses import is_dataclass

        assert is_dataclass(Disposition)
        d = Disposition(action="deliver_up", reason="completed")
        # 字段可读。
        assert d.action == "deliver_up"
        assert d.reason == "completed"
        assert d.tree_wide is False  # 默认 False

    def test_tree_wide_defaults_false(self) -> None:
        """非 cancel 类 reason 的 tree_wide 固定 False（默认值）。"""
        d = Disposition(action="deliver_up", reason="completed")
        assert d.tree_wide is False

    def test_disposition_frozen_immutable(self) -> None:
        """frozen=True：字段不可变（防止下游误改 Disposition）。"""
        d = Disposition(action="deliver_up", reason="completed")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            d.reason = "internal.bug"  # type: ignore[misc]

    def test_disposition_equality(self) -> None:
        """frozen dataclass 值相等：同字段相等、异字段不等（消费侧按 == 查表）。"""
        a = Disposition(action="deliver_up", reason="completed")
        b = Disposition(action="deliver_up", reason="completed")
        c = Disposition(action="emit_only", reason="user_interrupt", tree_wide=True)
        assert a == b
        assert a != c


# ---------------------------------------------------------------------------
# classify_result：status=completed（DoD reason 映射 #1）
# ---------------------------------------------------------------------------


class TestClassifyCompleted:
    def test_completed_plain(self) -> None:
        r = _result(status="completed", final_message=Message(role="assistant", content="done"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "completed")

    def test_completed_with_usage_metadata(self) -> None:
        r = _result(status="completed", metadata={"usage": {"input": 10}})
        assert classify_result(r, EPOCH) == Disposition("deliver_up", "completed")

    def test_completed_empty_metadata(self) -> None:
        r = _result(status="completed", metadata={})
        assert classify_result(r, EPOCH) == Disposition("deliver_up", "completed")

    def test_completed_with_max_turns_error_is_exhausted(self) -> None:
        """completed + error=MaxTurnsExceededError → Disposition("deliver_up", "max_turns")。"""
        r = _result(status="completed", error=MaxTurnsExceededError("max turns"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "max_turns")

    def test_completed_no_max_turns_info_is_completed(self) -> None:
        """completed 无 error、metadata 无 max_turns 标记 → 默认 completed。"""
        r = _result(status="completed", turn_count=50, metadata={})
        assert classify_result(r, EPOCH) == Disposition("deliver_up", "completed")


# ---------------------------------------------------------------------------
# classify_result：status=cancelled（DoD reason 映射 #2 + tree_wide 推荐值 + action 二选一）
# ---------------------------------------------------------------------------


class TestClassifyCancelled:
    @pytest.mark.parametrize(
        "reason,expected_tree_wide,expected_action",
        [
            ("user_interrupt", True, "emit_only"),
            ("parent_cascade", True, "emit_only"),
            ("hook_blocked", False, "deliver_up"),
            ("watchdog", True, "emit_only"),
        ],
    )
    def test_cancelled_known_reasons(
        self, reason: str, expected_tree_wide: bool, expected_action: str
    ) -> None:
        """已知 cancel_reason → reason 透传 + tree_wide 推荐值 + action 二选一。

        action 仅由 tree_wide 决定：tree_wide=True→emit_only（树级不上投）；
        tree_wide=False→deliver_up（局部取消上投 cancelled notice）。
        """
        r = _result(
            status="cancelled",
            metadata={
                "cancelled_at_turn": 3,
                "cancelled_tool_call_id": None,
                "cancel_reason": reason,
            },
        )
        d = classify_result(r, EPOCH)
        assert d.reason == reason
        assert d.tree_wide is expected_tree_wide
        assert d.action == expected_action

    def test_cancelled_runner_default_user_interrupt(self) -> None:
        """runner 现状（runner.py:428）写 cancel_reason=user_interrupt。"""
        r = _result(
            status="cancelled",
            metadata={
                "cancelled_at_turn": 2,
                "cancelled_tool_call_id": "call_abc",
                "cancel_reason": "user_interrupt",
            },
        )
        d = classify_result(r, EPOCH)
        assert d == Disposition("emit_only", "user_interrupt", tree_wide=True)

    def test_cancelled_unknown_reason_fallbacks_user_interrupt(self) -> None:
        """未知 cancel_reason → 兜底 user_interrupt（不抛异常）。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "weird_reason"})
        d = classify_result(r, EPOCH)
        assert d == Disposition("emit_only", "user_interrupt", tree_wide=True)

    def test_cancelled_missing_cancel_reason(self) -> None:
        """空 metadata → 兜底 user_interrupt。"""
        r = _result(status="cancelled", metadata={})
        d = classify_result(r, EPOCH)
        assert d == Disposition("emit_only", "user_interrupt", tree_wide=True)

    def test_cancelled_empty_cancel_reason_string(self) -> None:
        """cancel_reason=""（空串）→ 兜底 user_interrupt。"""
        r = _result(status="cancelled", metadata={"cancel_reason": ""})
        d = classify_result(r, EPOCH)
        assert d.reason == "user_interrupt"
        assert d.action == "emit_only"
        assert d.tree_wide is True

    def test_cancelled_non_string_reason(self) -> None:
        """cancel_reason 非 str（异常写入）→ 兜底 user_interrupt，不抛异常。"""
        r = _result(status="cancelled", metadata={"cancel_reason": 123})
        d = classify_result(r, EPOCH)
        assert d == Disposition("emit_only", "user_interrupt", tree_wide=True)

    def test_cancelled_tree_wide_recommendation_table(self) -> None:
        """tree_wide 推荐值静态表：user_interrupt→True / hook_blocked→False 等。"""
        cases = {
            "user_interrupt": True,
            "parent_cascade": True,
            "hook_blocked": False,
            "watchdog": True,
        }
        for reason, expected in cases.items():
            r = _result(status="cancelled", metadata={"cancel_reason": reason})
            d = classify_result(r, EPOCH)
            assert d.tree_wide is expected, f"reason={reason}"


# ---------------------------------------------------------------------------
# classify_result：action 二选一（新增 —— 老断言无法覆盖）
# ---------------------------------------------------------------------------


class TestClassifyCancelledActionSplit:
    """cancel_reason 的 action 分支：hook_blocked→deliver_up / user_interrupt→emit_only。

    这是新 Disposition 模型相对老 4 子类的核心新增语义：消费侧按 action 二选一，
    不再 isinstance 分发。老断言 isinstance(outcome, Cancelled) 只验了类型，无法
    区分「树级取消不上投」vs「局部取消上投」，本组补齐该缺口。
    """

    def test_hook_blocked_is_deliver_up(self) -> None:
        """hook_blocked（局部取消）→ action=deliver_up（上投 cancelled notice 通知父）。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "hook_blocked"})
        d = classify_result(r, EPOCH)
        assert d.action == "deliver_up"
        assert d.reason == "hook_blocked"
        assert d.tree_wide is False

    def test_user_interrupt_is_emit_only(self) -> None:
        """user_interrupt（树级取消）→ action=emit_only（父也被砍，不上投）。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "user_interrupt"})
        d = classify_result(r, EPOCH)
        assert d.action == "emit_only"
        assert d.reason == "user_interrupt"
        assert d.tree_wide is True

    def test_action_derived_from_tree_wide(self) -> None:
        """action 严格由 tree_wide 推导：tree_wide=True→emit_only，False→deliver_up。

        覆盖全部 4 个 cancel_reason 的 (reason, tree_wide, action) 三元组，确保 action
        与 tree_wide 一一对应，无歧义。
        """
        cases = {
            "user_interrupt": (True, "emit_only"),
            "parent_cascade": (True, "emit_only"),
            "hook_blocked": (False, "deliver_up"),
            "watchdog": (True, "emit_only"),
        }
        for reason, (tree_wide, action) in cases.items():
            r = _result(status="cancelled", metadata={"cancel_reason": reason})
            d = classify_result(r, EPOCH)
            assert d.tree_wide is tree_wide, f"reason={reason}"
            assert d.action == action, f"reason={reason}"


# ---------------------------------------------------------------------------
# classify_result：status=failed（DoD reason 映射 #3）
# ---------------------------------------------------------------------------


class TestClassifyFailed:
    @pytest.mark.parametrize(
        "status_code,expected_reason",
        [
            (401, "llm.auth"),
            (403, "llm.auth"),
            (429, "llm.rate_limit"),
            (400, "llm.protocol_400"),
            (500, "llm.capability"),
            (502, "llm.capability"),
            (503, "llm.capability"),
            (418, "llm.capability"),  # 其他 4xx 也归 capability（非鉴权/限流/协议）
        ],
    )
    def test_failed_provider_error_by_status_code(
        self, status_code: int, expected_reason: Reason
    ) -> None:
        r = _result(status="failed", error=_provider(status_code=status_code))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", expected_reason)

    def test_failed_provider_error_no_status_code_is_capability(self) -> None:
        """无 status_code（如 stream 解析错误）→ 默认 llm.capability。"""
        r = _result(status="failed", error=_provider(model="m1", raw_keys=["x"]))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "llm.capability")

    def test_failed_tool_error_is_tool_execution(self) -> None:
        r = _result(status="failed", error=ToolError("boom"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "tool.execution")

    def test_failed_error_none_is_internal_bug(self) -> None:
        """status=failed 但 error=None → 兜底 internal.bug。"""
        r = _result(status="failed", error=None)
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "internal.bug")

    def test_failed_other_agent_error_is_internal_bug(self) -> None:
        """其余 AgentError 子类（ApprovalRejected/ConfigError 等）→ internal.bug。"""
        r = _result(status="failed", error=AgentError("some error"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "internal.bug")

    def test_failed_max_turns_in_failed_status_is_max_turns(self) -> None:
        """runner 现状：max_turns 打满走 status=failed + MaxTurnsExceededError → max_turns。"""
        r = _result(status="failed", error=MaxTurnsExceededError("max turns"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "max_turns")

    def test_failed_bool_status_code_is_capability(self) -> None:
        """bool 是 int 子类；status_code=True（=1）不应被当 401/403/429/400 → capability。"""
        # 构造 details 写 status_code=True 的异常 ProviderError（防 dataclass 校验）
        err = ProviderError("bool", details={"status_code": True})
        r = _result(status="failed", error=err)
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "llm.capability")

    def test_failed_non_int_status_code_is_capability(self) -> None:
        """status_code 非 int（如 "429" 字符串）→ 不命中映射 → capability。"""
        err = ProviderError("str code", details={"status_code": "429"})
        r = _result(status="failed", error=err)
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "llm.capability")


# ---------------------------------------------------------------------------
# classify_result：兜底 + 幂等（DoD #4 + 失败路径表）
# ---------------------------------------------------------------------------


class TestClassifyFallbackAndIdempotent:
    def test_invalid_status_falls_back_internal_bug(self) -> None:
        """未知 status（理论不会出现）→ 兜底 internal.bug，不抛异常。"""
        r = _result(status=cast(ResultStatus, "running"))
        d = classify_result(r, EPOCH)
        assert d == Disposition("deliver_up", "internal.bug")

    def test_classify_never_raises_on_malformed_result(self) -> None:
        """classify_result 外层 try 兜底：即便 status 访问异常也返回 internal.bug，不抛。

        用一个 status 访问会抛异常的坏对象，验证 classify_result 的外层 try/except
        捕获后兜底 Disposition("deliver_up", "internal.bug")。
        """

        class _Raiser:
            # 模拟 .status 访问抛异常的坏对象
            @property
            def status(self) -> ResultStatus:
                raise RuntimeError("boom")

        bad = cast(Result, _Raiser())
        d = classify_result(bad, EPOCH)
        assert d == Disposition("deliver_up", "internal.bug")

    def test_classify_idempotent(self) -> None:
        """同输入两次调用返回相等 Disposition（纯函数幂等）。"""
        cases = [
            _result(status="completed", final_message=Message(role="assistant", content="x")),
            _result(status="cancelled", metadata={"cancel_reason": "hook_blocked"}),
            _result(status="failed", error=_provider(status_code=429)),
            _result(status="failed", error=None),
        ]
        for r in cases:
            d1 = classify_result(r, EPOCH)
            d2 = classify_result(r, EPOCH)
            assert d1 == d2

    def test_epoch_does_not_change_recommendation(self) -> None:
        """current_tree_epoch 仅日志用，不同 epoch 推荐值一致。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "user_interrupt"})
        for epoch in (0, 1, 99):
            d = classify_result(r, epoch)
            assert d == Disposition("emit_only", "user_interrupt", tree_wide=True)


# ---------------------------------------------------------------------------
# build_mail 助手（DoD #5）
# ---------------------------------------------------------------------------


class TestBuildMail:
    """build_mail 按 disposition.reason 构造 child_result Mail。

    单一入口（替代原 deliver_up / deliver_failure_up / deliver_partial_up /
    deliver_cancelled_up 4 函数），消费侧不再 isinstance 分发到不同助手。

    core.mail.Mail（task-4，模块 C）当前已合并存在，故 build_mail 运行期可用，
    无需 pytest.skip；若 task-4 尚未合并（core.mail.Mail 不存在），build_mail 会
    在运行期 ImportError，那时需 pytest.skip 并注释「core.mail 由 task-4 提供，
    未合并前 build_mail 运行期不可用」。
    """

    def test_build_mail_completed_uses_final_message(self) -> None:
        """completed → kind=child_result，payload=final_message（透传）。"""
        msg = Message(role="assistant", content="child done")
        result = _result(status="completed", final_message=msg)
        d = _disp(action="deliver_up", reason="completed")
        mail = build_mail(
            d,
            result=result,
            sender="child-1",
            recipient_agent_id="parent-0",
            task_id="task-1",
            epoch=3,
        )
        assert mail.kind == "child_result"
        assert mail.sender == "child-1"
        assert mail.recipient_agent_id == "parent-0"
        assert mail.task_id == "task-1"
        assert mail.epoch == 3
        assert mail.payload.content == msg.content
        assert mail.payload.metadata["turn_count"] == 0

    def test_build_mail_completed_none_final_message_fallback(self) -> None:
        """completed + final_message=None → 退化为空 assistant 消息（payload 非 None）。"""
        result = _result(status="completed", final_message=None)
        d = _disp(action="deliver_up", reason="completed")
        mail = build_mail(
            d,
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=0,
        )
        assert mail.payload.role == "assistant"
        assert mail.payload.content == ""

    def test_build_mail_completed_carries_usage_and_turn_count(self) -> None:
        """completed child 上投携带 Result usage/turn_count，供 workflow 报告消费。"""
        usage = {"input_tokens": 7, "output_tokens": 3}
        result = _result(
            status="completed",
            final_message=Message(role="assistant", content="done"),
            turn_count=2,
            metadata={"usage": usage},
        )
        mail = build_mail(
            _disp(action="deliver_up", reason="completed"),
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=0,
        )

        assert mail.payload.metadata["usage"] == usage
        assert mail.payload.metadata["turn_count"] == 2

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_build_mail_preserves_zero_turn_count(self, status: str) -> None:
        """首轮 LLM 前终止的 child 继续上投 turn_count=0。"""
        error = AgentError("boom") if status == "failed" else None
        metadata = {"cancel_reason": "watchdog"} if status == "cancelled" else {}
        result = _result(
            status=cast(ResultStatus, status),
            error=error,
            metadata=metadata,
            turn_count=0,
        )
        disposition = classify_result(result, EPOCH)
        if disposition.action == "emit_only":
            disposition = Disposition("deliver_up", disposition.reason)
        mail = build_mail(
            disposition,
            result=result,
            sender="child",
            recipient_agent_id="parent",
            task_id="task-zero",
            epoch=EPOCH,
        )

        assert mail.payload.metadata["turn_count"] == 0

    def test_build_mail_max_turns_attaches_exhausted_reason(self) -> None:
        """max_turns → payload.metadata 附带 exhausted_reason 标记（替代老 exhausted_budget）。"""
        msg = Message(role="assistant", content="partial")
        result = _result(status="failed", error=MaxTurnsExceededError("max"), final_message=msg)
        d = _disp(action="deliver_up", reason="max_turns")
        mail = build_mail(
            d,
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=1,
        )
        assert mail.kind == "child_result"
        assert mail.payload.metadata["exhausted_reason"] == "max_turns"

    def test_build_mail_failed_carries_reason_in_payload(self) -> None:
        """llm.* / tool.execution / internal.bug → payload 带 child_error_reason metadata。

        payload metadata 键名为 child_error_reason（替代老 child_error_class）；
        content 含 reason 标签。
        """
        result = _result(status="failed", error=_provider(status_code=429))
        d = _disp(action="deliver_up", reason="llm.rate_limit")
        mail = build_mail(
            d,
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=2,
        )
        assert mail.kind == "child_result"
        assert mail.payload.metadata["child_error_reason"] == "llm.rate_limit"
        assert "llm.rate_limit" in (mail.payload.content or "")

    def test_build_mail_local_cancel_carries_cancel_reason(self) -> None:
        """局部 cancel（hook_blocked, action=deliver_up）→ payload 带 child_cancel_reason。

        树级 cancel（action=emit_only）消费侧不调 build_mail，故本用例只覆盖局部
        cancel 上投 cancelled notice 的路径。
        """
        result = _result(status="cancelled", metadata={"cancel_reason": "hook_blocked"})
        d = _disp(action="deliver_up", reason="hook_blocked", tree_wide=False)
        mail = build_mail(
            d,
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=0,
        )
        assert mail.kind == "child_result"
        assert mail.payload.metadata["child_cancel_reason"] == "hook_blocked"
        assert "hook_blocked" in (mail.payload.content or "")

    def test_build_mail_and_classify_result_compose(self) -> None:
        """端到端组合：classify_result 产出 Disposition → 喂 build_mail 产出 Mail。

        验证 classify_result 与 build_mail 的契约衔接：classify 产数据，build_mail
        吃数据，无需中间 isinstance 分发。
        """
        msg = Message(role="assistant", content="done")
        result = _result(status="completed", final_message=msg)
        d = classify_result(result, EPOCH)
        mail = build_mail(
            d,
            result=result,
            sender="child-1",
            recipient_agent_id="parent-0",
            task_id="task-1",
            epoch=3,
        )
        assert mail.kind == "child_result"
        assert mail.payload.content == msg.content
        assert mail.payload.metadata["turn_count"] == 0

    def test_build_mail_callable_signatures(self) -> None:
        """classify_result + build_mail 都是可调用对象（签名就绪）。"""
        assert callable(classify_result)
        assert callable(build_mail)
