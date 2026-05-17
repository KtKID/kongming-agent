"""unit：``executors.llm.media_adapter`` 模块行为契约。

对应 ``dev-pipeline/tasks/claude-image-paste-e2e/`` §4 multimodal-assembly
的测试矩阵 A / B / C / E（pytest #21）。

覆盖：

- A. :func:`build_media_part_from_metadata` 各种 happy / 容错 / 未知 kind 分支
- B. :class:`ImageMediaPart.load_bytes` 真实 storage 读写 + 缺失抛错
- C. :func:`collect_media_parts_from_messages` 多消息汇总 / 容错 / 跳过非 user
- E. JSONL round-trip（``_message_to_dict`` / ``_message_from_dict``）后
  附件信息保留，仍可被 :func:`collect_media_parts_from_messages` 还原
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from context.session_store import _message_from_dict, _message_to_dict
from core.message import Message
from executors.llm.media_adapter import (
    ImageMediaPart,
    build_media_part_from_metadata,
    collect_media_parts_from_messages,
)
from web.uploads.registry import EXT_BY_MIME
from web.uploads.storage import AssetStorage

# ---------------------------------------------------------------------------
# 工具：构造 attachment ref dict + 落盘 fixture
# ---------------------------------------------------------------------------


def _make_ref(
    *,
    asset_id: str = "aaaabbbb",
    kind: str = "image",
    mime_type: str = "image/png",
    **extra: object,
) -> dict[str, object]:
    """构造一份 UserInputAttachment 形状的 ref dict。"""

    ref: dict[str, object] = {
        "asset_id": asset_id,
        "kind": kind,
        "mime_type": mime_type,
        "size_bytes": 12,
        "preview_url": f"/api/uploads/{asset_id}",
        "status": "ready",
    }
    ref.update(extra)
    return ref


@pytest.fixture
def storage(tmp_path: Path) -> AssetStorage:
    """tmp_path 隔离的 :class:`AssetStorage`。"""

    return AssetStorage(base_dir=tmp_path / "uploads")


def _write_image_bytes(
    storage: AssetStorage,
    *,
    asset_id: str,
    thread_id: str,
    ext: str,
    payload: bytes,
) -> None:
    """直接通过 storage 写入一份资产（含最小 metadata 字典）。"""

    storage.write_asset(
        asset_id=asset_id,
        thread_id=thread_id,
        kind="image",
        ext=ext,
        payload=payload,
        metadata={"asset_id": asset_id, "thread_id": thread_id},
    )


# ---------------------------------------------------------------------------
# A. build_media_part_from_metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_media_part_happy_path_image_png(storage: AssetStorage) -> None:
    """合法 image/png ref → 返回 ImageMediaPart，所有字段对位填充。"""

    ref = _make_ref(asset_id="abc123", mime_type="image/png")
    part = build_media_part_from_metadata(ref, thread_id="thread-x", storage=storage)

    assert isinstance(part, ImageMediaPart)
    assert part.kind == "image"
    assert part.asset_id == "abc123"
    assert part.thread_id == "thread-x"
    assert part.mime_type == "image/png"
    assert part.ext == ".png"
    assert part.storage is storage


@pytest.mark.unit
@pytest.mark.parametrize(
    "mime_type,expected_ext",
    [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
    ],
)
def test_build_media_part_ext_lookup_per_mime(
    storage: AssetStorage, mime_type: str, expected_ext: str
) -> None:
    """所有 EXT_BY_MIME 白名单 MIME 都能反查到正确 ext。"""

    ref = _make_ref(mime_type=mime_type)
    part = build_media_part_from_metadata(ref, thread_id="t", storage=storage)

    assert isinstance(part, ImageMediaPart)
    assert part.ext == expected_ext
    # EXT_BY_MIME 的契约真源
    assert part.ext == EXT_BY_MIME[mime_type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_key",
    ["asset_id", "kind", "mime_type"],
)
def test_build_media_part_missing_required_field_returns_none(
    storage: AssetStorage, missing_key: str
) -> None:
    """缺 asset_id / kind / mime_type 任一必需字段 → None。"""

    ref = _make_ref()
    ref.pop(missing_key)
    part = build_media_part_from_metadata(ref, thread_id="t", storage=storage)

    assert part is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("asset_id", ""),  # 空字符串
        ("asset_id", 12345),  # 非 str
        ("kind", ""),
        ("kind", 7),
        ("mime_type", ""),
        ("mime_type", None),
    ],
)
def test_build_media_part_invalid_field_type_returns_none(
    storage: AssetStorage, field: str, bad_value: object
) -> None:
    """必需字段非 str / 空值 → None。"""

    ref = _make_ref()
    ref[field] = bad_value
    part = build_media_part_from_metadata(ref, thread_id="t", storage=storage)

    assert part is None


@pytest.mark.unit
@pytest.mark.parametrize("unknown_kind", ["audio", "video", "file", "weird"])
def test_build_media_part_unknown_or_future_kind_returns_none(
    storage: AssetStorage, unknown_kind: str
) -> None:
    """非 image kind（含未来扩展的 video / file 与未知 audio）→ None。

    audio 在协议里完全没定义，video / file 是 union 预留但本期不支持。
    所有非 image 都应返回 None，调用方跳过。
    """

    ref = _make_ref(kind=unknown_kind)
    part = build_media_part_from_metadata(ref, thread_id="t", storage=storage)

    assert part is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_mime",
    [
        "image/bmp",  # 不在白名单
        "image/tiff",
        "text/plain",
        "application/octet-stream",
    ],
)
def test_build_media_part_image_with_non_whitelisted_mime_returns_none(
    storage: AssetStorage, bad_mime: str
) -> None:
    """image kind 但 mime_type 不在 EXT_BY_MIME → None。"""

    ref = _make_ref(mime_type=bad_mime)
    part = build_media_part_from_metadata(ref, thread_id="t", storage=storage)

    assert part is None


# ---------------------------------------------------------------------------
# B. ImageMediaPart.load_bytes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_image_media_part_load_bytes_round_trip(
    storage: AssetStorage,
) -> None:
    """写入资产 bytes → ImageMediaPart.load_bytes 读出原 bytes 完全一致。"""

    payload = b"\x89PNG\r\n\x1a\nFAKE-PNG-PAYLOAD"
    _write_image_bytes(
        storage,
        asset_id="img1",
        thread_id="tA",
        ext=".png",
        payload=payload,
    )

    part = ImageMediaPart(
        asset_id="img1",
        thread_id="tA",
        mime_type="image/png",
        ext=".png",
        storage=storage,
    )

    assert part.load_bytes() == payload


@pytest.mark.unit
def test_image_media_part_load_bytes_missing_asset_raises_filenotfound(
    storage: AssetStorage,
) -> None:
    """资产不存在 → load_bytes 直接抛 FileNotFoundError（不静默吞）。"""

    part = ImageMediaPart(
        asset_id="ghost",
        thread_id="tNope",
        mime_type="image/png",
        ext=".png",
        storage=storage,
    )

    with pytest.raises(FileNotFoundError):
        part.load_bytes()


@pytest.mark.unit
def test_image_media_part_kind_property_is_image(
    storage: AssetStorage,
) -> None:
    """ImageMediaPart.kind 固定为 'image'。"""

    part = ImageMediaPart(
        asset_id="x",
        thread_id="t",
        mime_type="image/png",
        ext=".png",
        storage=storage,
    )
    assert part.kind == "image"


@pytest.mark.unit
def test_image_media_part_is_frozen(storage: AssetStorage) -> None:
    """ImageMediaPart 是 frozen dataclass，不能就地修改。"""

    part = ImageMediaPart(
        asset_id="x",
        thread_id="t",
        mime_type="image/png",
        ext=".png",
        storage=storage,
    )
    with pytest.raises(Exception):
        # frozen → FrozenInstanceError
        part.asset_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# C. collect_media_parts_from_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collect_media_parts_empty_messages(storage: AssetStorage) -> None:
    """空 messages → 返回空 list。"""

    parts = collect_media_parts_from_messages([], storage=storage, thread_id="t")
    assert parts == []


@pytest.mark.unit
def test_collect_media_parts_single_user_msg_single_attachment(
    storage: AssetStorage,
) -> None:
    """1 条 user msg + 1 个 attachment → 返回 1 个 ImageMediaPart。"""

    msg = Message.user(
        "看这张图",
        metadata={"attachments": [_make_ref(asset_id="a1")]},
    )

    parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")

    assert len(parts) == 1
    assert isinstance(parts[0], ImageMediaPart)
    assert parts[0].asset_id == "a1"


@pytest.mark.unit
def test_collect_media_parts_single_user_msg_three_attachments(
    storage: AssetStorage,
) -> None:
    """1 条 user msg + 3 个 attachment → 返回 3 个，保持原顺序。"""

    refs = [
        _make_ref(asset_id="a1"),
        _make_ref(asset_id="a2", mime_type="image/jpeg"),
        _make_ref(asset_id="a3", mime_type="image/webp"),
    ]
    msg = Message.user("三张图", metadata={"attachments": refs})

    parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")

    assert [p.asset_id for p in parts] == ["a1", "a2", "a3"]


@pytest.mark.unit
def test_collect_media_parts_two_user_msgs_each_one_attachment(
    storage: AssetStorage,
) -> None:
    """2 条 user msg 各带 1 attachment → 顺序按消息顺序展平。"""

    msgs = [
        Message.user("first", metadata={"attachments": [_make_ref(asset_id="first")]}),
        Message.assistant("ack"),
        Message.user("second", metadata={"attachments": [_make_ref(asset_id="second")]}),
    ]
    parts = collect_media_parts_from_messages(msgs, storage=storage, thread_id="t")

    assert [p.asset_id for p in parts] == ["first", "second"]


@pytest.mark.unit
def test_collect_media_parts_assistant_and_tool_attachments_ignored(
    storage: AssetStorage,
) -> None:
    """理论上不该出现：assistant / tool 消息含 attachments → 不被提取。"""

    msgs = [
        Message.assistant(
            "assistant fake",
            metadata={"attachments": [_make_ref(asset_id="bad-asst")]},
        ),
        Message.tool_result(
            tool_call_id="tc1",
            content="tool fake",
            metadata={"attachments": [_make_ref(asset_id="bad-tool")]},
        ),
        Message.user("good", metadata={"attachments": [_make_ref(asset_id="ok")]}),
    ]

    parts = collect_media_parts_from_messages(msgs, storage=storage, thread_id="t")

    assert [p.asset_id for p in parts] == ["ok"]


@pytest.mark.unit
def test_collect_media_parts_malformed_ref_not_dict_warns_and_skips(
    storage: AssetStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ref 不是 dict（如 str）→ 跳过 + warning log。"""

    msg = Message.user(
        "bad ref",
        metadata={"attachments": ["not-a-dict", _make_ref(asset_id="good")]},
    )

    with caplog.at_level(logging.WARNING, logger="executors.llm.media_adapter"):
        parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")

    assert [p.asset_id for p in parts] == ["good"]
    assert any("malformed attachment ref" in rec.getMessage() for rec in caplog.records)


