"""provider 多模态 content block 适配器。

本模块只做 provider 格式转换：消费 ``core.contracts.MediaPart``，输出具体
厂商 content blocks。附件 ref 解析和资产 bytes 读取协议归属
``core.contracts.media``，宿主层负责提供读取实现。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.contracts import MediaPart

__all__ = [
    "AnthropicMediaAdapter",
    "MediaAdapter",
    "OpenAIMediaAdapter",
]


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
