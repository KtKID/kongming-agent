"""codex 通道附件 CLI args 构造器（codex-channel-image-paste §1）。

把 ``list[UserInputAttachment]`` 转成 Codex CLI 原生 ``--image <path>`` 参数列表。
Codex CLI 接收后，Rust 端 ``into_data_url()``（``other/codex/codex-rs/utils/image
/src/lib.rs:34``）自动把本地路径转 base64 data URL，注入 OpenAI Responses API
的 ``ContentItem::InputImage``。

设计要点（高内聚低耦合）：

- **单一职责**：只构造 ``--image`` flag 列表，**不读 bytes**（lazy 由 Codex CLI
  Rust 端完成），**不直接调 subprocess**（service 层职责）。
- **拒绝散落函数式**：所有逻辑收拢到 :class:`CodexImageCliArgsBuilder` 类内聚；
  ``service.py`` **不允许**散落拼 ``--image`` 字符串。未来视频附件 / 其他 flag
  扩展（如 ``--video``）走同一类的扩展方法。
- **路径反推**：通过 :class:`web.uploads.storage.AssetStorage.asset_path` 接口
  反推绝对路径（与 claude_code 通道 builder 同款）。
- **不需要 quoted 语法**：CLI args 用 ``list[str]`` 形式传给 ``asyncio
  .create_subprocess_exec``，shell **不再 tokenize**，路径可含任意字符
  （空格 / 中文 / 反斜杠 / 冒号）。**比 claude_code 通道的 quoted 处理更稳**。
- **路径不进 prompt**：subprocess args 不暴露给 LLM 上下文，路径只在
  Python ↔ Codex Rust 之间传递。
- **4 条输入防线**（任一不满足即跳过 + warning，不阻塞其他附件）：
  1. ``att.kind == "image"`` —— 拒 video / file 等未来 union 扩展
  2. ``att.status == "ready"`` —— 拒 uploading / failed（Composer 已守，
     但 builder 是入口最后一道防线）
  3. ``att.mime_type in EXT_BY_MIME`` —— 拒非白名单 mime
  4. ``storage.asset_path(...)`` 反推路径在磁盘存在
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from web.uploads.registry import EXT_BY_MIME

if TYPE_CHECKING:
    from web.protocol.rest_models import UserInputAttachment
    from web.uploads.storage import AssetStorage

__all__ = ["CodexImageCliArgsBuilder"]


_LOGGER = logging.getLogger(__name__)


class CodexImageCliArgsBuilder:
    """把 attachments 列表转成 Codex CLI ``--image`` flag args 列表。

    用法::

        builder = CodexImageCliArgsBuilder(asset_storage)
        image_args = builder.build(attachments, thread_id="thread-xxx")
        # ["--image", "/abs/path1.png", "--image", "/abs/path2.jpg"]
        full_args = base_args + image_args + [command]
        await asyncio.create_subprocess_exec(*full_args, ...)

    输出格式示例：

    - 空列表 / 全部跳过 → ``[]``
    - 1 图 → ``["--image", "/abs/path.png"]``
    - N 图 → ``["--image", "/p1", "--image", "/p2", ..., "--image", "/pN"]``

    与 claude_code 通道 ``AttachmentPrefixBuilder`` 的关键区别：

    - 输出 ``list[str]``（flag 序列）而非 ``str``（prompt 前缀）
    - 不需要 quoted 处理（CLI args list 不经 shell tokenize）
    - 路径不进 prompt 上下文
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
    ) -> list[str]:
        """构造 ``--image <path>`` CLI flag 列表。

        Args:
            attachments: 附件引用列表；``None`` / 空列表 / 全部跳过 → 返回 ``[]``。
            thread_id: 当前 thread id（与 ``AssetStorage`` 物理路径绑定）。

        Returns:
            ``["--image", path1, "--image", path2, ...]`` 形式 flag 序列。
            调用方 ``args.extend(builder.build(...))`` 总是安全（空 list 无副作用）。

        失败模式（4 条输入防线，任一不满足即跳过 + warning，**不阻塞**其他附件）：

        1. ``att.kind != "image"`` → 跳过（拒未来 video / file 扩展）
        2. ``att.status != "ready"`` → 跳过（拒 uploading / failed 状态）
        3. ``att.mime_type`` 不在 :data:`web.uploads.registry.EXT_BY_MIME` → 跳过
        4. 反推路径在磁盘不存在 → 跳过（asset 被 GC / 误删 / thread_id 不匹配）
        """
        if not attachments:
            return []

        args: list[str] = []
        for att in attachments:
            path_str = self._render_arg(att, thread_id=thread_id)
            if path_str is None:
                continue
            args.extend(["--image", path_str])
        return args

    def _render_arg(
        self,
        att: UserInputAttachment,
        *,
        thread_id: str,
    ) -> str | None:
        """构造单个附件的 path 字符串；失败返回 ``None`` 跳过。

        4 条输入防线按下面顺序检查，任一失败即返回 None 不报错。
        """
        # 防线 1：kind 必须是 image
        if att.kind != "image":
            _LOGGER.warning(
                "skip attachment %s: kind=%r is not image (codex builder only handles image)",
                att.asset_id,
                att.kind,
            )
            return None

        # 防线 2：status 必须是 ready
        if att.status != "ready":
            _LOGGER.warning(
                "skip attachment %s: status=%r is not ready",
                att.asset_id,
                att.status,
            )
            return None

        # 防线 3：mime 必须在白名单
        ext = EXT_BY_MIME.get(att.mime_type)
        if ext is None:
            _LOGGER.warning(
                "skip attachment %s: mime_type %r not in EXT_BY_MIME",
                att.asset_id,
                att.mime_type,
            )
            return None

        # 防线 4：反推路径必须存在
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

        return str(path)
