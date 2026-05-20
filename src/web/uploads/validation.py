"""上传请求校验：MIME 白名单 + size 限制 + thread 归属 + 登录态。

把所有 "上传时该拒绝什么" 的规则集中到一个类，
controller 只调 ``validator.validate_image_upload(...)``，校验失败抛
:class:`UploadValidationError`（controller 捕获后转 HTTP 4xx）。

设计动机：
    - **规则集中、动作单一**：MIME / size / thread 三类校验不散落到 controller 内 if 块，
      新增 video 上传时加一个 ``validate_video_upload`` 方法即可，不动 controller。
    - **真源对齐**：``ALLOWED_IMAGE_MIME_TYPES`` 通过 ``EXT_BY_MIME.keys()`` 派生，
      避免 registry / validator 两边各写一份白名单（同步漂移源）。
    - **登录态分层**：登录态由 FastAPI ``Depends`` 在路由层校验；本类只校验
      "已登录用户对这个 thread 是否有权上传"——单用户工具，无 owner 字段时
      step 4 自动跳过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from web.uploads.registry import EXT_BY_MIME  # 共享 MIME 白名单（单点真源）
from web.uploads.storage import AttachmentKind  # noqa: F401  # public re-export hint

if TYPE_CHECKING:
    # 使用 Protocol 而非具体 ThreadManager 类：
    # 1. routers 通过 ``web.uploads.validation`` 间接触达 ``web.thread_manager``
    #    会被 import-linter Contract 6 拦截（web.routers 不可传递依赖 host/
    #    executors/safety/...）。
    # 2. Protocol 是 ``web.types`` 暴露的最小子集，已包含 ``list_threads``
    #    （本类实际只用这一个方法），单测注入 fake 也更简单。
    from web.types import ThreadManagerProtocol

__all__ = [
    "ALLOWED_IMAGE_MIME_TYPES",
    "MAX_IMAGE_SIZE_BYTES",
    "MediaUploadValidator",
    "UploadValidationError",
]


#: Phase 1 硬限制（claude-image-paste-e2e README §"硬限制"段）。
#:
#: 单图 5MB 上限：>5MB 拒绝 + 提示用户压缩。未来按 thread 配置调高时改成
#: 注入参数，不在调用方散落 magic number。
MAX_IMAGE_SIZE_BYTES: int = 5 * 1024 * 1024  # 5 MiB

#: 允许的 image MIME 白名单。
#:
#: 与 :data:`web.uploads.registry.EXT_BY_MIME` 的 keys 一致（派生，**不**单写
#: 一份），单独 export 便于 controller / 前端 hook 共享。``frozenset`` 防止
#: 调用方意外 mutation。
ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset(EXT_BY_MIME.keys())


class UploadValidationError(Exception):
    """校验失败 —— controller 捕获后转 HTTP 4xx。

    Attributes:
        code: 机器可读的错误码，与前端 toast 文案对应。
            当前定义的 code：

            - ``"mime_not_allowed"``：MIME 不在白名单。
            - ``"empty_payload"``：``size_bytes <= 0``。
            - ``"too_large"``：``size_bytes > MAX_IMAGE_SIZE_BYTES``。
            - ``"thread_not_found"``：thread metadata 不存在。
            - ``"thread_not_owned"``：thread 归属其他用户（仅当
              ThreadMetadata 提供 owner 字段时使用，v0.1 暂未启用）。
        message: 人类可读的错误信息。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MediaUploadValidator:
    """媒体上传校验器（无状态）。

    通过注入 :class:`ThreadManagerProtocol` 做 thread 存在性 + 归属校验。
    复用 :meth:`ThreadManagerProtocol.list_threads` 扫盘结果，不为本任务在
    ThreadManager 上新增方法。

    扩展点：未来加 video 上传时新增 :meth:`validate_video_upload` 即可，
    不动现有方法签名。
    """

    def __init__(self, thread_manager: ThreadManagerProtocol) -> None:
        """初始化校验器。

        Args:
            thread_manager: 用于校验 thread 是否存在 + 是否归属当前用户。
        """

        self._thread_manager = thread_manager

    def validate_image_upload(
        self,
        *,
        thread_id: str,
        mime_type: str,
        size_bytes: int,
        user_email: str,
    ) -> None:
        """校验图片上传请求。

        校验项**按顺序**（先快后慢，避免无效扫盘）：

        1. ``mime_type`` 在 :data:`ALLOWED_IMAGE_MIME_TYPES` 中
           （code=``"mime_not_allowed"``）。
        2. ``0 < size_bytes <= MAX_IMAGE_SIZE_BYTES``
           （code=``"empty_payload"`` 或 ``"too_large"``）。
        3. ``thread_id`` 在 :meth:`ThreadManager.list_threads` 中存在
           （code=``"thread_not_found"``）。
        4. （可选）thread 归属 ``user_email`` —— 若 ``ThreadMetadata`` 有
           ``owner_email`` / ``user_email`` 字段则校验，没有则跳过
           （单用户工具，v0.1 不强制；future 多用户开关由此处启用）。

        登录态本身不在这里校验——它由 FastAPI ``Depends`` 在路由层负责。
        本方法的 ``user_email`` 入参约定为"已登录用户的 email"，仅用于
        归属判断。

        Args:
            thread_id: 上传目标 thread 的 ID。
            mime_type: 浏览器上报的 MIME（``image/png`` 等）。
            size_bytes: 上传 payload 字节数。
            user_email: 已登录用户 email（路由层注入）。

        Raises:
            UploadValidationError: 任一校验项失败。
        """

        # 1. MIME 白名单（O(1) set 查询，最便宜）
        if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise UploadValidationError(
                code="mime_not_allowed",
                message=(
                    f"mime_type {mime_type!r} not allowed; "
                    f"expected one of {sorted(ALLOWED_IMAGE_MIME_TYPES)}"
                ),
            )

        # 2. size 限制（早于 thread 校验：grep 整盘前先排除明显错请求）
        if size_bytes <= 0:
            raise UploadValidationError(
                code="empty_payload",
                message=f"size_bytes must be positive, got {size_bytes}",
            )
        if size_bytes > MAX_IMAGE_SIZE_BYTES:
            raise UploadValidationError(
                code="too_large",
                message=(
                    f"size_bytes {size_bytes} exceeds limit "
                    f"{MAX_IMAGE_SIZE_BYTES} ({MAX_IMAGE_SIZE_BYTES // (1024 * 1024)} MiB)"
                ),
            )

        # 3. thread 存在性（list_threads 触发扫盘，最贵；放最后）
        # ThreadManager 没有专用 ``get_thread(thread_id)`` 方法；``get_cell``
        # 只查"已 boot 的活 cell"会漏 idle-evicted 但仍存在的 thread，
        # 因此用 ``list_threads`` 做权威判断。
        threads = self._thread_manager.list_threads()
        target = next((t for t in threads if t.id == thread_id), None)
        if target is None:
            raise UploadValidationError(
                code="thread_not_found",
                message=f"thread {thread_id!r} not found",
            )

        # 4. thread 归属（若 ThreadMetadata 有 owner 字段才校验）
        # 当前 v10 ThreadMetadata 无 owner_email / user_email 字段
        # （单用户工具）。预留分支：未来加多用户时只需把字段名加进 attr 列表。
        owner_attrs = ("owner_email", "user_email")
        for attr in owner_attrs:
            owner = getattr(target, attr, None)
            if owner is None:
                continue
            # 找到一个归属字段就用它判断；空字符串视为"未指定归属"，放行。
            if owner and owner != user_email:
                raise UploadValidationError(
                    code="thread_not_owned",
                    message=(
                        f"thread {thread_id!r} owned by {owner!r}, current user {user_email!r}"
                    ),
                )
            break
