"""上传 API 路由：images 上传 + 资产读取（v0.1.6 media-upload-shell）。

端点：

- ``POST /api/uploads/images``       — multipart file upload；返回
  :class:`UserInputAttachment`，前端直接塞进
  :class:`web.protocol.ws_frames.UserInputFrame.attachments`。
- ``GET  /api/uploads/{asset_id}``   — 资产 bytes（``<img src>`` 直接消费）；
  thread 归属由 path 反推 + ``ThreadManager.list_threads`` 校验。

设计要点：

- **路由层只做 HTTP 入站/出站**：业务逻辑全部委托给
  :class:`web.uploads.storage.AssetStorage` /
  :class:`web.uploads.registry.AssetRegistry` /
  :class:`web.uploads.validation.MediaUploadValidator` 三件套。新增视频上传时
  只在 validator 加 ``validate_video_upload`` + 这里加 ``POST /videos`` 路由，
  storage/registry 不动。
- **登录态**：受保护 ``/api/*`` 由全局 :class:`web.auth.AuthMiddleware` 兜底，
  路由函数不再重复 Depends；middleware 已经把
  :class:`web.auth.SessionTokenPayload` 写到 ``request.state.session_payload``，
  本路由透传 ``payload.user_id`` 作为 ``user_email`` 入参（v0.1.5 单用户
  ``user_id="default"``；validator 在没有 owner 字段时自动跳过归属判断）。
- **依赖单例**：``AssetStorage`` / ``AssetRegistry`` / ``MediaUploadValidator``
  通过 module-level ``lru_cache`` getter 注入（与仓库其他 router 走
  ``request.app.state.*`` 的惯例对齐，不引入 FastAPI ``Depends``，便于 #9
  任务把单例预挂到 ``app.state.*`` 后无缝切换）。
- **错误映射**：:class:`web.uploads.validation.UploadValidationError` 按 code
  翻译 HTTP 状态码（415 / 413 / 400 / 404），其它 IO 异常一律 500。
- **MIME 检测**：当前信 ``file.content_type``（client 报）；二次确认（magic
  bytes）列入 TODO，不在本任务范围。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile

# UserInputAttachment 必须是运行时 import：FastAPI 在 include_router 时通过
# ``get_type_hints(func)`` + ``func.__globals__`` 解析 endpoint 的返回类型
# ForwardRef，TYPE_CHECKING 下的符号在 ``__globals__`` 里不存在 → TypeAdapter
# 处于 unbuilt 状态，任何成功响应序列化都会 500。
from web.protocol.rest_models import UserInputAttachment
from web.uploads.registry import EXT_BY_MIME, AssetRegistry
from web.uploads.storage import AssetStorage
from web.uploads.validation import (
    MAX_IMAGE_SIZE_BYTES,
    MediaUploadValidator,
    UploadValidationError,
)

if TYPE_CHECKING:
    from web.auth import SessionTokenPayload
    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


# ---------------------------------------------------------------------------
# 错误码 → HTTP 状态码映射
# ---------------------------------------------------------------------------

#: :class:`UploadValidationError.code` → HTTP status 映射。新增 code 时统一在
#: 这里加一条，避免散落到各路由函数。
_VALIDATION_STATUS_BY_CODE: dict[str, int] = {
    "mime_not_allowed": 415,
    "too_large": 413,
    "empty_payload": 400,
    "thread_not_found": 404,
    "thread_not_owned": 403,
}


#: ``asset_id`` 必须是 uuid4 hex（32 字符 / 0-9a-f）。与
#: :meth:`AssetRegistry.new_asset_id` 输出形态绑死。任何不匹配的输入（含
#: glob 元字符 ``*`` / ``?`` / ``[``）一律 400 拒绝，避免在 ``Path.glob`` /
#: ``Path.rglob`` 调用处展开为通配模式越权读取其他 thread 的图片。
_ASSET_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _raise_validation_http(exc: UploadValidationError) -> None:
    """把 :class:`UploadValidationError` 翻译成 :class:`HTTPException`。

    detail 同时带 ``code`` + ``message``，方便前端做 toast 文案分支。
    """

    status = _VALIDATION_STATUS_BY_CODE.get(exc.code, 400)
    raise HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# 依赖单例获取
#
# 优先级：``request.app.state.<key>`` > module-level lazy singleton。
# #9 task 把 storage/registry/validator 预挂到 ``app.state`` 后，自动切到注入
# 路径；当前任务没改 app.py，所以仍走 module-level fallback。
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _shared_storage() -> AssetStorage:
    """全进程共享的 :class:`AssetStorage` 单例（lazy）。"""

    return AssetStorage()


@lru_cache(maxsize=1)
def _shared_registry() -> AssetRegistry:
    """全进程共享的 :class:`AssetRegistry` 单例（无状态，share 安全）。"""

    return AssetRegistry()


def get_storage(request: Request) -> AssetStorage:
    """获取 :class:`AssetStorage` 实例。"""

    storage = getattr(request.app.state, "asset_storage", None)
    if isinstance(storage, AssetStorage):
        return storage
    return _shared_storage()


def get_registry(request: Request) -> AssetRegistry:
    """获取 :class:`AssetRegistry` 实例。"""

    registry = getattr(request.app.state, "asset_registry", None)
    if isinstance(registry, AssetRegistry):
        return registry
    return _shared_registry()


def get_validator(request: Request) -> MediaUploadValidator:
    """获取 :class:`MediaUploadValidator` 实例。

    优先级：``request.app.state.upload_validator`` > 用
    ``request.app.state.thread_manager`` 现场构造。``thread_manager`` 缺失抛
    500（``create_app`` 装配阶段必塞，这里仅防御）。
    """

    validator = getattr(request.app.state, "upload_validator", None)
    if isinstance(validator, MediaUploadValidator):
        return validator
    thread_manager: ThreadManagerProtocol | None = getattr(
        request.app.state, "thread_manager", None
    )
    if thread_manager is None:
        raise HTTPException(
            status_code=500,
            detail="thread_manager not configured; cannot validate uploads",
        )
    return MediaUploadValidator(thread_manager)


def _current_user_id(request: Request) -> str:
    """从中间件写入的 ``request.state.session_payload`` 取 ``user_id``。

    middleware 已挡未登录请求，路由函数走到这里 ``payload`` 必然存在；防御
    分支兜底 500（与 :class:`AuthMiddleware` 缺装配同义）。
    """

    payload: SessionTokenPayload | None = getattr(request.state, "session_payload", None)
    if payload is None:
        raise HTTPException(
            status_code=500,
            detail="session_payload missing on authenticated request",
        )
    return payload.user_id


# ---------------------------------------------------------------------------
# 路由实现
# ---------------------------------------------------------------------------


@router.post("/images")
async def upload_image(
    request: Request,
    file: UploadFile,
    thread_id: str = Form(...),
) -> UserInputAttachment:
    """上传一张图片，返回前端可直接放进 ``UserInputFrame.attachments`` 的引用。

    步骤：

    1. 中间件已校验登录态；这里取 ``user_id`` 透传给 validator。
    2. 一次性 ``file.read()`` 全量进内存：5 MiB 上限由 validator 把关，
       上限内的内存占用可接受；流式上传 / 大文件分片是未来视频上传的需求，
       本任务不引入。
    3. ``validator.validate_image_upload(...)`` 校验 MIME / size / thread。
    4. ``registry.build_asset(...)`` 构造 :class:`AttachmentAsset`，自动算
       sha256 / asset_id / storage_path / preview_url / created_at。
    5. ``storage.write_asset(...)`` 原子写 bytes + 同目录 ``<asset_id>.json``
       元数据。
    6. ``registry.to_user_input_attachment(...)`` 投影成协议 DTO 返回。
    """

    storage = get_storage(request)
    registry = get_registry(request)
    validator = get_validator(request)

    user_id = _current_user_id(request)

    raw_mime = (file.content_type or "").strip()
    # NOTE: 二次确认（magic bytes / Pillow header sniff）列入 TODO，避免恶意
    # client 把 .exe 标成 ``image/png``。当前白名单 + size + 真实写盘后下游
    # 用 Pillow 解析失败也能兜底。

    # P0-3 (R2 boundary fix)：用 ``UploadFile.size``（FastAPI 在 multipart 阶段
    # 已计算好）先做一道 declared-size 早拒绝，避免 ``await file.read()`` 把超
    # 大 payload 整个塞进内存炸 RSS。``file.size`` 在某些 transport 下可能为
    # ``None``，所以仍保留 read 后的 ``len(payload)`` 校验做最后一道（防 client
    # 谎报 size）。
    declared_size = file.size
    if declared_size is not None and declared_size > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "too_large",
                "message": (
                    f"file exceeds {MAX_IMAGE_SIZE_BYTES} bytes (declared {declared_size})"
                ),
            },
        )

    payload = await file.read()
    size_bytes = len(payload)

    try:
        validator.validate_image_upload(
            thread_id=thread_id,
            mime_type=raw_mime,
            size_bytes=size_bytes,
            user_email=user_id,
        )
    except UploadValidationError as exc:
        _raise_validation_http(exc)

    asset = registry.build_asset(
        thread_id=thread_id,
        kind="image",
        mime_type=raw_mime,
        payload=payload,
    )
    ext = EXT_BY_MIME[raw_mime]

    try:
        storage.write_asset(
            asset_id=asset.asset_id,
            thread_id=asset.thread_id,
            kind=asset.kind,
            ext=ext,
            payload=payload,
            metadata=asset.__dict__,
        )
    except OSError as exc:
        logger.exception("write_asset failed: thread=%s asset=%s", thread_id, asset.asset_id)
        raise HTTPException(
            status_code=500,
            detail=f"failed to persist asset: {exc.__class__.__name__}",
        ) from exc

    return registry.to_user_input_attachment(asset)


@router.get("/{asset_id}")
async def serve_asset(asset_id: str, request: Request) -> Response:
    """返回资产 bytes（前端 ``<img src>`` 直接消费）。

    实现策略：

    1. 中间件已校验登录态。
    2. asset_id 合法性：禁止 ``..`` / ``/`` 防 path traversal。
    3. 在 ``<base_dir>/images/`` 下 ``rglob("<asset_id>.*")`` 找文件；扫盘只
       覆盖图片目录，v0.1.5 用户量小，单次响应时间可接受；走索引文件 / db
       是未来扩展点。
    4. 从 path 反推 ``thread_id``（父目录名）→ 校验该 thread 在
       :class:`ThreadManager.list_threads` 中存在（防止 thread 删除后资产被
       继续访问 / 防 path traversal 二次确认）。
    5. 读同目录 ``<asset_id>.json`` 元数据拿 ``mime_type`` 返回。

    错误：

    - 400 asset_id 不是合法 uuid4 hex（含 glob 元字符 ``*``/``?``/``[``）
    - 422 asset_id 包含 path traversal（``..`` / ``/``）
    - 404 asset 不存在 / 所属 thread 已删除
    - 500 IO 错误 / 元数据损坏
    """

    if not asset_id or "/" in asset_id or ".." in asset_id or "\\" in asset_id:
        raise HTTPException(status_code=422, detail="invalid asset_id")

    # P0-1 (R2 boundary fix)：path-traversal 校验只挡 ``/`` / ``..`` / ``\\``，
    # 不挡 glob 元字符 ``*`` / ``?`` / ``[``。强制 uuid4 hex 形态校验，与
    # :meth:`AssetRegistry.new_asset_id` 输出绑死；任何非 32 位 0-9a-f 都拒
    # 绝。彻底关掉下方 ``rglob`` 展开成跨 thread 通配读取的口子。
    if not _ASSET_ID_PATTERN.fullmatch(asset_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_asset_id",
                "message": "asset_id must be 32-char lowercase hex (uuid4)",
            },
        )

    storage = get_storage(request)

    images_dir: Path = storage.base_dir / "images"
    if not images_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")

    # P0-1 (R2 boundary fix)：弃用 ``rglob(f"{asset_id}.*")`` ——
    # 即便 asset_id 已被 :data:`_ASSET_ID_PATTERN` 锁死，``rglob`` 仍属于 glob
    # 接口（``*`` 在第二段 ``.*`` 是合法 glob），稳妥起见统一改成显式
    # ``Path.is_file()`` 字面拼接。遍历 ``images/<thread>/<asset_id>.<ext>``
    # 4 个白名单 ext × 现有 thread 子目录，命中即停。v0.1.5 单用户场景 thread
    # 数量有限（实测 < 100），单次响应时间可接受；大规模场景建议走 SQLite
    # 索引（未来扩展点）。
    found: Path | None = None
    if images_dir.is_dir():
        for thread_dir in images_dir.iterdir():
            if not thread_dir.is_dir():
                continue
            for ext in EXT_BY_MIME.values():
                candidate = thread_dir / f"{asset_id}{ext}"
                if candidate.is_file():
                    found = candidate
                    break
            if found is not None:
                break

    if found is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")

    # 反推 thread_id（父目录名）+ 校验 thread 仍存在。
    thread_id = found.parent.name
    thread_manager: ThreadManagerProtocol | None = getattr(
        request.app.state, "thread_manager", None
    )
    if thread_manager is None:
        raise HTTPException(
            status_code=500,
            detail="thread_manager not configured; cannot verify asset ownership",
        )
    threads = thread_manager.list_threads()
    if not any(t.id == thread_id for t in threads):
        # thread 已删除，资产应在 :meth:`AssetStorage.delete_thread_assets`
        # 时一并清理；走到这里说明 GC 未执行或文件残留。统一 404 不暴露
        # 内部状态。
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")

    try:
        meta = storage.read_asset_metadata(
            asset_id=asset_id,
            thread_id=thread_id,
            kind="image",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"asset metadata missing: {asset_id}") from exc
    except OSError as exc:
        logger.exception("read_asset_metadata failed: asset=%s", asset_id)
        raise HTTPException(
            status_code=500,
            detail=f"failed to read asset metadata: {exc.__class__.__name__}",
        ) from exc

    mime_type = str(meta.get("mime_type") or "application/octet-stream")

    try:
        body = found.read_bytes()
    except OSError as exc:
        logger.exception("read asset bytes failed: asset=%s", asset_id)
        raise HTTPException(
            status_code=500,
            detail=f"failed to read asset: {exc.__class__.__name__}",
        ) from exc

    return Response(content=body, media_type=mime_type)


__all__ = [
    "get_registry",
    "get_storage",
    "get_validator",
    "router",
]
