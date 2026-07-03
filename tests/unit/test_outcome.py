"""unit：Outcome 分类器（task-2，模块 D）全覆盖测试。

覆盖 classify 纯函数对每个 Result.status × metadata 组合的输出断言：
- completed → Completed / completed+MaxTurnsExceededError → Exhausted(max_turns)
- cancelled + cancel_reason∈{user_interrupt,hook_blocked,parent_cascade,watchdog}
  → Cancelled(reason) + tree_wide 推荐值；未知/空 reason → Cancelled(user_interrupt)
- failed + ProviderError → Failed(llm.*) 按 status_code 细分；
  failed + ToolError → Failed(tool.execution)；failed + error=None → Failed(internal.bug)
- 兜底：非法 status、空 metadata、classify 永不抛异常
- 幂等：同输入同输出
- deliver 助手：Completed/Failed/Exhausted 构造 Mail；Cancelled raise NotImplementedError

事实源：
- docs/spec/agent-tree-v0.1/04-data-and-state.md（Outcome 字段定义）
- docs/research/claude错误分类器.md（query.ts reason 表 → Outcome 映射）
- dev-pipeline/tasks/agent-tree-outcome-classifier/README.md（reason 映射表 + tree_wide 推荐值）
"""

from __future__ import annotations

from dataclasses import is_dataclass
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
    Cancelled,
    Completed,
    Exhausted,
    Failed,
    Outcome,
    classify,
    deliver_failure_up,
    deliver_partial_up,
    deliver_up,
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


# ---------------------------------------------------------------------------
# 契约：Outcome 类型定义（DoD #1）
# ---------------------------------------------------------------------------


class TestOutcomeContract:
    """4 子类 frozen dataclass + 字面量枚举字段类型与 spec 一致。"""

    def test_completed_is_frozen_dataclass_no_fields(self) -> None:
        c = Completed()
        assert is_dataclass(Completed)
        # frozen：不可赋值（无字段，验证实例化 + isinstance）
        assert isinstance(c, Outcome)
        assert isinstance(c, Completed)

    def test_exhausted_carries_budget(self) -> None:
        for budget in ("max_turns", "token_limit", "cost_limit"):
            e = Exhausted(budget=cast(Any, budget))  # type: ignore[arg-type]
            assert e.budget == budget
            assert isinstance(e, Outcome)

    def test_cancelled_carries_reason_and_tree_wide(self) -> None:
        c = Cancelled(reason="user_interrupt", tree_wide=True)
        assert c.reason == "user_interrupt"
        assert c.tree_wide is True
        assert isinstance(c, Outcome)

    def test_failed_carries_error_class(self) -> None:
        for ec in (
            "llm.capability",
            "llm.protocol_400",
            "llm.auth",
            "llm.rate_limit",
            "tool.execution",
            "internal.bug",
        ):
            f = Failed(error_class=cast(Any, ec))  # type: ignore[arg-type]
            assert f.error_class == ec
            assert isinstance(f, Outcome)

    def test_outcome_frozen_immutable(self) -> None:
        """frozen=True：字段不可变（防止下游误改 Outcome）。"""
        e = Exhausted("max_turns")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            e.budget = "token_limit"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# classify：status=completed（DoD reason 映射 #1）
# ---------------------------------------------------------------------------


