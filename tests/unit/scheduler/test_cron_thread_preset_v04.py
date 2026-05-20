"""v0.4 cron-thread-preset 专项测试。

覆盖 cron-thread-preset-v0.4 引入的新字段和行为：

1. Domain: ScheduleDelivery.target + ScheduledTask.preset_id
2. Store: _task_to_dict / _dict_to_task 对新字段的序列化/反序列化
3. schedule_tool: _do_create 填 delivery.target / preset_id
4. provider_factory: apply_preset / build_provider
5. WebDeliverySink: broadcast payload 含 delivery_target
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.contracts import ToolContext
from scheduler.domain import (
    DeliveryChannel,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store, _dict_to_task, _task_to_dict
from tools.schedule_tool import build_schedule_tool

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_trigger() -> ScheduleTrigger:
    return ScheduleTrigger(trigger_type=TriggerType.CRON, expr="*/5 * * * *", timezone="UTC")


def _make_task(
    *,
    delivery: ScheduleDelivery | None = None,
    preset_id: str = "",
) -> ScheduledTask:
    return ScheduledTask(
        task_id="t1",
        name="test-task",
        enabled=True,
        state=TaskState.SCHEDULED,
        origin=TaskOrigin.TOOL,
        trigger=_make_trigger(),
        policy=TaskExecutionPolicy(),
        target=TaskTarget(agent_name="default", input_text="ping"),
        next_run_at=None,
        last_run_at=None,
        created_by="user-a",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
        delivery=delivery,
        preset_id=preset_id,
    )


def _ctx(session_id: str = "sess-test") -> ToolContext:
    return ToolContext(run_id="run-test", session_id=session_id, turn=1, call_id="call-1")


# ---------------------------------------------------------------------------
# Group 1: Domain
# ---------------------------------------------------------------------------


class TestDomainScheduleDeliveryTarget:
    """ScheduleDelivery.target 字段校验。"""

    def test_schedule_delivery_with_target(self) -> None:
        """构造 ScheduleDelivery(channel=WEB, target='thread:thread-abc123456789') 成功。"""
        d = ScheduleDelivery(channel=DeliveryChannel.WEB, target="thread:thread-abc123456789")
        assert d.channel is DeliveryChannel.WEB
        assert d.target == "thread:thread-abc123456789"

    def test_schedule_delivery_target_none_default(self) -> None:
        """构造 ScheduleDelivery(channel=WEB) 时 target 默认为 None。"""
        d = ScheduleDelivery(channel=DeliveryChannel.WEB)
        assert d.target is None

    def test_schedule_delivery_target_type_error(self) -> None:
        """target=123 应当抛 TypeError。"""
        with pytest.raises(TypeError, match="target"):
            ScheduleDelivery(channel=DeliveryChannel.WEB, target=123)  # type: ignore[arg-type]


class TestDomainScheduledTaskPresetId:
    """ScheduledTask.preset_id 字段校验。"""

    def test_scheduled_task_preset_id_default_empty(self) -> None:
        """默认 preset_id='' 正常工作。"""
        task = _make_task(preset_id="")
        assert task.preset_id == ""

    def test_scheduled_task_preset_id_custom(self) -> None:
        """自定义 preset_id='claude-sonnet' 正常工作。"""
        task = _make_task(preset_id="claude-sonnet")
        assert task.preset_id == "claude-sonnet"

    def test_scheduled_task_preset_id_type_error(self) -> None:
        """preset_id=123 应当抛 TypeError。"""
        with pytest.raises(TypeError, match="preset_id"):
            _make_task(preset_id=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Group 2: Store serialization
# ---------------------------------------------------------------------------


class TestStoreSerializationV04:
    """_task_to_dict / _dict_to_task 对 v0.4 新字段的序列化/反序列化。"""

    def test_task_round_trip_with_delivery_target(self) -> None:
        """带 delivery.target 的 task 序列化后能完整还原。"""
        delivery = ScheduleDelivery(
            channel=DeliveryChannel.WEB, target="thread:thread-abc123456789"
        )
        task = _make_task(delivery=delivery)
        d = _task_to_dict(task)
        restored = _dict_to_task(d)
        assert restored.delivery is not None
        assert restored.delivery.target == "thread:thread-abc123456789"
        assert restored.delivery.channel is DeliveryChannel.WEB

    def test_task_round_trip_with_preset_id(self) -> None:
        """带 preset_id 的 task 序列化后能完整还原。"""
        task = _make_task(preset_id="claude-sonnet")
        d = _task_to_dict(task)
        restored = _dict_to_task(d)
        assert restored.preset_id == "claude-sonnet"

    def test_dict_to_task_missing_target_defaults_none(self) -> None:
        """dict 中 delivery 没有 'target' 字段时默认为 None。"""
        task = _make_task(delivery=ScheduleDelivery(channel=DeliveryChannel.WEB))
        d = _task_to_dict(task)
        # 手动移除 target 模拟 v0.3 旧数据
        assert "target" not in d["delivery"] or d["delivery"].get("target") is None
        # 不管是缺失还是 None，_dict_to_task 都该正确处理
        d["delivery"].pop("target", None)
        restored = _dict_to_task(d)
        assert restored.delivery is not None
        assert restored.delivery.target is None

    def test_dict_to_task_missing_preset_id_defaults_empty(self) -> None:
        """dict 中没有 'preset_id' 字段时默认为空串。"""
        task = _make_task(preset_id="")
        d = _task_to_dict(task)
        # 手动移除 preset_id 模拟 v0.3 旧数据
        del d["preset_id"]
        restored = _dict_to_task(d)
        assert restored.preset_id == ""


# ---------------------------------------------------------------------------
# Group 3: schedule_tool delivery.target derivation
# ---------------------------------------------------------------------------


class TestScheduleToolDeliveryTarget:
    """schedule_tool._do_create 对 delivery.target / preset_id 的填充逻辑。"""

    async def test_do_create_fills_delivery_target_from_thread_session(
        self, tmp_path: Path
    ) -> None:
        """ctx.session_id 匹配 thread-<12hex> 时，task.delivery.target 为
        'thread:<session_id>'。"""
        store = Store(home_dir=tmp_path / "cron")
        tool = build_schedule_tool(store, default_delivery_channel="web")
        # thread_id 格式：thread-<12位十六进制>
        session_id = "thread-abcdef012345"
        r = await tool.execute(
            {
                "action": "create",
                "name": "target-test",
                "schedule": "every 10s",
                "input": "hi",
            },
            _ctx(session_id=session_id),
        )
        assert r.ok is True
        task = store.get_task(r.data["task_id"])
        assert task is not None
        assert task.delivery is not None
        assert task.delivery.target == f"thread:{session_id}"

    async def test_do_create_no_target_when_cli_session(self, tmp_path: Path) -> None:
        """ctx.session_id 不匹配 thread-<12hex> 时，delivery.target 为 None。"""
        store = Store(home_dir=tmp_path / "cron")
        tool = build_schedule_tool(store, default_delivery_channel="web")
        r = await tool.execute(
            {
                "action": "create",
                "name": "cli-test",
                "schedule": "every 10s",
                "input": "hi",
            },
            _ctx(session_id="some-random-id"),
        )
        assert r.ok is True
        task = store.get_task(r.data["task_id"])
        assert task is not None
        assert task.delivery is not None
        assert task.delivery.target is None

    async def test_do_create_fills_preset_id_from_default(self, tmp_path: Path) -> None:
        """default_preset_id='claude-sonnet' 时，创建的 task.preset_id 填为该值。"""
        store = Store(home_dir=tmp_path / "cron")
        tool = build_schedule_tool(store, default_preset_id="claude-sonnet")
        r = await tool.execute(
            {
                "action": "create",
                "name": "preset-test",
                "schedule": "every 10s",
                "input": "hi",
            },
            _ctx(),
        )
        assert r.ok is True
        task = store.get_task(r.data["task_id"])
        assert task is not None
        assert task.preset_id == "claude-sonnet"


# ---------------------------------------------------------------------------
# Group 4: provider_factory
# ---------------------------------------------------------------------------


class TestProviderFactory:
    """executors.llm.provider_factory 的 apply_preset / build_provider。"""

    def test_apply_preset_overrides_model_name(self) -> None:
        """apply_preset 把 preset.model 写入 config.model.name。"""
        from config_loader.models import Config, LLMPresetConfig, ModelConfig

        cfg = Config(
            model=ModelConfig(
                name="old-model",
                base_url="http://127.0.0.1:1234/v1",
                api_key="",
            )
        )
        preset = LLMPresetConfig(
            id="test-preset",
            display_name="Test",
            base_url="http://127.0.0.1:5678/v1",
            model="gpt-4",
        )
        from executors.llm.provider_factory import apply_preset

        new_cfg = apply_preset(cfg, preset)
        assert new_cfg.model.name == "gpt-4"
        assert new_cfg.model.base_url == "http://127.0.0.1:5678/v1"
        # 原 cfg 不变
        assert cfg.model.name == "old-model"

    def test_build_provider_openai(self) -> None:
        """config 的 effective_provider 为 openai_compatible 时返回 OpenAIResponsesProvider。"""
        from config_loader.models import Config, ModelConfig
        from executors.llm.openai_responses import OpenAIResponsesProvider
        from executors.llm.provider_factory import build_provider

        cfg = Config(
            model=ModelConfig(
                name="gpt-4",
                base_url="http://127.0.0.1:1234/v1",
                api_key="",
            )
        )
        provider = build_provider(cfg)
        assert isinstance(provider, OpenAIResponsesProvider)

    def test_build_provider_anthropic(self) -> None:
        """config 的 effective_provider 为 anthropic 时返回 AnthropicMessagesProvider。"""
        from config_loader.models import Config, ModelConfig
        from executors.llm.anthropic_messages import AnthropicMessagesProvider
        from executors.llm.provider_factory import build_provider

        cfg = Config(
            model=ModelConfig(
                provider="anthropic",
                name="claude-sonnet-4-20250514",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-test-key",
            )
        )
        provider = build_provider(cfg)
        assert isinstance(provider, AnthropicMessagesProvider)


# ---------------------------------------------------------------------------
# Group 5: WebDeliverySink broadcast includes delivery_target
# ---------------------------------------------------------------------------


class TestWebDeliverySinkDeliveryTarget:
    """WebDeliverySink.deliver 广播 payload 含 delivery_target。"""

    async def test_web_delivery_sink_broadcast_includes_delivery_target(self) -> None:
        """task.delivery.target 非空时，broadcast payload 携带 delivery_target。"""
        from scheduler.domain import RunStatus
        from web.cron_delivery import WebDeliverySink

        # mock broker
        broker = MagicMock()
        broker.broadcast = AsyncMock()

        sink = WebDeliverySink(broker)

        delivery = ScheduleDelivery(
            channel=DeliveryChannel.WEB, target="thread:thread-abc123456789"
        )
        task = _make_task(delivery=delivery)
        run = ScheduledRun(
            run_id="r1",
            task_id="t1",
            status=RunStatus.COMPLETED,
            scheduled_for="2026-05-01T00:00:00Z",
            started_at="2026-05-01T00:00:01Z",
            finished_at="2026-05-01T00:00:10Z",
            session_id="s1",
            result_status="completed",
            final_message_excerpt="hello",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        await sink.deliver(task, run, "hello from cron")

        # broker.broadcast 被调用一次
        broker.broadcast.assert_called_once()
        payload = broker.broadcast.call_args[0][0]
        assert payload["delivery_target"] == "thread:thread-abc123456789"
        assert payload["kind"] == "cron.run.completed"
        assert payload["task_id"] == "t1"
        assert payload["final_message"] == "hello from cron"
        # v0.5 web-cron-router 扩展字段
        assert payload["next_run_at"] == task.next_run_at
        assert payload["status"] == "completed"

    async def test_web_delivery_sink_broadcast_delivery_target_none(self) -> None:
        """task.delivery.target 为 None 时，broadcast payload 中 delivery_target 为 None。"""
        from scheduler.domain import RunStatus
        from web.cron_delivery import WebDeliverySink

        broker = MagicMock()
        broker.broadcast = AsyncMock()

        sink = WebDeliverySink(broker)

        delivery = ScheduleDelivery(channel=DeliveryChannel.WEB, target=None)
        task = _make_task(delivery=delivery)
        run = ScheduledRun(
            run_id="r2",
            task_id="t1",
            status=RunStatus.COMPLETED,
            scheduled_for="2026-05-01T00:00:00Z",
            started_at="2026-05-01T00:00:01Z",
            finished_at="2026-05-01T00:00:10Z",
            session_id="s2",
            result_status="completed",
            final_message_excerpt="world",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
        await sink.deliver(task, run, "world")

        payload = broker.broadcast.call_args[0][0]
        assert payload["delivery_target"] is None
        # v0.5 web-cron-router 扩展字段
        assert payload["next_run_at"] == task.next_run_at
        assert payload["status"] == "completed"