@pytest.mark.unit
def test_collect_media_parts_unknown_kind_warns_and_skips(
    storage: AssetStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ref kind 不在支持范围 → 跳过 + warning log（"unrebuildable"）。"""

    msg = Message.user(
        "future",
        metadata={
            "attachments": [
                _make_ref(asset_id="future-video", kind="video"),
                _make_ref(asset_id="ok"),
            ]
        },
    )

    with caplog.at_level(logging.WARNING, logger="executors.llm.media_adapter"):
        parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")

    assert [p.asset_id for p in parts] == ["ok"]
    assert any("unrebuildable attachment ref" in rec.getMessage() for rec in caplog.records)


@pytest.mark.unit
def test_collect_media_parts_metadata_none_skipped(
    storage: AssetStorage,
) -> None:
    """user message 没有 metadata（dataclass 默认空 dict）→ 不被提取，不报错。

    Message.user(...) 默认 metadata={}, 我们再补一种 frozen instance 自己
    metadata 是 None 的极端情况（直接构造 dataclass）来验证 `msg.metadata or {}`
    分支。
    """

    msg_default = Message.user("no metadata")
    # 直接构造一个 metadata=None 的极端 Message（绕过 factory 默认值）
    msg_none = Message(role="user", content="metadata=None", metadata=None)  # type: ignore[arg-type]

    parts = collect_media_parts_from_messages(
        [msg_default, msg_none], storage=storage, thread_id="t"
    )
    assert parts == []


@pytest.mark.unit
def test_collect_media_parts_metadata_missing_attachments_key_skipped(
    storage: AssetStorage,
) -> None:
    """metadata 不含 attachments key → 跳过，无 warning。"""

    msg = Message.user("foo", metadata={"other_key": "x"})

    parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")
    assert parts == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_value",
    [
        "not-a-list",  # str
        123,  # int
        {"asset_id": "x"},  # dict 不是 list
    ],
)
def test_collect_media_parts_attachments_not_list_skipped(
    storage: AssetStorage, bad_value: object
) -> None:
    """metadata['attachments'] 不是 list → 跳过，不抛错。"""

    msg = Message.user("bar", metadata={"attachments": bad_value})

    parts = collect_media_parts_from_messages([msg], storage=storage, thread_id="t")
    assert parts == []


# ---------------------------------------------------------------------------
# E. 端到端协议 round-trip（轻量级）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_message_with_attachments_jsonl_round_trip_preserves_metadata(
    storage: AssetStorage,
) -> None:
    """构造 user message + attachments → 序列化 → 反序列化 → 字段完整等价。

    模拟 file_session JSONL 持久化往返。
    """

    refs = [
        _make_ref(asset_id="rt1", mime_type="image/png"),
        _make_ref(asset_id="rt2", mime_type="image/jpeg"),
    ]
    original = Message.user("round trip", metadata={"attachments": refs})

    encoded = _message_to_dict(original)
    decoded = _message_from_dict(encoded)

    assert decoded.role == "user"
    assert decoded.content == "round trip"
    assert decoded.metadata.get("attachments") == refs


@pytest.mark.unit
def test_collect_media_parts_works_on_jsonl_restored_message(
    storage: AssetStorage,
) -> None:
    """从 JSONL 持久化往返还原的 message，仍能被 collect 还原成 MediaPart。"""

    original = Message.user(
        "from jsonl",
        metadata={"attachments": [_make_ref(asset_id="loaded")]},
    )
    restored = _message_from_dict(_message_to_dict(original))

    parts = collect_media_parts_from_messages([restored], storage=storage, thread_id="t")

    assert len(parts) == 1
    assert isinstance(parts[0], ImageMediaPart)
    assert parts[0].asset_id == "loaded"