class TestClassifyCompleted:
    def test_completed_plain(self) -> None:
        r = _result(status="completed", final_message=Message(role="assistant", content="done"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Completed)

    def test_completed_with_usage_metadata(self) -> None:
        r = _result(status="completed", metadata={"usage": {"input": 10}})
        assert isinstance(classify(r, EPOCH), Completed)

    def test_completed_empty_metadata(self) -> None:
        r = _result(status="completed", metadata={})
        assert isinstance(classify(r, EPOCH), Completed)

    def test_completed_with_max_turns_error_is_exhausted(self) -> None:
        """completed + error=MaxTurnsExceededError → Exhausted(max_turns)。"""
        r = _result(status="completed", error=MaxTurnsExceededError("max turns"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Exhausted)
        assert outcome.budget == "max_turns"

    def test_completed_no_max_turns_info_is_completed(self) -> None:
        """completed 无 error、metadata 无 max_turns 标记 → 默认 Completed（无法判定打满）。"""
        r = _result(status="completed", turn_count=50, metadata={})
        assert isinstance(classify(r, EPOCH), Completed)


# ---------------------------------------------------------------------------
# classify：status=cancelled（DoD reason 映射 #2 + tree_wide 推荐值）
# ---------------------------------------------------------------------------


class TestClassifyCancelled:
    @pytest.mark.parametrize(
        "reason,expected_tree_wide",
        [
            ("user_interrupt", True),
            ("parent_cascade", True),
            ("hook_blocked", False),
            ("watchdog", True),
        ],
    )
    def test_cancelled_known_reasons(self, reason: str, expected_tree_wide: bool) -> None:
        r = _result(
            status="cancelled",
            metadata={
                "cancelled_at_turn": 3,
                "cancelled_tool_call_id": None,
                "cancel_reason": reason,
            },
        )
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == reason
        assert outcome.tree_wide is expected_tree_wide

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
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == "user_interrupt"
        assert outcome.tree_wide is True

    def test_cancelled_unknown_reason_fallbacks_user_interrupt(self) -> None:
        """未知 cancel_reason → 兜底 user_interrupt（不抛异常）。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "weird_reason"})
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == "user_interrupt"
        assert outcome.tree_wide is True

    def test_cancelled_missing_cancel_reason(self) -> None:
        """空 metadata → 兜底 user_interrupt。"""
        r = _result(status="cancelled", metadata={})
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == "user_interrupt"
        assert outcome.tree_wide is True

    def test_cancelled_empty_cancel_reason_string(self) -> None:
        """cancel_reason=""（空串）→ 兜底 user_interrupt。"""
        r = _result(status="cancelled", metadata={"cancel_reason": ""})
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == "user_interrupt"

    def test_cancelled_non_string_reason(self) -> None:
        """cancel_reason 非 str（异常写入）→ 兜底 user_interrupt，不抛异常。"""
        r = _result(status="cancelled", metadata={"cancel_reason": 123})
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Cancelled)
        assert outcome.reason == "user_interrupt"

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
            outcome = classify(r, EPOCH)
            assert isinstance(outcome, Cancelled)
            assert outcome.tree_wide is expected, f"reason={reason}"


# ---------------------------------------------------------------------------
# classify：status=failed（DoD reason 映射 #3）
# ---------------------------------------------------------------------------


class TestClassifyFailed:
    @pytest.mark.parametrize(
        "status_code,expected_class",
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
        self, status_code: int, expected_class: str
    ) -> None:
        r = _result(status="failed", error=_provider(status_code=status_code))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == expected_class

    def test_failed_provider_error_no_status_code_is_capability(self) -> None:
        """无 status_code（如 stream 解析错误）→ 默认 llm.capability。"""
        r = _result(status="failed", error=_provider(model="m1", raw_keys=["x"]))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "llm.capability"

    def test_failed_tool_error_is_tool_execution(self) -> None:
        r = _result(status="failed", error=ToolError("boom"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "tool.execution"

    def test_failed_error_none_is_internal_bug(self) -> None:
        """status=failed 但 error=None → 兜底 internal.bug。"""
        r = _result(status="failed", error=None)
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "internal.bug"

    def test_failed_other_agent_error_is_internal_bug(self) -> None:
        """其余 AgentError 子类（ApprovalRejected/ConfigError 等）→ internal.bug。"""
        r = _result(status="failed", error=AgentError("some error"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "internal.bug"

    def test_failed_max_turns_in_failed_status_is_exhausted(self) -> None:
        """runner 现状：max_turns 打满走 status=failed + MaxTurnsExceededError → Exhausted。"""
        r = _result(status="failed", error=MaxTurnsExceededError("max turns"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Exhausted)
        assert outcome.budget == "max_turns"

    def test_failed_bool_status_code_is_capability(self) -> None:
        """bool 是 int 子类；status_code=True（=1）不应被当 401/403/429/400 → capability。"""
        # 构造 details 写 status_code=True 的异常 ProviderError（防 dataclass 校验）
        err = ProviderError("bool", details={"status_code": True})
        r = _result(status="failed", error=err)
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "llm.capability"

    def test_failed_non_int_status_code_is_capability(self) -> None:
        """status_code 非 int（如 "429" 字符串）→ 不命中映射 → capability。"""
        err = ProviderError("str code", details={"status_code": "429"})
        r = _result(status="failed", error=err)
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "llm.capability"


# ---------------------------------------------------------------------------
# classify：兜底 + 幂等（DoD #4 + 失败路径表）
# ---------------------------------------------------------------------------


class TestClassifyFallbackAndIdempotent:
    def test_invalid_status_falls_back_internal_bug(self) -> None:
        """未知 status（理论不会出现）→ 兜底 internal.bug，不抛异常。"""
        r = _result(status=cast(ResultStatus, "running"))
        outcome = classify(r, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "internal.bug"

    def test_classify_never_raises_on_malformed_result(self) -> None:
        """classify 外层 try 兜底：即便 status 访问异常也返回 internal.bug，不抛。

        用 object.__setattr__ 把 status 替换成会抛异常的 property，验证 classify
        的外层 try/except 捕获后兜底 Failed(internal.bug)。
        """

        class _Raiser:
            # 模拟 .status 访问抛异常的坏对象
            @property
            def status(self) -> ResultStatus:
                raise RuntimeError("boom")

        bad = cast(Result, _Raiser())
        outcome = classify(bad, EPOCH)
        assert isinstance(outcome, Failed)
        assert outcome.error_class == "internal.bug"

    def test_classify_idempotent(self) -> None:
        """同输入同输出（纯函数幂等）。"""
        cases = [
            _result(status="completed", final_message=Message(role="assistant", content="x")),
            _result(status="cancelled", metadata={"cancel_reason": "hook_blocked"}),
            _result(status="failed", error=_provider(status_code=429)),
            _result(status="failed", error=None),
        ]
        for r in cases:
            o1 = classify(r, EPOCH)
            o2 = classify(r, EPOCH)
            assert type(o1) is type(o2)
            assert o1 == o2

    def test_epoch_does_not_change_recommendation(self) -> None:
        """current_tree_epoch 仅日志用，不同 epoch 推荐值一致。"""
        r = _result(status="cancelled", metadata={"cancel_reason": "user_interrupt"})
        for epoch in (0, 1, 99):
            outcome = classify(r, epoch)
            assert isinstance(outcome, Cancelled)
            assert outcome.tree_wide is True
            assert outcome.reason == "user_interrupt"


# ---------------------------------------------------------------------------
# deliver 助手（DoD #5）
# ---------------------------------------------------------------------------


class TestDeliverHelpers:
    """Completed/Failed/Exhausted 构造 Mail；Cancelled raise NotImplementedError。

    Mail 类型在 task-4 定义（core.mail），当前 task 阶段 core.mail 尚不存在，
    故 deliver_* 在运行期会抛 ImportError（Mail 延迟 import）。这里断言：
    1. Cancelled 分支显式 NotImplementedError（task-4 钩子）。
    2. Completed/Failed/Exhausted 的构造路径逻辑正确（用 monkeypatch 桩掉 Mail）。
    """

    def test_deliver_cancelled_raises_not_implemented(self) -> None:
        """Cancelled deliver 留 task-4（tree_wide 最终判定在消费侧）。"""
        outcome = Cancelled(reason="user_interrupt", tree_wide=True)
        with pytest.raises(NotImplementedError, match="task-4"):
            deliver_up  # 引用保证 import 可达
            from core.outcome import deliver_cancelled_up

            deliver_cancelled_up(
                outcome,
                sender="child",
                recipient_agent_id="parent",
                task_id="t1",
                epoch=1,
            )

    def test_deliver_up_completed_builds_child_result_mail(self, monkeypatch) -> None:
        """Completed 上投：kind=child_result，payload=final_message。"""
        from core.message import Message

        captured: dict[str, Any] = {}

        class FakeMail:
            def __init__(self, **kw: Any) -> None:
                captured.update(kw)

        # 桩掉 core.mail.Mail（task-4 尚未实现）
        import sys
        import types

        fake_mod = types.ModuleType("core.mail")
        fake_mod.Mail = FakeMail  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core.mail", fake_mod)

        msg = Message(role="assistant", content="child done")
        result = _result(status="completed", final_message=msg)
        mail = deliver_up(
            Completed(),
            result=result,
            sender="child-1",
            recipient_agent_id="parent-0",
            task_id="task-1",
            epoch=3,
        )
        assert captured["kind"] == "child_result"
        assert captured["sender"] == "child-1"
        assert captured["recipient_agent_id"] == "parent-0"
        assert captured["task_id"] == "task-1"
        assert captured["epoch"] == 3
        assert captured["payload"] is msg
        assert isinstance(mail, FakeMail)

    def test_deliver_up_completed_none_final_message_fallback(self, monkeypatch) -> None:
        """final_message=None 时退化为空 assistant 消息（Mail.payload 非 None）。"""
        import sys
        import types

        class FakeMail:
            def __init__(self, **kw: Any) -> None:
                self.kw = kw

        fake_mod = types.ModuleType("core.mail")
        fake_mod.Mail = FakeMail  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core.mail", fake_mod)

        result = _result(status="completed", final_message=None)
        mail = deliver_up(
            Completed(),
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=0,
        )
        payload = mail.kw["payload"]  # type: ignore[attr-defined]
        assert payload.role == "assistant"
        assert payload.content == ""

    def test_deliver_failure_up_builds_child_result_mail(self, monkeypatch) -> None:
        """Failed 上投：payload 带 child_error_class metadata。"""
        import sys
        import types

        captured: dict[str, Any] = {}

        class FakeMail:
            def __init__(self, **kw: Any) -> None:
                captured.update(kw)

        fake_mod = types.ModuleType("core.mail")
        fake_mod.Mail = FakeMail  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core.mail", fake_mod)

        mail = deliver_failure_up(
            Failed("llm.rate_limit"),
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=2,
        )
        assert captured["kind"] == "child_result"
        assert captured["payload"].metadata["child_error_class"] == "llm.rate_limit"
        assert "llm.rate_limit" in (captured["payload"].content or "")
        assert isinstance(mail, FakeMail)

    def test_deliver_partial_up_attaches_budget(self, monkeypatch) -> None:
        """Exhausted 上投：payload metadata 带 exhausted_budget。"""
        import sys
        import types

        captured: dict[str, Any] = {}

        class FakeMail:
            def __init__(self, **kw: Any) -> None:
                captured.update(kw)

        fake_mod = types.ModuleType("core.mail")
        fake_mod.Mail = FakeMail  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core.mail", fake_mod)

        msg = Message(role="assistant", content="partial")
        result = _result(status="failed", error=MaxTurnsExceededError("max"), final_message=msg)
        deliver_partial_up(
            Exhausted("max_turns"),
            result=result,
            sender="c",
            recipient_agent_id="p",
            task_id="t",
            epoch=1,
        )
        assert captured["kind"] == "child_result"
        assert captured["payload"].metadata["exhausted_budget"] == "max_turns"

    def test_deliver_helpers_signatures_callable(self) -> None:
        """三个 deliver 助手 + classify 都是可调用对象（签名就绪）。"""
        assert callable(classify)
        assert callable(deliver_up)
        assert callable(deliver_failure_up)
        assert callable(deliver_partial_up)
