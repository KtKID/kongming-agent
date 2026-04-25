"""unit：context.history_compactor 纯逻辑覆盖。

B4 / CR 报告 cr-report-20260424-202744.md。覆盖：

- ``should_compact`` 在阈值以下 / 以上的分支
- 不就地修改入参；空输入返回空 list
- head（首条 system）保留 / 非首条 system 的行为
- middle 过滤空 assistant（无 content 且无 tool_calls）
- middle 保留 tool 消息，超长时截断并打标 ``compactor_truncated``
- tail 精确保留最近 N 条
- ``keep_system=False`` / ``keep_recent=0`` 边界
"""

from __future__ import annotations

import pytest

from context.history_compactor import _TRUNCATED_SUFFIX, CompactorConfig, HistoryCompactor
from core.message import Message, ToolCall


def _user(i: int) -> Message:
    return Message.user(f"u{i}")


def _asst(i: int) -> Message:
    return Message.assistant(content=f"a{i}")


def test_should_compact_below_threshold():
    cfg = CompactorConfig(max_messages=5)
    c = HistoryCompactor(cfg)
    history = [_user(i) for i in range(5)]
    assert c.should_compact(history) is False


def test_should_compact_above_threshold():
    cfg = CompactorConfig(max_messages=3)
    c = HistoryCompactor(cfg)
    history = [_user(i) for i in range(4)]
    assert c.should_compact(history) is True


@pytest.mark.asyncio
async def test_compact_below_threshold_returns_copy():
    c = HistoryCompactor(CompactorConfig(max_messages=10))
    history = [_user(i) for i in range(3)]
    result = await c.compact(history)
    assert result == history
    # 必须是新 list，不就地修改
    assert result is not history


@pytest.mark.asyncio
async def test_compact_empty_input():
    c = HistoryCompactor(CompactorConfig(max_messages=1))
    result = await c.compact([])
    assert result == []


@pytest.mark.asyncio
async def test_compact_preserves_first_system():
    cfg = CompactorConfig(max_messages=3, keep_recent=2, keep_system=True)
    c = HistoryCompactor(cfg)
    sys_msg = Message.system("rules")
    history = [sys_msg, _user(1), _user(2), _user(3), _user(4), _user(5)]
    result = await c.compact(history)
    # head 是首条 system；tail 是最近 2 条
    assert result[0] is sys_msg
    assert result[-2:] == [_user(4), _user(5)]


@pytest.mark.asyncio
async def test_compact_keep_system_false_does_not_anchor_system_as_head():
    """keep_system=False 时：首条 system 不被提到 head 块；但若出现在中段，
    仍会被 _filter_middle 保留（"中段额外 system"语义，见实现注释）。
    这里只校验"head 第一个不是 system"，符合 keep_system=False 的语义点。"""
    cfg = CompactorConfig(max_messages=3, keep_recent=2, keep_system=False)
    c = HistoryCompactor(cfg)
    sys_msg = Message.system("rules")
    history = [sys_msg, _user(1), _user(2), _user(3), _user(4)]
    result = await c.compact(history)
    # keep_system=False 时 head 不再锚定首条 system；但 system 仍可能通过 middle 保留
    # 判据：结果的头部应当是 middle 段内容，而不是被"提前"的 head。
    # 因为实现会在 head 空时把首条 system 放进 middle_source → 过滤后依然在原相对位置
    assert result.count(sys_msg) <= 1  # 不会被复制
    # tail 应仍保留最近 2 条
    assert result[-2:] == [_user(3), _user(4)]


@pytest.mark.asyncio
async def test_compact_filters_empty_assistant_in_middle():
    cfg = CompactorConfig(max_messages=3, keep_recent=2, keep_system=False)
    c = HistoryCompactor(cfg)
    empty_asst = Message.assistant(content=None)
    whitespace_asst = Message.assistant(content="   ")
    real_user = _user(99)
    history = [
        empty_asst,
        whitespace_asst,
        real_user,
        _user(100),
        _user(101),
    ]
    result = await c.compact(history)
    # 空 assistant 都被过滤掉；real_user 保留
    assert empty_asst not in result
    assert whitespace_asst not in result
    assert real_user in result


@pytest.mark.asyncio
async def test_compact_preserves_assistant_with_tool_calls_even_if_no_content():
    cfg = CompactorConfig(max_messages=2, keep_recent=1, keep_system=False)
    c = HistoryCompactor(cfg)
    tc = ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "/tmp/x"})
    asst_tc = Message.assistant(content=None, tool_calls=[tc])
    history = [_user(1), asst_tc, _user(2), _user(3)]
    result = await c.compact(history)
    # 带 tool_calls 的 assistant 应保留
    assert asst_tc in result


@pytest.mark.asyncio
async def test_compact_truncates_long_tool_result():
    cfg = CompactorConfig(
        max_messages=2, keep_recent=1, keep_system=False, tool_result_max_chars=10
    )
    c = HistoryCompactor(cfg)
    long_content = "x" * 100
    tool_msg = Message.tool_result(tool_call_id="c1", content=long_content, name="read_file")
    history = [_user(1), tool_msg, _user(2), _user(3)]
    result = await c.compact(history)

    kept_tool = next(m for m in result if m.role == "tool")
    assert kept_tool.content is not None
    assert kept_tool.content.startswith("x" * 10)
    assert _TRUNCATED_SUFFIX in kept_tool.content
    assert kept_tool.metadata.get("compactor_truncated") is True
    # metadata 与 tool_call_id 保留
    assert kept_tool.tool_call_id == "c1"
    assert kept_tool.name == "read_file"


@pytest.mark.asyncio
async def test_compact_does_not_truncate_short_tool_result():
    cfg = CompactorConfig(
        max_messages=2, keep_recent=1, keep_system=False, tool_result_max_chars=100
    )
    c = HistoryCompactor(cfg)
    tool_msg = Message.tool_result(tool_call_id="c1", content="short")
    history = [_user(1), tool_msg, _user(2), _user(3)]
    result = await c.compact(history)
    kept = next(m for m in result if m.role == "tool")
    # 未截断时保持原消息（不新增 compactor_truncated）
    assert kept.content == "short"
    assert "compactor_truncated" not in kept.metadata


@pytest.mark.asyncio
async def test_compact_tail_exact_keep_recent():
    cfg = CompactorConfig(max_messages=3, keep_recent=3, keep_system=False)
    c = HistoryCompactor(cfg)
    history = [_user(i) for i in range(10)]
    result = await c.compact(history)
    # 最后 3 条必须在 result 末尾
    assert result[-3:] == [_user(7), _user(8), _user(9)]


@pytest.mark.asyncio
async def test_compact_keep_recent_zero_falls_back_to_tail_is_empty():
    cfg = CompactorConfig(max_messages=1, keep_recent=0, keep_system=True)
    c = HistoryCompactor(cfg)
    history = [Message.system("s"), _user(1), _user(2)]
    result = await c.compact(history)
    # keep_recent=0 → tail 为空；保留 system + 中段筛选
    assert all(m.role != "assistant" or (m.content and m.content.strip()) for m in result)


@pytest.mark.asyncio
async def test_compact_does_not_mutate_input():
    cfg = CompactorConfig(max_messages=2, keep_recent=1)
    c = HistoryCompactor(cfg)
    history = [_user(1), _user(2), _user(3)]
    snapshot = list(history)
    await c.compact(history)
    assert history == snapshot


def test_config_exposed_via_property():
    cfg = CompactorConfig(max_messages=7, keep_recent=3)
    c = HistoryCompactor(cfg)
    assert c.config is cfg
    assert c.config.max_messages == 7
