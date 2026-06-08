"""claude_code 通道附件前缀构造器（claude-code-channel-image-paste §B）。

把 ``list[UserInputAttachment]`` 转成 Claude Code CLI 能识别的 ``@<path>``
prompt 前缀。SDK subprocess 端 ``@anthropic-ai/claude-code`` 的
``attachments.ts::extractAtMentionedFiles`` 会从 prompt 内容 grep ``@<path>``
引用，交给 ``FileReadTool`` 自动读图 → 转 base64 inline 进 Anthropic
Messages API 的 image block。

设计要点：

- **单一职责**：只构造 prefix 字符串，**不读文件 bytes**（lazy 由 SDK 内置
  ``FileReadTool`` 完成）。
- **拒绝散落函数式**：所有逻辑收拢到 :class:`AttachmentPrefixBuilder` 类内聚；
  未来视频附件 / 别的语法走同一类的扩展方法，不再开新工具函数。
- **路径反推**：通过 :class:`web.uploads.storage.AssetStorage.asset_path` 接口
  反推绝对路径（不重写路径计算）。
- **路径含空格**走 ``@"path with space.png"`` quoted 语法（Claude Code CLI 支持）。
- **失败容错**：路径不存在 / mime 未知 → warning log + 跳过该附件，不阻塞发送。

依赖：

- ``EXT_BY_MIME``（``web.uploads.registry``）做 mime → ext 映射，与上传链路单点真源
- ``AssetStorage``（``web.uploads.storage``）做路径反推
- ``UserInputAttachment``（``web.protocol.rest_models``）作为输入 DTO
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from web.uploads.registry import EXT_BY_MIME

if TYPE_CHECKING:
    from web.protocol.rest_models import UserInputAttachment
    from web.uploads.storage import AssetStorage

__all__ = ["AttachmentPrefixBuilder"]


_LOGGER = logging.getLogger(__name__)


class AttachmentPrefixBuilder:
    """把 attachments 列表转成 Claude Code CLI ``@file`` prompt 前缀。

    用法::

        builder = AttachmentPrefixBuilder(asset_storage)
        prefix = builder.build(attachments, thread_id="thread-xxx")
        full_prompt = prefix + user_text  # 喂给 client.query(prompt=...)

    输出格式示例::

        @/abs/path/to/img1.png
        @"/abs/path with space/img2.jpeg"
        @/abs/path/to/img3.webp

        <user text>

    无附件时 :meth:`build` 返回空字符串，调用方可直接 ``prefix + text`` 不破坏。
    """

    def __init__(self, asset_storage: AssetStorage) -> None:
        """注入 :class:`AssetStorage` 用于反推 asset 绝对路径。

        Args:
            asset_storage: 资产存储抽象。复用 web.uploads §2 单例。
        """
        self._storage = asset_storage

    def build(
        self,
        attachments: Sequence[UserInputAttachment] | None,
        *,
        thread_id: str,
    ) -> str:
        """构造 prompt 前缀。

        Args:
            attachments: 附件引用列表；``None`` / 空列表返回 ``""``。
            thread_id: 当前 thread id（与 ``AssetStorage`` 物理路径绑定）。

        Returns:
            形如 ``"@/abs/path1.png\\n@/abs/path2.jpg\\n\\n"`` 的前缀字符串。
            空列表 / 全部跳过时返回 ``""``。末尾保留一个空行与 user text 隔开。

        失败模式（全部 warning log + 跳过，不抛错）：

        - ``mime_type`` 不在 :data:`web.uploads.registry.EXT_BY_MIME`
        - 反推路径在磁盘不存在（资产被 GC / 误删 / thread_id 不匹配）
        """
        if not attachments:
            return ""

        lines: list[str] = []
        for att in attachments:
            line = self._render_attachment(att, thread_id=thread_id)
            if line is not None:
                lines.append(line)

        if not lines:
            return ""

        # 末尾追加空行让 prefix 与后续 user text 视觉隔开（CLI grep ``@<path>``
        # 不要求 inline，多行 OK）。
        return "\n".join(lines) + "\n\n"

    def _render_attachment(
        self,
        att: UserInputAttachment,
        *,
        thread_id: str,
    ) -> str | None:
        """构造单条 ``@<path>`` 行；失败返回 ``None`` 跳过。"""
        ext = EXT_BY_MIME.get(att.mime_type)
        if ext is None:
            _LOGGER.warning(
                "skip attachment %s: mime_type %r not in EXT_BY_MIME",
                att.asset_id,
                att.mime_type,
            )
            return None

        try:
            path = self._storage.asset_path(
                asset_id=att.asset_id,
                thread_id=thread_id,
                kind=att.kind,
                ext=ext,
            )
        except Exception as exc:
            _LOGGER.warning(
                "skip attachment %s: asset_path() failed: %s",
                att.asset_id,
                exc,
            )
            return None

        if not path.is_file():
            _LOGGER.warning(
                "skip attachment %s: file not found at %s",
                att.asset_id,
                path,
            )
            return None

        path_str = str(path)
        # CLI ``@file`` 语法解析（other/claude-code-main/src/utils/attachments.ts::
        # extractAtMentionedFiles）：
        # - quoted regex ``@"([^"]+)"`` 接受任意非 ``"`` 字符（含空格/冒号/反斜杠）
        # - regular regex ``@([^\s]+)\b`` 到 word boundary 停；Windows 路径含 ``:``
        #   和 ``\`` 虽可匹配但接近正则歧义边界，**强制走 quoted 更稳**。
        #
        # 走 quoted 的触发条件（任一即走）：
        # 1. 含空白字符（含中文全角空格、tab、换行等）
        # 2. 含 ``"`` 字面引号
        # 3. 含 ``\``（Windows 反斜杠分隔符 / Unix 文件名理论可含但极罕见）
        # 4. 含 ``:`` 但不在首尾（Windows ``C:\...`` 的 drive letter；macOS / Linux
        #    路径理论可含 ``:`` 但极罕见）
        #
        # Unix 纯 ASCII 路径（无空格 / 反斜杠 / 冒号）走 regular，保持原行为。
        needs_quote = (
            any(ch.isspace() for ch in path_str)
            or '"' in path_str
            or "\\" in path_str
            or ":" in path_str
        )
        if needs_quote:
            # 内嵌双引号转义；CLI quoted regex 接受 ``[^"]+``，转义 ``"`` 后内容
            # 仍能被正则匹配（regex 不识别 ``\\"``，但路径里出现真实 ``"`` 极罕见，
            # 这里转义优先保护 prompt 解析层不破坏）。
            escaped = path_str.replace('"', '\\"')
            return f'@"{escaped}"'
        return f"@{path_str}"
