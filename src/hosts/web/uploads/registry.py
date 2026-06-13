"""资产元数据登记 + asset_id 生成 + 内容哈希。

职责：

- 生成稳定唯一的 ``asset_id``（uuid4 hex）。
- 计算 payload sha256（去重 + 完整性校验）。
- 根据上传 bytes + 上下文构造 :class:`AttachmentAsset`。
- 检查 thread 归属（防止跨 thread 访问）。
- 提供 :class:`AttachmentAsset` → :class:`UserInputAttachment` 投影
  （去掉服务端字段）。

设计动机：把"asset 怎么生 + 怎么登记 + 怎么投影对外"集中到一处，
``UploadController`` 只关心 HTTP 入站/出站，``AssetStorage`` 只关心
物理 IO，三者各司其职。

约束：本模块**不感知存储 IO**——不 import :class:`AssetStorage`，由
``UploadController`` 编排 storage + registry 协作。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import TYPE_CHECKING

from hosts.web.uploads.storage import (
    AttachmentAsset,
    AttachmentKind,
    AttachmentStatus,
)

if TYPE_CHECKING:
    from hosts.web.protocol.rest_models import UserInputAttachment

__all__ = ["EXT_BY_MIME", "AssetRegistry", "compute_sha256"]


#: 允许的 image MIME → 文件扩展名映射。
#:
#: §8 validation 会 import 该常量做 MIME 白名单，避免规则散落。
EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def compute_sha256(payload: bytes) -> str:
    """计算 ``payload`` 的 SHA-256 hex digest。

    单独 export 是为了让 §10 测试和未来去重逻辑可以直接复用，无需实例化
    :class:`AssetRegistry`。
    """

    return hashlib.sha256(payload).hexdigest()


class AssetRegistry:
    """资产记录管理（生成 + 投影），不持久化状态。

    所有方法都是无状态计算（构造 / 哈希 / 投影 / 校验），不维护内存索引；
    真正的"读出 asset"由调用方走 :class:`AssetStorage` 文件系统完成。

    预留 ``__init__`` 给未来可能的 in-memory cache / 计数器扩展点；
    现阶段保持空实现以避免散落函数式调用。
    """

    def __init__(self) -> None:
        # 无状态；占位以保留扩展点。
        pass

    def new_asset_id(self) -> str:
        """生成 ``uuid4().hex``（32 字符无连字符，URL 安全）。"""

        return uuid.uuid4().hex

    def build_asset(
        self,
        *,
        thread_id: str,
        kind: AttachmentKind,
        mime_type: str,
        payload: bytes,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        status: AttachmentStatus = "ready",
    ) -> AttachmentAsset:
        """根据 ``payload`` + 上下文构造一份完整 :class:`AttachmentAsset`。

        字段填充规则：

        - ``asset_id``：:meth:`new_asset_id`。
        - ``size_bytes``：``len(payload)``。
        - ``sha256``：:func:`compute_sha256`。
        - ``storage_path``：``f"images/{thread_id}/{asset_id}{ext}"``，
          其中 ``ext`` 通过 :data:`EXT_BY_MIME` 反查 ``mime_type``。
          目前仅支持 image kind，所以路径前缀写死 ``images/``；未来
          ``"video"`` / ``"file"`` kind 接入时再拓展前缀策略。
        - ``preview_url``：``f"/api/uploads/{asset_id}"``，与 §7
          ``UploadController`` 的 GET 路由一致。
        - ``created_at``：``time.time()``（UNIX 秒，float）。

        :raises ValueError: ``mime_type`` 不在 :data:`EXT_BY_MIME` 白名单内。
        """

        if mime_type not in EXT_BY_MIME:
            raise ValueError(
                f"unsupported mime_type for build_asset: {mime_type!r}; "
                f"expected one of {sorted(EXT_BY_MIME)}"
            )

        asset_id = self.new_asset_id()
        ext = EXT_BY_MIME[mime_type]
        storage_path = f"images/{thread_id}/{asset_id}{ext}"
        preview_url = f"/api/uploads/{asset_id}"

        return AttachmentAsset(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(payload),
            width=width,
            height=height,
            duration_ms=duration_ms,
            sha256=compute_sha256(payload),
            storage_path=storage_path,
            preview_url=preview_url,
            status=status,
            created_at=time.time(),
        )

    def to_user_input_attachment(self, asset: AttachmentAsset) -> UserInputAttachment:
        """把服务端 :class:`AttachmentAsset` 投影为协议层
        :class:`UserInputAttachment`。

        去掉服务端独有字段（``thread_id`` / ``sha256`` / ``storage_path`` /
        ``created_at``），保留前后端共享的协议字段。

        用 ``model_validate`` 而非直接构造，确保 ``Literal`` / ``frozen`` /
        ``extra='forbid'`` 约束在投影时立刻校验，前后端协议漂移立即报错。
        """

        from hosts.web.protocol.rest_models import UserInputAttachment

        return UserInputAttachment.model_validate(
            {
                "asset_id": asset.asset_id,
                "kind": asset.kind,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "width": asset.width,
                "height": asset.height,
                "duration_ms": asset.duration_ms,
                "preview_url": asset.preview_url,
                "status": asset.status,
            }
        )

    def verify_thread_ownership(self, asset: AttachmentAsset, *, thread_id: str) -> bool:
        """校验 ``asset`` 归属指定 ``thread_id``，防止跨 thread 越权访问。"""

        return asset.thread_id == thread_id
