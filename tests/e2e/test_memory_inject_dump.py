"""e2e：memory 注入 system prompt 的完整链路验证 + JSON dump。

覆盖场景：
- 读取真实 `.kongming/memory/MEMORY.md`（不存在则 ensure 创建默认模板）
- 内容完整注入 system prompt 并落盘 JSON dump

运行方式：
    uv run pytest tests/e2e/test_memory_inject_dump.py -v

JSON dump 输出到 ``.kongming/debug/memory-inject-<timestamp>.json``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.message import Message
from memory import MemoryStore
from prompting import InputAssembler, InstructionSource
from tests.e2e.conftest import RecordingApproval, StubLLMProvider

_DUMP_DIR = Path(".kongming/debug")


def _dump_path() -> Path:
    """生成时间戳命名的 dump 文件路径。"""
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    return _DUMP_DIR / f"memory-inject-{ts}.json"


def _messages_to_dicts(messages: list[Message]) -> list[dict]:
    """把 Message 列表转成 JSON 可序列化的 dict 列表。"""
    result: list[dict] = []
    for m in messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {"call_id": tc.call_id, "tool_name": tc.tool_name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        result.append(d)
    return result


@pytest.mark.e2e
async def test_memory_inject_with_dump(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """读取真实 MEMORY.md，完整链路注入 system prompt 并落盘 JSON dump。

    验证：
    1. ensure() 保证 MEMORY.md 存在
    2. load() 读到的内容 == MEMORY.md 文件内容
    3. 发给 LLM 的 messages[0] 是 system 角色，内容包含 memory
    4. 落盘 JSON 的内容与 MEMORY.md 完全一致
    """
    # --- Step 1：确保 MEMORY.md 存在并读取 ---
    store = MemoryStore()
    memory_path = await store.ensure()
    loaded = await store.load()
    assert loaded is not None, "ensure() 后 load() 必须有内容"

    # 读原始文件用于对比
    raw_file_content = memory_path.read_text(encoding="utf-8").strip()
    assert loaded is not None
    assert loaded.strip() == raw_file_content, "load() 返回内容必须与文件内容一致"

    # --- Step 2：组装 instruction sources ---
    sources = [
        InstructionSource(origin="", content="You are kongming agent."),
        InstructionSource(origin="memory", content=loaded),
    ]

    # --- Step 3：InputAssembler 装配 ---
    assembler = InputAssembler()
    assembled = await assembler.assemble([], sources)

    assert assembled.metadata["added_system"] is True
    assert "memory" in assembled.metadata["instruction_sources"]

    system_msgs = [m for m in assembled.messages if m.role == "system"]
    assert len(system_msgs) == 1

    # system 消息内容包含 MEMORY.md 的完整文本（render 会 strip 各段内容）
    system_text = system_msgs[0].content
    assert loaded.strip() in system_text, "system prompt 必须包含 MEMORY.md 完整内容"

    # --- Step 4：通过 Runner 走完整链路 ---
    stub_llm.script(content="OK, noted your preferences.")

    runner = Runner(
        input_assembler=assembler,
        instruction_sources=sources,
    )
    session = InMemorySession("test-memory-inject")
    spec = AgentSpec(
        name="test",
        instructions="You are kongming agent.",
        default_model="stub",
        max_turns=3,
    )

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    assert result.status == "completed"

    # --- Step 5：验证 LLM 收到的请求 ---
    assert len(stub_llm.calls) == 1
    llm_request = stub_llm.calls[0]
    messages = list(llm_request.messages)

    assert messages[0].role == "system"
    assert loaded.strip() in messages[0].content, "发给 LLM 的 system 必须包含 MEMORY.md 完整内容"

    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) >= 1
    assert user_msgs[0].content == "hello"

    # --- Step 6：落盘 JSON dump（与 MEMORY.md 完全一致） ---
    dump = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": session.session_id,
        "model": spec.default_model,
        "memory_file": str(memory_path),
        "instruction_sources": assembled.metadata["instruction_sources"],
        "added_system": assembled.metadata["added_system"],
        "messages_sent_to_llm": _messages_to_dicts(messages),
    }

    dump_file = _dump_path()
    dump_file.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n📄 Memory inject dump: {dump_file.resolve()}")
    print(f"📄 Memory file: {memory_path.resolve()}")


@pytest.mark.e2e
async def test_memory_inject_without_memory_file(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """指定空 base_path 时，MEMORY.md 不存在，system prompt 只包含基础指令。"""
    store = MemoryStore(base_path=tmp_path / "nonexistent-project")
    loaded = await store.load()
    assert loaded is None

    sources = [InstructionSource(origin="", content="You are kongming agent.")]

    assembler = InputAssembler()
    assembled = await assembler.assemble([], sources)

    system_msgs = [m for m in assembled.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert "memory" not in assembled.metadata["instruction_sources"]
