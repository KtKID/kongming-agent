"""多模态媒体输入的宿主无关协议。

本模块只定义跨模块共享的媒体 contract：

- ``AttachmentKind`` / ``AttachmentStatus``：附件类型与状态字面量。
- ``IMAGE_EXT_BY_MIME``：图片 MIME 到扩展名的统一映射。
- ``AssetBytesReader``：按资产坐标读取 bytes 的最小 IO 协议。
- ``MediaPart`` / ``ImageMediaPart``：provider 消费的媒体中间表示。
- ``build_media_part_from_metadata`` / ``collect_media_parts_from_messages``：
  从 ``Message.metadata["attachments"]`` 还原媒体中间表示。

具体资产存储、上传校验、HTTP 路由属于宿主层实现，provider 只消费这里的
协议与中间表示。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from core.message import Message

AttachmentKind = Literal["image", "video", "file"]
AttachmentStatus = Literal["ready", "processing", "failed"]

IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class AssetBytesReader(Protocol):
    """资产 bytes 读取协议。

    宿主层可用本地文件、对象存储、内存缓存等任意实现满足该协议。
    """

    def read_asset_bytes(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
        ext: str,
    ) -> bytes:
        """按资产坐标读取原始 bytes。"""
        ...


@runtime_checkable
class MediaPart(Protocol):
    """provider 消费的统一媒体中间表示。"""

    @property
    def kind(self) -> AttachmentKind: ...

    @property
    def asset_id(self) -> str: ...

    @property
    def mime_type(self) -> str: ...

    def load_bytes(self) -> bytes: ...


@dataclass(frozen=True)
class ImageMediaPart:
    """图片媒体中间表示，按需通过 ``reader`` 读取 bytes。"""

    asset_id: str
    thread_id: str
    mime_type: str
    ext: str
    reader: AssetBytesReader

    @property
    def kind(self) -> AttachmentKind:
        """图片实装固定返回 ``"image"``。"""

        return "image"

    def load_bytes(self) -> bytes:
        """通过 ``reader`` 读取图片 bytes。"""

        return self.reader.read_asset_bytes(
            asset_id=self.asset_id,
            thread_id=self.thread_id,
            kind="image",
            ext=self.ext,
        )


def build_media_part_from_metadata(
    ref: dict[str, Any],
    *,
    thread_id: str,
    reader: AssetBytesReader,
) -> MediaPart | None:
    """从 attachment ref dict 还原成 ``MediaPart``。

    字段缺失、未知 kind、未知 MIME 均返回 ``None``，调用方负责跳过并记录
    warning，保证损坏附件退化为纯文本输入。
    """

    asset_id = ref.get("asset_id")
    kind = ref.get("kind")
    mime_type = ref.get("mime_type")

    if not isinstance(asset_id, str) or not asset_id:
        return None
    if not isinstance(kind, str) or not kind:
        return None
    if not isinstance(mime_type, str) or not mime_type:
        return None

    if kind == "image":
        ext = IMAGE_EXT_BY_MIME.get(mime_type)
        if ext is None:
            return None
        return ImageMediaPart(
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type=mime_type,
            ext=ext,
            reader=reader,
        )

    return None


def collect_media_parts_from_messages(
    messages: Sequence[Message],
    *,
    reader: AssetBytesReader,
    thread_id: str,
) -> list[MediaPart]:
    """从 user 消息附件 metadata 中提取 ``MediaPart`` 列表。"""

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
            part = build_media_part_from_metadata(ref, thread_id=thread_id, reader=reader)
            if part is None:
                _LOGGER.warning(
                    "skip unrebuildable attachment ref: asset_id=%s kind=%s",
                    ref.get("asset_id"),
                    ref.get("kind"),
                )
                continue
            parts.append(part)
    return parts


__all__ = [
    "IMAGE_EXT_BY_MIME",
    "AssetBytesReader",
    "AttachmentKind",
    "AttachmentStatus",
    "ImageMediaPart",
    "MediaPart",
    "build_media_part_from_metadata",
    "collect_media_parts_from_messages",
]
