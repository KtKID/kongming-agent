"""e2e P2：多轮 session 连续性。

覆盖 plan.md "e2e 主链路基线"的第 7 条 / README "多轮 session 连续性"：

    至少两轮对话连续运行，第二轮能够看到第一轮 history，不丢 session。

本文件覆盖 :class:`InMemorySession` 与 :class:`SQLiteSession` 两个实现，
确保它们都满足 :class:`core.contracts.Session` 协议在多轮场景下的契约：

1. ``InMemorySession`` 多轮内连续：同一实例跑两轮，第二轮 ``LLMRequest.messages``
   能看到第一轮 user + assistant。
2. ``SQLiteSession`` 跨实例恢复：模拟进程重启后，用相同 ``session_id`` + ``db_path``
   构造新实例仍能读到此前的历史，并在后续轮次里喂给 LLM。
3. ``SQLiteSession`` 多 session_id 隔离：同一个 db 文件里不同 session_id 彼此互不污染。
4. ``SQLiteSession`` ToolCall 往返：带 ``tool_calls`` 的 assistant 消息跨实例重建后
   仍能完整读回（call_id / tool_name / arguments 都保留）。

所有测试都走 ``StubLLMProvider``，不碰真实模型服务；``StubLLMProvider.calls`` 是
``list[LLMRequest]`` 累计记录，可以用 ``stub_llm.calls[-1].messages`` 拿到最后一次
LLM 请求看到的历史。对于 assistant 带 ``tool_calls`` 时 ``content`` 可能为 ``None``
的情况，测试里用 ``m.content`` 截取前过滤 ``None`` 或走 ``" ".join(... or "")`` 形式
的聚合，避免把 ``None`` 塞进字符串比较。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.message import Message, ToolCall
from sessions.session_store import SQLiteSession
from tests.e2e.conftest import RecordingApproval, StubLLMProvider

# ---------------------------------------------------------------------------
# 场景 1：InMemorySession 单实例内两轮连续
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_in_memory_session_preserves_history_across_runs(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """同一 :class:`InMemorySession` 跑两轮，第二轮模型能看到第一轮的 history。"""
    session = InMemorySession("multi-inmem")
    runner = Runner(event_sinks=[])
    spec = AgentSpec(
        name="t",
        instructions="memorize",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )

    # 第一轮
    stub_llm.script(content="first-answer")
    r1 = await runner.run(
        "round-one",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert r1.status == "completed"
    assert r1.final_message is not None
    assert r1.final_message.content == "first-answer"

    # 第二轮
    stub_llm.script(content="second-answer")
    r2 = await runner.run(
        "round-two",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert r2.status == "completed"
    assert r2.final_message is not None
    assert r2.final_message.content == "second-answer"

    # 第二轮的 LLM 请求中必须包含第一轮的 user + assistant + 第二轮 user。
    # 过滤 None 避免 assistant 带 tool_calls 时 content 为 None 污染字符串比较。
    assert len(stub_llm.calls) == 2
    last_request = stub_llm.calls[-1]
    content_list = [m.content for m in last_request.messages if m.content is not None]
    assert "round-one" in content_list
    assert "first-answer" in content_list
    assert "round-two" in content_list

    # session history 累积：至少包含两个 user + 两个 assistant
    hist = await session.history()
    assert len(hist) >= 4
    user_msgs = [m for m in hist if m.role == "user"]
    assistant_msgs = [m for m in hist if m.role == "assistant"]
    assert [m.content for m in user_msgs] == ["round-one", "round-two"]
    assert [m.content for m in assistant_msgs] == ["first-answer", "second-answer"]


@pytest.mark.e2e
async def test_in_memory_session_only_injects_system_once_across_runs(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """instructions 注入是幂等的——第二轮不会重复加一条 system 消息。"""
    session = InMemorySession("multi-inmem-sys")
    runner = Runner()
    spec = AgentSpec(
        name="t",
        instructions="SYS-PROMPT-ONCE",
        default_model="stub",
        max_turns=3,
    )

    stub_llm.script(content="a1")
    await runner.run(
        "u1",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    stub_llm.script(content="a2")
    await runner.run(
        "u2",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    hist = await session.history()
    system_msgs = [m for m in hist if m.role == "system"]
    # 关键断言：系统提示只应出现一次（_seed_messages 检测到已有 system 时不会再追加）
    assert len(system_msgs) == 1
    assert system_msgs[0].content == "SYS-PROMPT-ONCE"


# ---------------------------------------------------------------------------
# 场景 2：SQLiteSession 跨实例恢复
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_sqlite_session_recovers_history_after_reconstruction(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """第一轮跑完后，用相同 session_id + db_path 构造新实例，仍能读到之前的 history。"""
    db = tmp_path / "sessions.db"

    runner = Runner(event_sinks=[])
    spec = AgentSpec(
        name="t",
        instructions="persist",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )

    # ---- 第一轮：session1 ----
    session1 = SQLiteSession("persist-1", db)
    stub_llm.script(content="answer-1")
    r1 = await runner.run(
        "remember apple",
        session=session1,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert r1.status == "completed"

    # ---- 模拟进程重启：完全新建一个 SQLiteSession 实例，只给相同 session_id + db_path ----
    session2 = SQLiteSession("persist-1", db)
    hist_after_restart = await session2.history()
    # 第一轮至少留下：system + user + assistant 三条
    assert len(hist_after_restart) >= 2
    roles_after_restart = [m.role for m in hist_after_restart]
    assert "user" in roles_after_restart
    assert "assistant" in roles_after_restart

    # 第一轮的 user content 和 assistant content 都能从新实例读出
    contents_after_restart = [m.content for m in hist_after_restart if m.content is not None]
    assert "remember apple" in contents_after_restart
    assert "answer-1" in contents_after_restart

    # ---- 第二轮：使用新实例继续运行 ----
    stub_llm.script(content="answer-2")
    r2 = await runner.run(
        "what fruit did I mention?",
        session=session2,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert r2.status == "completed"

    # 第二轮发出的 LLMRequest.messages 必须带上第一轮的 user + assistant
    last_request = stub_llm.calls[-1]
    content_list = [m.content for m in last_request.messages if m.content is not None]
    assert "remember apple" in content_list
    assert "answer-1" in content_list
    # 第二轮自己这条 user 也在里面（大小写不敏感地匹配关键词，避免 None 污染）
    joined = " ".join(content_list).lower()
    assert "what fruit" in joined

    # 最终 SQLiteSession 的 history 至少包含两轮 user + 两轮 assistant
    final_hist = await session2.history()
    user_msgs = [m for m in final_hist if m.role == "user"]
    assistant_msgs = [m for m in final_hist if m.role == "assistant"]
    assert [m.content for m in user_msgs] == [
        "remember apple",
        "what fruit did I mention?",
    ]
    assert [m.content for m in assistant_msgs] == ["answer-1", "answer-2"]


# ---------------------------------------------------------------------------
# 场景 3：同 db、不同 session_id 互不污染
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_sqlite_session_isolates_different_session_ids(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """同一个 db 文件下，不同 session_id 互不污染 history。"""
    db = tmp_path / "multi-session.db"

    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )
    runner = Runner(event_sinks=[])

    # session A
    session_a = SQLiteSession("A", db)
    stub_llm.script(content="reply-A")
    await runner.run(
        "secret-A",
        session=session_a,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # session B
    session_b = SQLiteSession("B", db)
    stub_llm.script(content="reply-B")
    await runner.run(
        "topic-B",
        session=session_b,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    hist_a = await session_a.history()
    hist_b = await session_b.history()
    # 聚合 content 时过滤 None，保证 tool_calls-only 的 assistant 不会炸
    contents_a = " ".join(m.content or "" for m in hist_a)
    contents_b = " ".join(m.content or "" for m in hist_b)

    # A 只能看到自己的内容
    assert "secret-A" in contents_a
    assert "reply-A" in contents_a
    assert "secret-A" not in contents_b
    assert "reply-A" not in contents_b

    # B 只能看到自己的内容
    assert "topic-B" in contents_b
    assert "reply-B" in contents_b
    assert "topic-B" not in contents_a
    assert "reply-B" not in contents_a


# ---------------------------------------------------------------------------
# 场景 4：ToolCall 往返跨实例保留
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_sqlite_session_preserves_tool_call_roundtrip(
    tmp_path: Path,
) -> None:
    """Message 里带 tool_calls 的 assistant 消息，跨实例重建后仍能完整读回。

    这一条单独测的意义：assistant 消息的 ``content`` 允许为 ``None``（仅携带
    ``tool_calls``），而 :class:`SQLiteSession` 采取 JSON 序列化存储时必须完整往返
    ``tool_calls`` 结构（``ToolCall.call_id`` / ``tool_name`` / ``arguments``）。
    """
    db = tmp_path / "tool-roundtrip.db"
    session = SQLiteSession("tool-rt", db)

    # 手动 append 一条带 tool_calls 的 assistant 消息（content 为 None 是合法的）
    msg = Message(
        role="assistant",
        content=None,
        tool_calls=(
            ToolCall(
                call_id="x1",
                tool_name="read_file",
                arguments={"path": "/tmp/a"},
            ),
        ),
    )
    await session.append(msg)

    # 构造新 SQLiteSession 实例读
    session2 = SQLiteSession("tool-rt", db)
    hist = await session2.history()
    assert len(hist) == 1

    loaded = hist[0]
    assert loaded.role == "assistant"
    # content 为 None 的情况必须被完整保留，而不是被序列化层"偷偷"替换成空串
    assert loaded.content is None
    assert loaded.tool_calls is not None
    assert len(loaded.tool_calls) == 1

    only_call = loaded.tool_calls[0]
    assert only_call.call_id == "x1"
    assert only_call.tool_name == "read_file"
    assert only_call.arguments == {"path": "/tmp/a"}
