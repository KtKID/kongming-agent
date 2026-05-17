"""unit：``InputAssembler`` 的 attachments 汇总与透传契约。

对应 ``dev-pipeline/tasks/claude-image-paste-e2e/`` §4 multimodal-assembly
测试矩阵 D（pytest #21）。

关注点：

- ``AssembledInput.metadata["attachments"]`` 是**汇总视图**：把每条 user
  消息的 ``metadata["attachments"]`` 引用展平后放入，便于 trace 观测。
- assembler **不破坏** user 消息自身的 ``metadata["attachments"]``：
  ``final_messages`` 里每条 user 消息的 metadata 保持原始引用。
- 压缩器删除部分消息时，汇总反映"压缩后实际剩余"，不是原始 history。
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from context.input_assembler import InputAssembler
from core.message import Message


def _make_ref(
    asset_id: str,
    *,
    kind: str = "image",
    mime_type: str = "image/png",
) -> dict[str, object]:
    """构造一份 UserInputAttachment 形状的 ref dict。"""

    return {
        "asset_id": asset_id,
        "kind": kind,
        "mime_type": mime_type,
        "size_bytes": 12,
        "preview_url": f"/api/uploads/{asset_id}",
        "status": "ready",
    }


# ---------------------------------------------------------------------------
# D. metadata["attachments"] 汇总
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_assemble_no_attachments_metadata_field_is_empty_list() -> None:
    """历史无附件 → ``metadata["attachments"]`` 是空 list。

    （注意是空 list，不是缺失键——assemble 总是写入此 key。）
    """

    history: list[Message] = [
        Message.user("hi"),
        Message.assistant("hello"),
    ]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    assert result.metadata["attachments"] == []


@pytest.mark.unit
async def test_assemble_two_user_msgs_each_one_attachment_summarized() -> None:
    """2 条 user msg 各带 1 attachment → 汇总长度 2，顺序按消息顺序。"""

    refs1 = [_make_ref("a1")]
    refs2 = [_make_ref("a2", mime_type="image/jpeg")]
    history = [
        Message.user("first", metadata={"attachments": refs1}),
        Message.assistant("ack"),
        Message.user("second", metadata={"attachments": refs2}),
    ]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    summary = result.metadata["attachments"]
    assert len(summary) == 2
    assert summary[0]["asset_id"] == "a1"
    assert summary[1]["asset_id"] == "a2"


@pytest.mark.unit
async def test_assemble_preserves_attachments_in_user_message_metadata() -> None:
    """final_messages 中 user 消息的 metadata["attachments"] 保持原值。

    assembler 不应改写、深拷或丢失 attachments 引用——provider 直接读
    ``messages[i].metadata`` 拿原始结构。
    """

    refs = [_make_ref("origin1"), _make_ref("origin2", mime_type="image/gif")]
    user_msg = Message.user("preserve me", metadata={"attachments": refs})
    history = [user_msg, Message.assistant("ok")]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    user_msgs = [m for m in result.messages if m.role == "user"]
    assert len(user_msgs) == 1
    # 等值即可，不强求 is（compactor 可能浅拷贝消息）
    assert user_msgs[0].metadata["attachments"] == refs


@pytest.mark.unit
async def test_assemble_summary_reflects_compacted_history_not_original() -> None:
    """compactor 丢掉部分 user 消息时，attachments 汇总反映压缩后实际剩下的。

    用 mock compactor 模拟"丢掉前两条 user 附件"的极端场景，
    确认 summary 只来自 compacted 视图，不是原始 history。
    """

    keeper_refs = [_make_ref("keeper")]
    dropped_refs = [_make_ref("dropped1"), _make_ref("dropped2")]

    history = [
        Message.user("old1", metadata={"attachments": dropped_refs[:1]}),
        Message.user("old2", metadata={"attachments": dropped_refs[1:]}),
        Message.user("keep", metadata={"attachments": keeper_refs}),
    ]

    # mock 把前两条丢掉，只留下 keeper
    async def _compact(msgs: Sequence[Message]) -> list[Message]:
        return list(msgs[-1:])

    mock_compactor = MagicMock()
    mock_compactor.compact = AsyncMock(side_effect=_compact)
    assembler = InputAssembler(compactor=mock_compactor)  # type: ignore[arg-type]

    result = await assembler.assemble(history, [])

    summary = result.metadata["attachments"]
    assert len(summary) == 1
    assert summary[0]["asset_id"] == "keeper"


@pytest.mark.unit
async def test_assemble_skips_non_dict_refs_in_summary() -> None:
    """attachments 里混入非 dict（异常历史）→ 汇总只收 dict，跳过其他。

    assembler 本身做防御性过滤（``isinstance(r, dict)``），不抛错，
    与 :func:`executors.llm.media_adapter.collect_media_parts_from_messages`
    的"malformed ref 跳过"行为一致。
    """

    bad_then_good = [
        "not-a-dict",
        _make_ref("good"),
    ]
    history = [
        Message.user("mix", metadata={"attachments": bad_then_good}),
    ]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    summary = result.metadata["attachments"]
    assert len(summary) == 1
    assert summary[0]["asset_id"] == "good"


@pytest.mark.unit
async def test_assemble_attachments_field_value_not_list_ignored() -> None:
    """attachments 字段不是 list（如 str / dict）→ 汇总不收，不抛错。"""

    history = [
        Message.user("bad-shape", metadata={"attachments": "oops"}),
        Message.user("also-bad", metadata={"attachments": {"a": 1}}),
    ]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    assert result.metadata["attachments"] == []


@pytest.mark.unit
async def test_assemble_assistant_attachments_not_summarized() -> None:
    """汇总只看 user 消息——assistant / tool 消息含 attachments 不被收。

    （理论上不该出现，但 assembler 不能信任，要做最小防御。）
    """

    history = [
        Message.assistant(
            "assistant fake",
            metadata={"attachments": [_make_ref("bad-asst")]},
        ),
        Message.tool_result(
            tool_call_id="tc1",
            content="tool fake",
            metadata={"attachments": [_make_ref("bad-tool")]},
        ),
        Message.user("good", metadata={"attachments": [_make_ref("ok")]}),
    ]
    assembler = InputAssembler()

    result = await assembler.assemble(history, [])

    summary = result.metadata["attachments"]
    assert [r["asset_id"] for r in summary] == ["ok"]
