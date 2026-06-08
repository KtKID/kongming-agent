"""多模态输入抽象：MediaPart Protocol + ImageMediaPart 实装。

设计动机：
- assembly 层（§4 multimodal-assembly）把 ``Message.metadata["attachments"]``
  还原成 ``list[MediaPart]``，与 provider 解耦。
- provider 适配层（§5 provider-media-adapters）消费 ``list[MediaPart]``，
  按厂商规范转 content block；每个 provider 一个 MediaAdapter 实现。

本文件**只**定义 :class:`MediaPart` Protocol + :class:`ImageMediaPart` 实装
+ :func:`build_media_part_from_metadata` 工厂函数。未来 ``VideoMediaPart`` /
``FileMediaPart`` 加新类即可。``MediaAdapter`` 由 §5 任务（#22）定义，本文件
不涉及。

容错策略：
- :func:`build_media_part_from_metadata` 对未知 kind / 字段缺失返回 ``None``，
  调用方应跳过（建议 warning log）而不是抛错，保持向后兼容（旧 JSONL
  历史可能含未来格式或缺字段的格式）。
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.message import Message
from web.uploads.registry import EXT_BY_MIME
from web.uploads.storage import AssetStorage, AttachmentKind

__all__ = [
    "AnthropicMediaAdapter",
    "AssetStorage",
    "ImageMediaPart",
    "MediaAdapter",
    "MediaPart",
    "OpenAIMediaAdapter",
    "build_media_part_from_metadata",
    "collect_media_parts_from_messages",
]

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class MediaPart(Protocol):
    """assembly 输出的统一中间表示。

    每个 MediaPart 持有足以让 adapter 完成转换的信息：

    - :attr:`kind`：类型 union（image / video / file），让 adapter 分派。
    - :attr:`asset_id`：资产唯一 id（debug / 日志用）。
    - :attr:`mime_type`：用于 provider 设置 media_type。
    - :meth:`load_bytes`：读资产 bytes（lazy，避免在 assembly 阶段就读全量）。

    设计为 ``runtime_checkable`` Protocol 而非 abstract base class：
    duck typing 让未来不同来源（本地存储 / 对象存储 / 内存缓存）的实装
    都能直接传给 adapter，无需共享继承树。

    所有方法都是同步的。assembly 是同步函数，如果未来需要异步读取
    （如对象存储 / 跨进程缓存），再加 ``load_bytes_async`` 方法，
    现阶段保持简单。
    """

    @property
    def kind(self) -> AttachmentKind: ...

    @property
    def asset_id(self) -> str: ...

    @property
    def mime_type(self) -> str: ...

    def load_bytes(self) -> bytes: ...


@dataclass(frozen=True)
class ImageMediaPart:
    """图片实装。

    持 ``asset_id`` + ``thread_id`` + ``mime_type`` + ``ext`` + storage 引用；
    :meth:`load_bytes` 触发时才走 ``storage.read_asset_bytes``。

    设计：

    - ``dataclass(frozen=True)`` 保证不可变，避免 assembly → adapter 路径上
      被意外修改；与 :class:`web.uploads.storage.AttachmentAsset` 风格一致。
    - ``storage`` 字段是 IO 抽象，让单测可注入 fake storage（不依赖真实
      ``.kongming/web/uploads/`` 目录）。
    """

    asset_id: str
    thread_id: str
    mime_type: str
    ext: str
    storage: AssetStorage

    @property
    def kind(self) -> AttachmentKind:
        """图片实装固定返回 ``"image"``。"""

        return "image"

    def load_bytes(self) -> bytes:
        """通过 ``storage`` 读取资产 bytes（同步）。

        Raises:
            FileNotFoundError: 资产不存在（被 GC / 误删）。
        """

        return self.storage.read_asset_bytes(
            asset_id=self.asset_id,
            thread_id=self.thread_id,
            kind="image",
            ext=self.ext,
        )


def build_media_part_from_metadata(
    ref: dict[str, Any],
    *,
    thread_id: str,
    storage: AssetStorage,
) -> MediaPart | None:
    """从 ``Message.metadata["attachments"][i]`` 还原成 :class:`MediaPart` 实例。

    ``ref`` 是 :class:`web.protocol.rest_models.UserInputAttachment` 形状的
    dict（含 ``asset_id`` / ``kind`` / ``mime_type`` 等字段）。

    返回 ``None`` 表示无法还原——调用方应**跳过**该条（建议 warning log）
    而不是抛错，保持向后兼容：

    - ``kind`` 未支持（如未来 video / file 出现在老节点的 jsonl）→ ``None``
    - 必需字段缺失（``asset_id`` / ``kind`` / ``mime_type``）→ ``None``
    - ``image`` 但 ``mime_type`` 不在 :data:`web.uploads.registry.EXT_BY_MIME`
      白名单 → ``None``

    Args:
        ref: 单条 attachment ref dict。
        thread_id: 当前 thread id（与 storage 物理路径绑定，``ref`` 自身
            不含 thread_id，必须由调用方传入）。
        storage: 资产 IO 抽象，注入到 :class:`ImageMediaPart` 中用于 lazy 读 bytes。

    Returns:
        :class:`ImageMediaPart` 实例 或 ``None``。

    支持的 ``kind``：``"image"``。未来 ``"video"`` / ``"file"`` 在此函数加分支。
    """

    asset_id = ref.get("asset_id")
    kind = ref.get("kind")
    mime_type = ref.get("mime_type")

    # 必需字段缺失 → 跳过
    if not isinstance(asset_id, str) or not asset_id:
        return None
    if not isinstance(kind, str) or not kind:
        return None
    if not isinstance(mime_type, str) or not mime_type:
        return None

    if kind == "image":
        ext = EXT_BY_MIME.get(mime_type)
        if ext is None:
            # 未知 / 非白名单 MIME → 调用方跳过
            return None
        return ImageMediaPart(
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type=mime_type,
            ext=ext,
            storage=storage,
        )

    # 未来 video / file 在这里加分支
    return None


def collect_media_parts_from_messages(
    messages: Sequence[Message],
    *,
    storage: AssetStorage,
    thread_id: str,
) -> list[MediaPart]:
    """从一组消息中提取所有 :class:`MediaPart`（batch 版本）。

    扫描 ``messages`` 中每条 user 消息的 ``metadata["attachments"]``，
    逐条调 :func:`build_media_part_from_metadata` 还原成 MediaPart 实例；
    `None`（无法还原）的条目自动跳过并 warning log。

    本函数是 assembly → provider 路径上的**唯一**入口，
    把"持久化字段名 ``attachments``"和"中间表示 ``MediaPart``"绑死在一处，
    provider 侧只消费 ``list[MediaPart]``，不再感知 metadata key 名。

    Args:
        messages: 已装配的消息列表（通常来自 :class:`prompting.input_assembler.AssembledInput.messages`）。
        storage: 资产 IO 抽象，注入到每个 MediaPart 中用于 lazy 读 bytes。
        thread_id: 当前 thread id（attachments dict 自身不含 thread_id，
            按"一轮对话 = 单 thread"的语义由调用方传入）。

    Returns:
        ``list[MediaPart]``，按消息顺序、附件顺序展平；无附件时返回空列表。

    设计：

    - 只扫 user 消息——assistant / tool 消息当前没有 attachments 语义
    - 容错 metadata 缺失 / 类型不对 / kind 未支持，全部 warning log + 跳过
    - 不抛错：保持端到端"图片有问题 → 退化到纯文本"的健壮行为
    """

    parts: list[MediaPart] = []
    for msg in messages:
        if msg.role != "user":
            continue
        metadata = msg.metadata or {}
        refs = metadata.get("attachments")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                _LOGGER.warning("skip malformed attachment ref (not dict): %r", ref)
                continue
            part = build_media_part_from_metadata(ref, thread_id=thread_id, storage=storage)
            if part is None:
                _LOGGER.warning(
                    "skip unrebuildable attachment ref: asset_id=%s kind=%s",
                    ref.get("asset_id"),
                    ref.get("kind"),
                )
                continue
            parts.append(part)
    return parts


@runtime_checkable
class MediaAdapter(Protocol):
    """provider 适配层 Protocol：把统一的 list[MediaPart] 映射为厂商 content blocks。

    每个 provider 一个实装（AnthropicMediaAdapter / OpenAIMediaAdapter / ...）。

    设计动机：
    - assembly 输出 list[MediaPart] 是与厂商无关的中间表示
    - adapter 负责把它转成具体厂商 content block 字段（image / image_url / ...）
    - 不支持的 kind 显式 fail（不 silent fallback），让上层显式拒绝并提示用户
    """

    def supports(self, kind: str) -> bool:
        """该 adapter 是否支持指定 kind。"""
        ...

    def to_blocks(self, parts: Sequence[MediaPart]) -> list[dict[str, Any]]:
        """把 MediaPart 列表转成 provider content block 字段列表。

        - 输入空列表 → 返回空列表
        - 输入含 unsupported kind → 抛 ValueError（不 silent skip）
        """
        ...


@dataclass(frozen=True)
class AnthropicMediaAdapter:
    """Anthropic Messages API 图片适配。

    Anthropic image block 规范::

        {
          "type": "image",
          "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "<base64-string>"
          }
        }

    详见 https://docs.anthropic.com/claude/reference/messages_post（image content block）。
    """

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def to_blocks(self, parts: Sequence[MediaPart]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for part in parts:
            if not self.supports(part.kind):
                raise ValueError(
                    f"AnthropicMediaAdapter unsupported kind: {part.kind!r} "
                    f"(asset_id={part.asset_id})"
                )
            data = base64.standard_b64encode(part.load_bytes()).decode("ascii")
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime_type,
                        "data": data,
                    },
                }
            )
        return blocks


@dataclass(frozen=True)
class OpenAIMediaAdapter:
    """OpenAI vision 适配 —— **stub（Phase 2 实装）**。

    OpenAI 规范见 https://platform.openai.com/docs/guides/vision，
    形如::

        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,<base64>",
            "detail": "auto"
          }
        }

    本期（claude-image-paste-e2e Phase 1）**只跑 Anthropic 链路**，
    OpenAI stub 仅占位，防止 §4 assembly 调用方误以为 OpenAI 已实装。

    TODO（Phase 2 / provider-media-adapters Phase 2 task）：填充 to_blocks 实装。
    """

    def supports(self, kind: str) -> bool:
        return False  # Phase 1 不支持任何 kind

    def to_blocks(self, parts: Sequence[MediaPart]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "OpenAIMediaAdapter is a Phase 1 stub; vision support pending "
            "(see claude-image-paste-e2e Phase 2)"
        )
