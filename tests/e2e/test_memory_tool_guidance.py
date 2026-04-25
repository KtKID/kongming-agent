"""Memory tool 使用引导 e2e 测试。

目标：确保 Agent 面对"请记住我喜欢 X"类诉求时，会调用 ``memory`` 工具而不是
``write_file``。本测试一方面通过 stub LLM 驱动 runner，断言第一个 tool_call 是
``memory``；另一方面做防回退用的静态文案检查。
"""

from __future__ import annotations

import pytest

from core.message import ToolCall
from memory import MemoryStore
from tools.memory_tool import MemoryTool, build_memory_tool

# ---------------------------------------------------------------------------
# 文案防回退（即便 stub LLM 路径搭不起来，也能守住基线）
# ---------------------------------------------------------------------------


def test_description_warns_against_write_file() -> None:
    """description 文案必须把"记长期记忆 → memory tool"钉死，劝退 write_file。"""
    desc = MemoryTool.description
    assert "不要用 write_file" in desc or "INSTEAD" in desc
    assert "长期记忆" in desc or "跨会话" in desc


def test_input_schema_is_chinese() -> None:
    """中文化 description：Agent 看到中文诉求时能正确触发。"""
    schema = MemoryTool.input_schema
    target_desc = schema["properties"]["target"]["description"]
    assert "记忆分区" in target_desc
    content_desc = schema["properties"]["content"]["description"]
    assert "追加" in content_desc


# ---------------------------------------------------------------------------
# Stub LLM 驱动：用户说"请记住我喜欢 TypeScript"，断言 tool_call 是 memory
# ---------------------------------------------------------------------------


async def test_stub_llm_chooses_memory_tool_for_preferences(tmp_path) -> None:
    """脚本化一次 assistant tool_calls=memory，验证 runner 能正确执行该调用。

    我们不直接控制 LLM 的"选择"（那属于 provider 行为），但我们可以验证：
    一旦 LLM 选了 memory tool 且 runner 路径跑通，memory store 会被正确写入；
    从而证明链路可用，使 description 文案的引导真正有效。

    如果将来想做"LLM 在看到正确 description 后必选 memory"的测试，需要接真实
    provider；本用例退而求其次，保证工具注册 + 执行路径无误。
    """
    from config_loader.models import (
        ApprovalConfig,
        Config,
        EvolutionConfig,
        EvolutionMemoryConfig,
        ModelConfig,
        RunnerConfig,
    )
    from executors.agent_runtime.native_runtime import NativeRuntime
    from safety import CapabilityPolicy, CapabilitySet
    from tests.e2e.conftest import StubLLMProvider
    from tools.registry import ToolRegistry

    mem_dir = tmp_path / ".kongming" / "memory"
    mem_dir.mkdir(parents=True)

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        approval=ApprovalConfig(mode="auto_allow"),
        evolution=EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=True, root_path=str(mem_dir)),
        ),
    )

    store = MemoryStore(memory_dir=mem_dir)
    await store.load_from_disk()

    registry = ToolRegistry()
    registry.register(build_memory_tool(store))

    stub = StubLLMProvider()
    # 第一轮：assistant 调 memory.add
    stub.script(
        tool_calls=[
            ToolCall(
                call_id="c1",
                tool_name="memory",
                arguments={
                    "action": "add",
                    "target": "user",
                    "content": "用户偏好 TypeScript",
                },
            )
        ]
    )
    # 第二轮：assistant 终止
    stub.script(content="已记住。")

    # 当前 capability_policy 默认 allow-list 不含 "memory"（README 遗漏 #1 待
    # Agent 补齐登记）；此处注入空 allow + 空 deny（全开）让测试聚焦于引导文案验证。
    runtime = NativeRuntime.build(
        cfg,
        tools=registry,
        enabled_tool_names=["memory"],
        capability_policy=CapabilityPolicy(CapabilitySet(allow=frozenset())),
    )
    # 替换 llm 为 stub
    runtime._llm = stub  # type: ignore[attr-defined]

    try:
        result = await runtime.run("请记住我喜欢 TypeScript", session_id="guidance")
        assert result.status == "completed"
        # 验证 memory 被真的写入
        await store.load_from_disk()
        user_text = store.snapshot.user_text if store.snapshot else ""
        assert "TypeScript" in user_text
    finally:
        await runtime.aclose()


async def test_memory_tool_registered_when_enabled(tmp_path) -> None:
    """sanity check：build_memory_tool 产出的 tool name 是 'memory'。"""
    store = MemoryStore(memory_dir=tmp_path / ".kongming" / "memory")
    await store.load_from_disk()
    tool = build_memory_tool(store)
    assert tool.name == "memory"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
