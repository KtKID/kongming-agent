"""unit: Anthropic media 适配 + provider _convert_user_message 链路。

对应 ``dev-pipeline/tasks/claude-image-paste-e2e/`` §5 provider-media-adapters
的测试矩阵 A / B / C / D (pytest #25).

覆盖范围 (与 ``test_media_adapter.py`` 互不重叠):

- A. :class:`AnthropicMediaAdapter` — to_blocks / supports / unsupported kind 抛错
- B. :class:`OpenAIMediaAdapter` stub — supports 永 False / to_blocks 抛 NotImplementedError
- C. :meth:`AnthropicMessagesProvider._convert_user_message` — 纯文本 vs multi-block
- D. provider 注入 — 默认 / 自定义 MediaAdapter
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from config_loader.models import ModelConfig
from core.message import Message
from executors.llm.anthropic_messages import AnthropicMessagesProvider
from executors.llm.media_adapter import (
    AnthropicMediaAdapter,
    ImageMediaPart,
    MediaPart,
    OpenAIMediaAdapter,
)
from web.uploads.storage import AssetStorage

# ---------------------------------------------------------------------------
# 工具与 fixtures
# ---------------------------------------------------------------------------


# 任意 8 字节负载: 用于 base64 round-trip 校验, 不需要是合法 PNG.
PAYLOAD_PNG = b"\x89PNG_DATA"
PAYLOAD_JPEG = b"\xff\xd8\xff\xe0JFIF"
PAYLOAD_WEBP = b"RIFF_WEBPVP8"
PAYLOAD_GIF = b"GIF89a\x01\x00"


@pytest.fixture
def storage(tmp_path: Path) -> AssetStorage:
    """tmp_path 隔离的 AssetStorage."""

    return AssetStorage(base_dir=tmp_path / "uploads")


def _write_image(
    storage: AssetStorage,
    *,
    asset_id: str,
    thread_id: str,
    ext: str,
    payload: bytes,
) -> None:
    storage.write_asset(
        asset_id=asset_id,
        thread_id=thread_id,
        kind="image",
        ext=ext,
        payload=payload,
        metadata={"asset_id": asset_id, "thread_id": thread_id},
    )


def _make_provider(
    *,
    media_adapter: Any = None,
    asset_storage: AssetStorage | None = None,
) -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name="claude-sonnet-4-5",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        ),
        media_adapter=media_adapter,
        asset_storage=asset_storage,
    )


def _make_image_part(
    storage: AssetStorage,
    *,
    asset_id: str = "asset-1",
    thread_id: str = "thread-x",
    mime_type: str = "image/png",
    ext: str = ".png",
    payload: bytes = PAYLOAD_PNG,
) -> ImageMediaPart:
    _write_image(
        storage,
        asset_id=asset_id,
        thread_id=thread_id,
        ext=ext,
        payload=payload,
    )
    return ImageMediaPart(
        asset_id=asset_id,
        thread_id=thread_id,
        mime_type=mime_type,
        ext=ext,
        storage=storage,
    )


def _make_attachment_ref(
    *,
    asset_id: str = "asset-1",
    kind: str = "image",
    mime_type: str = "image/png",
    **extra: Any,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "asset_id": asset_id,
        "kind": kind,
        "mime_type": mime_type,
        "size_bytes": 12,
        "preview_url": f"/api/uploads/{asset_id}",
        "status": "ready",
    }
    ref.update(extra)
    return ref


@dataclass(frozen=True)
class _FakeVideoMediaPart:
    """fake MediaPart, kind="video" — 用于 unsupported kind 抛错测试."""

    asset_id: str = "fake-video"
    mime_type: str = "video/mp4"

    @property
    def kind(self) -> str:
        return "video"

    def load_bytes(self) -> bytes:
        return b""


class _CapturingMediaAdapter:
    """记录 to_blocks 调用次数 + 参数的 MediaAdapter 实装."""

    def __init__(self) -> None:
        self.calls: list[tuple[MediaPart, ...]] = []

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def to_blocks(self, parts: Any) -> list[dict[str, Any]]:
        parts_tuple = tuple(parts)
        self.calls.append(parts_tuple)
        # 返回单一占位 block, 不依赖真实 base64 编码.
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "FAKE",
                },
            }
        ]


# ---------------------------------------------------------------------------
# A. AnthropicMediaAdapter 单元测试
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_blocks_empty_list() -> None:
    """空 parts 列表 → 空 blocks 列表."""

    adapter = AnthropicMediaAdapter()
    assert adapter.to_blocks([]) == []


@pytest.mark.unit
def test_to_blocks_single_image(storage: AssetStorage) -> None:
    """单个 image part → 单个 anthropic image block, 字段对位."""

    part = _make_image_part(
        storage,
        asset_id="asset-1",
        thread_id="t1",
        mime_type="image/png",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    adapter = AnthropicMediaAdapter()
    blocks = adapter.to_blocks([part])

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    # data 字段必须是合法 base64
    decoded = base64.standard_b64decode(block["source"]["data"])
    assert decoded == PAYLOAD_PNG


@pytest.mark.unit
def test_to_blocks_multiple_images_order_preserved(
    storage: AssetStorage,
) -> None:
    """多个 image parts → 多个 image block, 顺序与输入一致."""

    p1 = _make_image_part(storage, asset_id="a1", thread_id="t1", payload=b"AAA")
    p2 = _make_image_part(
        storage,
        asset_id="a2",
        thread_id="t1",
        mime_type="image/jpeg",
        ext=".jpg",
        payload=b"BBB",
    )
    p3 = _make_image_part(
        storage,
        asset_id="a3",
        thread_id="t1",
        mime_type="image/webp",
        ext=".webp",
        payload=b"CCC",
    )

    adapter = AnthropicMediaAdapter()
    blocks = adapter.to_blocks([p1, p2, p3])

    assert len(blocks) == 3
    assert [b["source"]["media_type"] for b in blocks] == [
        "image/png",
        "image/jpeg",
        "image/webp",
    ]
    assert [base64.standard_b64decode(b["source"]["data"]) for b in blocks] == [
        b"AAA",
        b"BBB",
        b"CCC",
    ]


@pytest.mark.unit
def test_to_blocks_base64_round_trip(storage: AssetStorage) -> None:
    """data 字段 base64 解码后 == 原始 bytes."""

    raw = bytes(range(256))  # 0..255 全字节, 杜绝编码丢字节
    part = _make_image_part(
        storage,
        asset_id="round-trip",
        thread_id="t1",
        payload=raw,
    )
    adapter = AnthropicMediaAdapter()
    blocks = adapter.to_blocks([part])

    assert base64.standard_b64decode(blocks[0]["source"]["data"]) == raw


@pytest.mark.unit
@pytest.mark.parametrize(
    "mime_type,ext,payload",
    [
        ("image/png", ".png", PAYLOAD_PNG),
        ("image/jpeg", ".jpg", PAYLOAD_JPEG),
        ("image/webp", ".webp", PAYLOAD_WEBP),
        ("image/gif", ".gif", PAYLOAD_GIF),
    ],
)
def test_to_blocks_mime_type_preserved(
    storage: AssetStorage,
    mime_type: str,
    ext: str,
    payload: bytes,
) -> None:
    """4 种白名单 mime 都正确填到 source.media_type."""

    part = _make_image_part(
        storage,
        asset_id="m1",
        thread_id="t1",
        mime_type=mime_type,
        ext=ext,
        payload=payload,
    )
    blocks = AnthropicMediaAdapter().to_blocks([part])

    assert blocks[0]["source"]["media_type"] == mime_type


@pytest.mark.unit
def test_supports_image() -> None:
    """image kind 必须支持."""

    assert AnthropicMediaAdapter().supports("image") is True


@pytest.mark.unit
def test_supports_video_file_rejected() -> None:
    """v0.1.6 phase 1 不支持 video / file kind."""

    adapter = AnthropicMediaAdapter()
    assert adapter.supports("video") is False
    assert adapter.supports("file") is False


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["audio", "", "unknown", "IMAGE", "Image"])
def test_supports_other_kinds_false(kind: str) -> None:
    """音频 / 空串 / 大小写不同 / 任意未知 kind → 全 False."""

    assert AnthropicMediaAdapter().supports(kind) is False


@pytest.mark.unit
def test_to_blocks_unsupported_kind_raises() -> None:
    """fake MediaPart kind='video' → ValueError, 错误信息含 asset_id."""

    fake = _FakeVideoMediaPart(asset_id="vid-XYZ")
    adapter = AnthropicMediaAdapter()
    with pytest.raises(ValueError) as exc_info:
        adapter.to_blocks([fake])

    assert "vid-XYZ" in str(exc_info.value)
    assert "video" in str(exc_info.value)


# ---------------------------------------------------------------------------
# B. OpenAIMediaAdapter stub 测试
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["image", "video", "file", "audio", ""])
def test_openai_supports_returns_false_for_all_kinds(kind: str) -> None:
    """OpenAIMediaAdapter Phase 1 stub: supports 永远 False."""

    assert OpenAIMediaAdapter().supports(kind) is False


@pytest.mark.unit
def test_openai_to_blocks_raises_not_implemented(
    storage: AssetStorage,
) -> None:
    """OpenAIMediaAdapter.to_blocks 必须抛 NotImplementedError, 错误信息含 'Phase 2'."""

    part = _make_image_part(storage, asset_id="x", thread_id="t1")
    adapter = OpenAIMediaAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.to_blocks([part])

    assert "Phase 2" in str(exc_info.value)


@pytest.mark.unit
def test_openai_to_blocks_raises_even_with_empty_list() -> None:
    """空 parts 也要显式抛 NotImplementedError, 不能 silent 返回 []."""

    with pytest.raises(NotImplementedError):
        OpenAIMediaAdapter().to_blocks([])


# ---------------------------------------------------------------------------
# C. AnthropicMessagesProvider._convert_user_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_text_only_keeps_str_content() -> None:
    """纯文本 user message → content 仍是 str."""

    provider = _make_provider()
    msg = Message.user("hello world")

    result = provider._convert_user_message(msg, thread_id="t1")

    assert result == {"role": "user", "content": "hello world"}


@pytest.mark.unit
def test_user_with_one_image_attachment_multi_block(
    storage: AssetStorage,
) -> None:
    """text + 1 attachment → multi-block list, image 在前 text 在后."""

    _write_image(
        storage,
        asset_id="a1",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    provider = _make_provider(asset_storage=storage)
    msg = Message.user(
        "look at this",
        metadata={"attachments": [_make_attachment_ref(asset_id="a1")]},
    )

    result = provider._convert_user_message(msg, thread_id="t1")

    assert result["role"] == "user"
    content = result["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    # image block 在前
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    # text block 在后
    assert content[1] == {"type": "text", "text": "look at this"}


@pytest.mark.unit
def test_user_with_multiple_images(storage: AssetStorage) -> None:
    """3 个 attachments → 3 image + 1 text block, 顺序正确."""

    refs: list[dict[str, Any]] = []
    for i, mime in enumerate(["image/png", "image/jpeg", "image/webp"]):
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
        aid = f"img-{i}"
        _write_image(
            storage,
            asset_id=aid,
            thread_id="t1",
            ext=ext,
            payload=f"BODY-{i}".encode(),
        )
        refs.append(_make_attachment_ref(asset_id=aid, mime_type=mime))

    provider = _make_provider(asset_storage=storage)
    msg = Message.user("multi", metadata={"attachments": refs})

    result = provider._convert_user_message(msg, thread_id="t1")
    content = result["content"]
    assert isinstance(content, list)
    assert len(content) == 4
    # 前 3 个是 image, 类型分别对位
    assert [b["type"] for b in content[:3]] == ["image", "image", "image"]
    assert [b["source"]["media_type"] for b in content[:3]] == [
        "image/png",
        "image/jpeg",
        "image/webp",
    ]
    # 最后一个是 text
    assert content[3] == {"type": "text", "text": "multi"}


@pytest.mark.unit
def test_user_with_image_no_text(storage: AssetStorage) -> None:
    """text 空 + 1 attachment → content 是 list, 只含 image block (无 text block)."""

    _write_image(
        storage,
        asset_id="a1",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    provider = _make_provider(asset_storage=storage)
    # Message.user 不允许 content=None; 用 "" 表达"无文本"
    msg = Message.user(
        "",
        metadata={"attachments": [_make_attachment_ref(asset_id="a1")]},
    )

    result = provider._convert_user_message(msg, thread_id="t1")
    content = result["content"]

    assert isinstance(content, list)
    # 按实装当前写法: text 为空 → 不追加 text block, 仅 image
    # 若实装改为追加 ``{"type":"text","text":""}`` 也算合规, 因此宽容判断
    image_blocks = [b for b in content if b.get("type") == "image"]
    text_blocks = [b for b in content if b.get("type") == "text"]
    assert len(image_blocks) == 1
    # text_blocks 为空 OR 内容为空串都接受
    assert all(b.get("text", "") == "" for b in text_blocks)


@pytest.mark.unit
def test_user_with_unresolvable_attachment_falls_back_to_text(
    storage: AssetStorage,
) -> None:
    """mime 不在白名单 (image/bmp) → build 返回 None → 退化为 str content, 不抛错."""

    provider = _make_provider(asset_storage=storage)
    bad_ref = _make_attachment_ref(asset_id="a1", mime_type="image/bmp")
    msg = Message.user("fallback please", metadata={"attachments": [bad_ref]})

    result = provider._convert_user_message(msg, thread_id="t1")

    # 退化路径: content 是 str
    assert result == {"role": "user", "content": "fallback please"}


@pytest.mark.unit
def test_user_with_empty_attachments_list_keeps_str_content() -> None:
    """metadata={'attachments': []} → 走 str 路径, 不进 multi-block 分支."""

    provider = _make_provider()
    msg = Message.user("hello", metadata={"attachments": []})

    result = provider._convert_user_message(msg, thread_id="t1")

    assert result == {"role": "user", "content": "hello"}


@pytest.mark.unit
def test_user_with_missing_metadata_keeps_str_content() -> None:
    """metadata 为空 dict (Message 默认) → 走 str 路径."""

    provider = _make_provider()
    msg = Message.user("hello")

    result = provider._convert_user_message(msg, thread_id="t1")

    assert result == {"role": "user", "content": "hello"}


@pytest.mark.unit
def test_user_with_attachments_not_list_keeps_str_content() -> None:
    """metadata['attachments'] 不是 list (协议错误) → 容错走 str 路径, 不抛."""

    provider = _make_provider()
    msg = Message.user(
        "hello",
        metadata={"attachments": "not-a-list"},  # type: ignore[dict-item]
    )

    result = provider._convert_user_message(msg, thread_id="t1")

    assert result == {"role": "user", "content": "hello"}


# ---------------------------------------------------------------------------
# D. provider 注入测试
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_default_media_adapter() -> None:
    """不传 media_adapter / asset_storage → 内部默认实例化, 纯文本聊天不抛错."""

    provider = _make_provider()

    # 纯文本场景应当顺利转换为 str content (不触发 storage IO)
    msg = Message.user("plain text only")
    result = provider._convert_user_message(msg, thread_id="t1")
    assert result == {"role": "user", "content": "plain text only"}

    # 验证默认 media_adapter 是 AnthropicMediaAdapter
    assert isinstance(provider._media_adapter, AnthropicMediaAdapter)
    # 验证默认 asset_storage 是 AssetStorage
    assert isinstance(provider._asset_storage, AssetStorage)


@pytest.mark.unit
def test_provider_inject_fake_adapter(storage: AssetStorage) -> None:
    """注入 fake MediaAdapter → 含 attachment 的 user message 触发 fake.to_blocks 1 次."""

    _write_image(
        storage,
        asset_id="a1",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    fake_adapter = _CapturingMediaAdapter()
    provider = _make_provider(media_adapter=fake_adapter, asset_storage=storage)

    msg = Message.user(
        "with image",
        metadata={"attachments": [_make_attachment_ref(asset_id="a1")]},
    )

    result = provider._convert_user_message(msg, thread_id="t1")

    # fake.to_blocks 被调用恰好 1 次
    assert len(fake_adapter.calls) == 1
    # 入参是 1 个 MediaPart (image)
    parts = fake_adapter.calls[0]
    assert len(parts) == 1
    assert parts[0].kind == "image"
    assert parts[0].asset_id == "a1"

    # provider 返回的 content 含 fake adapter 产出的 image block + text
    content = result["content"]
    assert isinstance(content, list)
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["data"] == "FAKE"


@pytest.mark.unit
def test_provider_inject_adapter_not_called_for_plain_text(
    storage: AssetStorage,
) -> None:
    """纯文本 user message (无 attachments) 不调 media_adapter.to_blocks."""

    fake_adapter = _CapturingMediaAdapter()
    provider = _make_provider(media_adapter=fake_adapter, asset_storage=storage)

    msg = Message.user("plain text only")
    provider._convert_user_message(msg, thread_id="t1")

    assert fake_adapter.calls == []


# ---------------------------------------------------------------------------
# E. R2 boundary fix: to_blocks 抛错时 _convert_user_message 必须退化纯文本
# ---------------------------------------------------------------------------


class _FailingMediaAdapter:
    """to_blocks 必抛 ``FileNotFoundError`` 的 MediaAdapter 实装。

    用来回归 P0-2：旧实现 to_blocks 抛错时整轮 turn 崩，违反"图片有问题 →
    退化纯文本"承诺。修复后 :meth:`AnthropicMessagesProvider._convert_user_message`
    必须 catch (FileNotFoundError / OSError / ValueError) → 返回 str content。
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def to_blocks(self, parts: Any) -> list[dict[str, Any]]:
        self.calls += 1
        raise self._exc


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("/tmp/missing.png"),
        OSError("disk read error"),
        ValueError("bad mime"),
    ],
)
def test_convert_user_message_falls_back_when_to_blocks_raises(
    storage: AssetStorage,
    exc: Exception,
) -> None:
    """P0-2 R2 boundary fix：to_blocks 抛 IO/格式错 → 退化纯文本，不抛。

    构造一份合法 attachment ref（asset 真存在）让 ``collect_media_parts_from_messages``
    返回非空 parts，再注入 _FailingMediaAdapter 让 to_blocks 抛错，验证
    provider 不冒泡而是退化到 ``{"role":"user","content": text}``。
    """

    _write_image(
        storage,
        asset_id="boom",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    failing = _FailingMediaAdapter(exc)
    provider = _make_provider(media_adapter=failing, asset_storage=storage)

    msg = Message.user(
        "describe this image",
        metadata={"attachments": [_make_attachment_ref(asset_id="boom")]},
    )

    # 修复后必须不抛
    result = provider._convert_user_message(msg, thread_id="t1")

    # to_blocks 确实被调到了（证明走到了 multi-block 分支）
    assert failing.calls == 1
    # 退化路径：content 是 str（与 not parts 同 return 分支等价）
    assert result == {"role": "user", "content": "describe this image"}


@pytest.mark.unit
def test_convert_user_message_to_blocks_failure_preserves_empty_text(
    storage: AssetStorage,
) -> None:
    """to_blocks 抛错 + 原始 text 为空 → 退化路径返回 content="" 而非崩溃。"""

    _write_image(
        storage,
        asset_id="boom2",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    failing = _FailingMediaAdapter(FileNotFoundError("vanished.png"))
    provider = _make_provider(media_adapter=failing, asset_storage=storage)
    msg = Message.user(
        "",
        metadata={"attachments": [_make_attachment_ref(asset_id="boom2")]},
    )

    result = provider._convert_user_message(msg, thread_id="t1")
    assert result == {"role": "user", "content": ""}


# ---------------------------------------------------------------------------
# F. P1 #1 R2 boundary fix: dropped_attachments 后端 audit trail
#
# 退化分支需要把丢弃的 asset_id 透传到 ``LLMRequest.metadata["dropped_attachments"]``
# 让上层(观测 / audit / 未来前端 toast)能感知 silent fallback。
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dropped_attachments_collected_when_to_blocks_fails(
    storage: AssetStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """to_blocks 抛 FileNotFoundError → dropped list 收集 asset_id + warning log。"""

    _write_image(
        storage,
        asset_id="vanish-1",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    failing = _FailingMediaAdapter(FileNotFoundError("vanished.png"))
    provider = _make_provider(media_adapter=failing, asset_storage=storage)
    msg = Message.user(
        "describe",
        metadata={"attachments": [_make_attachment_ref(asset_id="vanish-1")]},
    )

    dropped: list[str] = []
    with caplog.at_level("WARNING", logger="executors.llm.anthropic_messages"):
        result = provider._convert_user_message(msg, thread_id="t1", dropped=dropped)

    # 退化:str content
    assert result == {"role": "user", "content": "describe"}
    # asset_id 进 dropped
    assert dropped == ["vanish-1"]
    # warning log 含 asset_id
    assert any("vanish-1" in rec.getMessage() for rec in caplog.records), (
        f"expected 'vanish-1' in warnings; got={[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.unit
def test_dropped_attachments_collected_when_parts_empty(
    storage: AssetStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """attachments 全是 unsupported mime(image/bmp) → collect_media_parts 返回空,
    asset_id 也要进 dropped。
    """

    provider = _make_provider(asset_storage=storage)
    bad_ref = _make_attachment_ref(asset_id="bmp-bad", mime_type="image/bmp")
    msg = Message.user("fallback please", metadata={"attachments": [bad_ref]})

    dropped: list[str] = []
    with caplog.at_level("WARNING", logger="executors.llm.anthropic_messages"):
        result = provider._convert_user_message(msg, thread_id="t1", dropped=dropped)

    # 退化路径:str content
    assert result == {"role": "user", "content": "fallback please"}
    # asset_id 进 dropped
    assert dropped == ["bmp-bad"]


@pytest.mark.unit
def test_build_payload_writes_dropped_to_request_metadata(
    storage: AssetStorage,
) -> None:
    """端到端验证 ``_build_payload`` 把 dropped asset_id 写到 request.metadata["dropped_attachments"]。

    这是后端 audit trail 的真正入口:观测 sink / 未来前端 toast 都从
    request.metadata["dropped_attachments"] 读。
    """

    from core.contracts import LLMRequest

    _write_image(
        storage,
        asset_id="missing-asset",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    failing = _FailingMediaAdapter(FileNotFoundError("vanish"))
    provider = _make_provider(media_adapter=failing, asset_storage=storage)

    msg = Message.user(
        "with image",
        metadata={"attachments": [_make_attachment_ref(asset_id="missing-asset")]},
    )
    request = LLMRequest(
        model="claude-sonnet-4-5",
        messages=(msg,),
        metadata={"thread_id": "t1"},
    )

    payload = provider._build_payload(request)

    # 确认是退化纯文本:user content 是 str(不是 list)
    user_messages = [m for m in payload["messages"] if m.get("role") == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == "with image"

    # 关键回归:dropped_attachments 写入 request.metadata
    assert "dropped_attachments" in request.metadata
    assert request.metadata["dropped_attachments"] == ["missing-asset"]


@pytest.mark.unit
def test_build_payload_no_dropped_field_when_attachments_loaded_ok(
    storage: AssetStorage,
) -> None:
    """正常路径(图片正常加载)不应在 request.metadata 里塞 dropped_attachments 字段。"""

    from core.contracts import LLMRequest

    _write_image(
        storage,
        asset_id="ok-1",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    provider = _make_provider(asset_storage=storage)
    msg = Message.user(
        "hi",
        metadata={"attachments": [_make_attachment_ref(asset_id="ok-1")]},
    )
    request = LLMRequest(
        model="claude-sonnet-4-5",
        messages=(msg,),
        metadata={"thread_id": "t1"},
    )

    provider._build_payload(request)

    # 正常路径不应写 dropped_attachments
    assert "dropped_attachments" not in request.metadata


@pytest.mark.unit
def test_convert_user_message_dropped_none_does_not_crash(
    storage: AssetStorage,
) -> None:
    """调用方不传 dropped(None) 时退化分支不能崩 — 向后兼容路径。"""

    _write_image(
        storage,
        asset_id="legacy",
        thread_id="t1",
        ext=".png",
        payload=PAYLOAD_PNG,
    )
    failing = _FailingMediaAdapter(FileNotFoundError("x"))
    provider = _make_provider(media_adapter=failing, asset_storage=storage)
    msg = Message.user(
        "txt",
        metadata={"attachments": [_make_attachment_ref(asset_id="legacy")]},
    )

    # 不传 dropped(默认 None) → 退化但不抛
    result = provider._convert_user_message(msg, thread_id="t1")
    assert result == {"role": "user", "content": "txt"}
