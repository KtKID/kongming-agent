"""上传链路 pytest（claude-image-paste-e2e §10 + §2 media-upload-shell）。

覆盖范围（4 层）：

1. :class:`web.uploads.storage.AssetStorage` 单元：路径计算、原子写、读取、
   删除。
2. :class:`web.uploads.registry.AssetRegistry` 单元：``new_asset_id`` 唯一性、
   ``build_asset`` 字段完整性、``to_user_input_attachment`` 投影脱敏、
   ``compute_sha256`` 稳定性、未知 MIME 拒绝。
3. :class:`web.uploads.validation.MediaUploadValidator` 单元：MIME / size /
   thread 三类错误码 + 正常路径。
4. ``POST /api/uploads/images`` + ``GET /api/uploads/{asset_id}`` 集成
   （FastAPI TestClient，最小 app）。

集成测试用 **最小 app** 方案：直接 import ``web.routers.uploads.router``
+ 自挂 fake :class:`AuthMiddleware`-equivalent ASGI middleware 注入
``request.state.session_payload``。避免拉起完整 :func:`web.app.create_app`
带来的副作用（DB、cookie serializer、ThreadManager 启动……）。

测试不允许 ``python -c`` / 一次性脚本（仓库约束）；所有用例都是可重跑的
pytest 用例。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from web.auth.middleware import SessionTokenPayload
from web.routers.uploads import router as uploads_router
from web.threads.metadata import ThreadMetadata
from web.uploads.registry import EXT_BY_MIME, AssetRegistry, compute_sha256
from web.uploads.storage import AssetStorage, AttachmentAsset
from web.uploads.validation import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_SIZE_BYTES,
    MediaUploadValidator,
    UploadValidationError,
)

# ---------------------------------------------------------------------------
# 公用 fixture
# ---------------------------------------------------------------------------


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def tmp_storage(tmp_path: Path) -> AssetStorage:
    """每个测试一个干净的 AssetStorage（独立 tmp_path）。"""

    return AssetStorage(base_dir=tmp_path / "uploads")


@pytest.fixture
def png_payload() -> bytes:
    """最小合法 PNG 字节串（带 magic header + 填充，> 16 字节）。"""

    return PNG_HEADER + b"\x00" * 64


def _make_thread_meta(thread_id: str) -> ThreadMetadata:
    now = time.time()
    return ThreadMetadata(
        id=thread_id,
        name="test-thread",
        preset_id="default",
        backend_kind="generic_chat",
        created_at=now,
        updated_at=now,
    )


class _FakeThreadManager:
    """最小 ThreadManagerProtocol 实现：只暴露 list_threads。

    其他 Protocol 方法没用到，运行时不调用就不会爆。``isinstance(...,
    ThreadManagerProtocol)`` 因为是 ``runtime_checkable`` Protocol，只检查
    属性 / 方法**存在**，不检查签名，所以这个 fake 足够。
    """

    def __init__(self, metas: list[ThreadMetadata]) -> None:
        self._metas = metas

    def list_threads(self) -> list[ThreadMetadata]:
        return list(self._metas)


@pytest.fixture
def fake_thread_manager() -> _FakeThreadManager:
    return _FakeThreadManager([_make_thread_meta("thread-aaaaaaaaaaaa")])


# ---------------------------------------------------------------------------
# §1 单元测试：AssetStorage
# ---------------------------------------------------------------------------


class TestAssetStorage:
    def test_asset_path_computation(self, tmp_storage: AssetStorage) -> None:
        path = tmp_storage.asset_path(
            asset_id="abc",
            thread_id="thread-x",
            kind="image",
            ext=".png",
        )
        # 形态：<base>/images/<thread>/<asset>.<ext>
        assert path.parent.name == "thread-x"
        assert path.parent.parent.name == "images"
        assert path.name == "abc.png"

    def test_write_and_read_round_trip(self, tmp_storage: AssetStorage, png_payload: bytes) -> None:
        metadata = {"asset_id": "id1", "mime_type": "image/png"}
        written = tmp_storage.write_asset(
            asset_id="id1",
            thread_id="thread-x",
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata=metadata,
        )
        assert written.is_file()
        assert (
            tmp_storage.read_asset_bytes(
                asset_id="id1",
                thread_id="thread-x",
                kind="image",
                ext=".png",
            )
            == png_payload
        )
        meta = tmp_storage.read_asset_metadata(
            asset_id="id1",
            thread_id="thread-x",
            kind="image",
        )
        assert meta == metadata
        assert tmp_storage.asset_exists(
            asset_id="id1",
            thread_id="thread-x",
            kind="image",
            ext=".png",
        )

    def test_atomic_write_no_tmp_leftover(
        self, tmp_storage: AssetStorage, png_payload: bytes
    ) -> None:
        tmp_storage.write_asset(
            asset_id="id1",
            thread_id="thread-x",
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata={"k": "v"},
        )
        thread_dir = tmp_storage.base_dir / "images" / "thread-x"
        leftovers = sorted(p.name for p in thread_dir.iterdir() if p.name.endswith(".tmp"))
        assert leftovers == []

    def test_delete_thread_assets_removes_dir(
        self, tmp_storage: AssetStorage, png_payload: bytes
    ) -> None:
        tmp_storage.write_asset(
            asset_id="id1",
            thread_id="thread-x",
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata={"k": "v"},
        )
        tmp_storage.write_asset(
            asset_id="id2",
            thread_id="thread-x",
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata={"k": "v"},
        )
        removed = tmp_storage.delete_thread_assets(thread_id="thread-x")
        # 2 资产 × (1 bytes + 1 json) = 4 文件
        assert removed == 4
        assert not (tmp_storage.base_dir / "images" / "thread-x").exists()
        with pytest.raises(FileNotFoundError):
            tmp_storage.read_asset_bytes(
                asset_id="id1",
                thread_id="thread-x",
                kind="image",
                ext=".png",
            )


# ---------------------------------------------------------------------------
# §2 单元测试：AssetRegistry
# ---------------------------------------------------------------------------


class TestAssetRegistry:
    def test_new_asset_id_unique(self) -> None:
        reg = AssetRegistry()
        ids = {reg.new_asset_id() for _ in range(1000)}
        assert len(ids) == 1000
        # uuid4 hex 都是 32 字符 + 十六进制
        for asset_id in ids:
            assert len(asset_id) == 32
            assert all(c in "0123456789abcdef" for c in asset_id)

    def test_build_asset_fields(self, png_payload: bytes) -> None:
        reg = AssetRegistry()
        asset = reg.build_asset(
            thread_id="thread-x",
            kind="image",
            mime_type="image/png",
            payload=png_payload,
        )
        assert asset.asset_id and len(asset.asset_id) == 32
        assert asset.thread_id == "thread-x"
        assert asset.kind == "image"
        assert asset.mime_type == "image/png"
        assert asset.size_bytes == len(png_payload)
        assert asset.sha256 == compute_sha256(png_payload)
        assert asset.preview_url == f"/api/uploads/{asset.asset_id}"
        assert asset.storage_path == f"images/thread-x/{asset.asset_id}.png"
        assert asset.status == "ready"
        # created_at 是合理 unix 秒
        assert asset.created_at > 0
        assert asset.created_at <= time.time() + 5

    def test_to_user_input_attachment_drops_server_fields(self, png_payload: bytes) -> None:
        reg = AssetRegistry()
        asset = reg.build_asset(
            thread_id="thread-x",
            kind="image",
            mime_type="image/png",
            payload=png_payload,
        )
        dto = reg.to_user_input_attachment(asset)
        dumped = dto.model_dump()
        # 协议字段必须保留
        assert dumped["asset_id"] == asset.asset_id
        assert dumped["mime_type"] == "image/png"
        assert dumped["preview_url"] == asset.preview_url
        assert dumped["status"] == "ready"
        # 服务端字段必须脱敏
        for forbidden in ("thread_id", "sha256", "storage_path", "created_at"):
            assert forbidden not in dumped, (
                f"server-side field {forbidden!r} leaked into UserInputAttachment"
            )

    def test_compute_sha256_stable(self) -> None:
        # 同样的 bytes → 同样的 hash
        a = compute_sha256(b"hello world")
        b = compute_sha256(b"hello world")
        assert a == b
        # 不同 bytes → 不同 hash
        c = compute_sha256(b"hello WORLD")
        assert a != c
        # 64 字符 hex
        assert len(a) == 64

    def test_build_asset_rejects_unknown_mime(self, png_payload: bytes) -> None:
        reg = AssetRegistry()
        with pytest.raises(ValueError, match="unsupported mime_type"):
            reg.build_asset(
                thread_id="thread-x",
                kind="image",
                mime_type="image/bmp",
                payload=png_payload,
            )

    def test_verify_thread_ownership(self) -> None:
        reg = AssetRegistry()
        asset = AttachmentAsset(
            asset_id="a",
            thread_id="thread-x",
            kind="image",
            mime_type="image/png",
            size_bytes=1,
        )
        assert reg.verify_thread_ownership(asset, thread_id="thread-x") is True
        assert reg.verify_thread_ownership(asset, thread_id="thread-y") is False


# ---------------------------------------------------------------------------
# §3 单元测试：MediaUploadValidator
# ---------------------------------------------------------------------------


class TestMediaUploadValidator:
    def test_validate_image_upload_happy_path(
        self, fake_thread_manager: _FakeThreadManager
    ) -> None:
        v = MediaUploadValidator(fake_thread_manager)
        # 不抛 = 通过
        v.validate_image_upload(
            thread_id="thread-aaaaaaaaaaaa",
            mime_type="image/png",
            size_bytes=1024,
            user_email="default",
        )

    @pytest.mark.parametrize(
        "bad_mime",
        ["image/bmp", "application/pdf", "text/plain", "", "image/png; charset=utf-8"],
    )
    def test_validate_image_upload_mime_rejected(
        self, fake_thread_manager: _FakeThreadManager, bad_mime: str
    ) -> None:
        v = MediaUploadValidator(fake_thread_manager)
        with pytest.raises(UploadValidationError) as exc_info:
            v.validate_image_upload(
                thread_id="thread-aaaaaaaaaaaa",
                mime_type=bad_mime,
                size_bytes=1024,
                user_email="default",
            )
        assert exc_info.value.code == "mime_not_allowed"

    def test_validate_image_upload_too_large(self, fake_thread_manager: _FakeThreadManager) -> None:
        v = MediaUploadValidator(fake_thread_manager)
        with pytest.raises(UploadValidationError) as exc_info:
            v.validate_image_upload(
                thread_id="thread-aaaaaaaaaaaa",
                mime_type="image/png",
                size_bytes=MAX_IMAGE_SIZE_BYTES + 1,
                user_email="default",
            )
        assert exc_info.value.code == "too_large"

    @pytest.mark.parametrize("empty_size", [0, -1])
    def test_validate_image_upload_empty_payload(
        self, fake_thread_manager: _FakeThreadManager, empty_size: int
    ) -> None:
        v = MediaUploadValidator(fake_thread_manager)
        with pytest.raises(UploadValidationError) as exc_info:
            v.validate_image_upload(
                thread_id="thread-aaaaaaaaaaaa",
                mime_type="image/png",
                size_bytes=empty_size,
                user_email="default",
            )
        assert exc_info.value.code == "empty_payload"

    def test_validate_image_upload_thread_not_found(
        self, fake_thread_manager: _FakeThreadManager
    ) -> None:
        v = MediaUploadValidator(fake_thread_manager)
        with pytest.raises(UploadValidationError) as exc_info:
            v.validate_image_upload(
                thread_id="thread-zzzzzzzzzzzz",
                mime_type="image/png",
                size_bytes=1024,
                user_email="default",
            )
        assert exc_info.value.code == "thread_not_found"

    def test_constants_consistency(self) -> None:
        # ALLOWED_IMAGE_MIME_TYPES 必须与 EXT_BY_MIME.keys() 一致
        assert ALLOWED_IMAGE_MIME_TYPES == frozenset(EXT_BY_MIME.keys())
        # 5 MiB
        assert MAX_IMAGE_SIZE_BYTES == 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# §4 集成测试：POST /api/uploads/images + GET /api/uploads/{asset_id}
#
# 最小 app 方案：直接挂 uploads_router，自己写个 ASGI middleware 注入
# session_payload，避免 create_app 完整装配链。
# ---------------------------------------------------------------------------


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """简化 :class:`web.auth.middleware.AuthMiddleware`：

    - ``authenticated=True``  → 给 ``request.state.session_payload`` 塞合法
      :class:`SessionTokenPayload`
    - ``authenticated=False`` → 直接返回 401（模拟原 AuthMiddleware 行为）

    通过 ``request.app.state.test_authenticated`` 切换，让单 client 内不同用例
    都能复用同一 app fixture。
    """

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        authenticated: bool = getattr(request.app.state, "test_authenticated", True)
        if not authenticated:
            from starlette.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "not authenticated"})
        request.state.session_payload = SessionTokenPayload(user_id="default", iat=0)
        return await call_next(request)


def _build_minimal_app(
    *,
    storage: AssetStorage,
    registry: AssetRegistry,
    thread_manager: _FakeThreadManager,
    validator: MediaUploadValidator | None = None,
) -> FastAPI:
    """组装一个最小 FastAPI 实例 + uploads router + 注入 state。"""

    app = FastAPI()
    app.add_middleware(_FakeAuthMiddleware)
    app.include_router(uploads_router)

    app.state.asset_storage = storage
    app.state.asset_registry = registry
    app.state.thread_manager = thread_manager
    app.state.upload_validator = validator or MediaUploadValidator(thread_manager)
    app.state.test_authenticated = True
    return app


@pytest.fixture
def app_and_client(
    tmp_storage: AssetStorage,
    fake_thread_manager: _FakeThreadManager,
) -> Any:
    """返回 ``(app, client, thread_id)`` 三元组。"""

    registry = AssetRegistry()
    app = _build_minimal_app(
        storage=tmp_storage,
        registry=registry,
        thread_manager=fake_thread_manager,
    )
    client = TestClient(app)
    thread_id = fake_thread_manager.list_threads()[0].id
    return app, client, thread_id


class TestUploadImagesEndpoint:
    def test_post_images_success(self, app_and_client: Any, png_payload: bytes) -> None:
        _app, client, thread_id = app_and_client
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # UserInputAttachment 字段齐全
        assert set(body.keys()) >= {
            "asset_id",
            "kind",
            "mime_type",
            "size_bytes",
            "preview_url",
            "status",
        }
        assert body["kind"] == "image"
        assert body["mime_type"] == "image/png"
        assert body["size_bytes"] == len(png_payload)
        assert body["status"] == "ready"
        assert body["preview_url"] == f"/api/uploads/{body['asset_id']}"
        # 服务端字段必须被脱敏（不能泄露给 client）
        for forbidden in ("thread_id", "sha256", "storage_path", "created_at"):
            assert forbidden not in body

    def test_post_images_mime_not_allowed(self, app_and_client: Any) -> None:
        _app, client, thread_id = app_and_client
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 415
        detail = resp.json()["detail"]
        assert detail["code"] == "mime_not_allowed"

    def test_post_images_too_large(self, app_and_client: Any) -> None:
        _app, client, thread_id = app_and_client
        big_payload = b"\x00" * (MAX_IMAGE_SIZE_BYTES + 1)
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("big.png", big_payload, "image/png")},
        )
        assert resp.status_code == 413
        detail = resp.json()["detail"]
        assert detail["code"] == "too_large"

    def test_post_images_thread_not_found(self, app_and_client: Any, png_payload: bytes) -> None:
        _app, client, _thread_id = app_and_client
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": "thread-zzzzzzzzzzzz"},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["code"] == "thread_not_found"

    def test_post_images_unauthenticated(self, app_and_client: Any, png_payload: bytes) -> None:
        app, client, thread_id = app_and_client
        # 关闭 fake auth
        app.state.test_authenticated = False
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert resp.status_code == 401

    def test_post_images_empty_payload(self, app_and_client: Any) -> None:
        _app, client, thread_id = app_and_client
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", b"", "image/png")},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "empty_payload"


class TestServeAssetEndpoint:
    def test_get_asset_success(self, app_and_client: Any, png_payload: bytes) -> None:
        _app, client, thread_id = app_and_client
        post = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert post.status_code == 200, post.text
        asset_id = post.json()["asset_id"]

        get = client.get(f"/api/uploads/{asset_id}")
        assert get.status_code == 200
        assert get.content == png_payload
        assert get.headers["content-type"].startswith("image/png")

    def test_get_asset_not_found(self, app_and_client: Any) -> None:
        _app, client, _thread_id = app_and_client
        # 32 字符 hex 但不存在
        bogus = "0" * 32
        resp = client.get(f"/api/uploads/{bogus}")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "bad_id",
        [
            "..%2Fetc%2Fpasswd",  # URL 编码
            "..",  # 直接父目录
            "foo..bar",  # 内嵌
        ],
    )
    def test_get_asset_path_traversal_blocked(self, app_and_client: Any, bad_id: str) -> None:
        _app, client, _thread_id = app_and_client
        resp = client.get(f"/api/uploads/{bad_id}")
        # 422（asset_id 含非法字符）或 404（无效但语法合法）—— 关键：拒绝读盘越权
        assert resp.status_code in (404, 422)
        # 显式不读到 /etc/passwd 内容
        if resp.status_code == 200:  # pragma: no cover - 防御
            pytest.fail(f"path traversal not blocked: {resp.content!r}")

    def test_get_asset_thread_deleted(
        self,
        tmp_storage: AssetStorage,
        png_payload: bytes,
    ) -> None:
        """资产文件还在，但归属 thread 已从 ThreadManager 移除 → 404。

        覆盖 router 的 "thread 已删除则资产不可访问" 防御分支。
        """

        # 装一个带 1 个 thread 的 manager，先上传，再换成空 manager
        thread_meta = _make_thread_meta("thread-bbbbbbbbbbbb")
        tm = _FakeThreadManager([thread_meta])
        registry = AssetRegistry()
        app = _build_minimal_app(
            storage=tmp_storage,
            registry=registry,
            thread_manager=tm,
        )
        client = TestClient(app)
        post = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_meta.id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert post.status_code == 200, post.text
        asset_id = post.json()["asset_id"]

        # 移除 thread；asset 文件仍在盘上
        empty_tm = _FakeThreadManager([])
        app.state.thread_manager = empty_tm
        app.state.upload_validator = MediaUploadValidator(empty_tm)

        get = client.get(f"/api/uploads/{asset_id}")
        assert get.status_code == 404


# ---------------------------------------------------------------------------
# §5 R2 boundary fix 回归用例
#
# - P0-1 路径校验漏 glob 元字符 → 跨 thread 越权读取
# - P0-3 大文件读取早于 size 校验 → OOM
# ---------------------------------------------------------------------------


class TestServeAssetGlobMetacharBlocked:
    """P0-1 R2 boundary fix：asset_id 必须是 uuid4 hex 字面值。

    旧实现只挡 ``/``、``..``、``\\``，不挡 glob 元字符 ``*`` / ``?`` / ``[``。
    攻击 path ``GET /api/uploads/*`` 会让 ``rglob(f"{asset_id}.*")`` 展开成
    ``*.*`` 匹配任意 thread 的任意图片。修复后必须返回 400/404/422 之一，
    且 body 绝不含 PNG header（不漏读任何 asset）。
    """

    @pytest.mark.parametrize(
        "bad_asset_id",
        [
            "*",  # 单字符通配
            "*.png",  # 后缀通配
            "?" * 32,  # 单字符匹配，凑够 32 位
            "[abcdef]",  # 字符集
            "**",  # 递归通配
            "abc",  # 长度不够（不是 32 位）
            "G" * 32,  # 32 位但有非 hex 字符
            "a" * 31,  # 长度差 1
            "a" * 33,  # 长度多 1
        ],
    )
    def test_get_asset_glob_metachar_blocked(
        self,
        app_and_client: Any,
        png_payload: bytes,
        bad_asset_id: str,
    ) -> None:
        _app, client, thread_id = app_and_client
        # 先在 storage 里塞一张真实图片，确保即便 glob 展开也"有东西可读"——
        # 测试要保证：即便能展开，也不会读到这张图。
        post = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("real.png", png_payload, "image/png")},
        )
        assert post.status_code == 200, post.text

        resp = client.get(f"/api/uploads/{bad_asset_id}")
        # 修复后必须挡：400（uuid 校验）/ 404（reverse traversal 错挡）/
        # 422（FastAPI path validator 拒）/ 405（路由不匹配 e.g. *）
        assert resp.status_code in (400, 404, 422, 405), (
            f"unexpected status={resp.status_code} for bad_asset_id={bad_asset_id!r}"
        )
        # 关键回归点：不返回任何 PNG bytes
        assert PNG_HEADER not in resp.content
        assert not resp.headers.get("content-type", "").startswith("image/")

    def test_valid_uuid_hex_still_accepted(self, app_and_client: Any, png_payload: bytes) -> None:
        """正面回归：正常 32 位 lowercase hex asset_id 仍能正常返回。

        防止 ``_ASSET_ID_PATTERN`` 收得太紧把合法 ID 也挡掉。
        """

        _app, client, thread_id = app_and_client
        post = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert post.status_code == 200, post.text
        asset_id = post.json()["asset_id"]
        # 形态校验：32 位 hex
        assert len(asset_id) == 32
        assert all(c in "0123456789abcdef" for c in asset_id)

        get = client.get(f"/api/uploads/{asset_id}")
        assert get.status_code == 200
        assert get.content == png_payload


class TestUploadImageDeclaredSizeEarlyReject:
    """P0-3 R2 boundary fix：``UploadFile.size`` 已声明超限时不读 payload。

    旧实现把 ``await file.read()`` 放在 size 校验前，5MB 上限只通过
    ``len(payload)`` 事后判断；攻击者用 100MB content-length 上传会先把
    整个 payload 塞进内存才被拒，存在 OOM 风险。修复后必须看到
    declared-size 走 413 早拒绝路径。
    """

    def test_post_image_declared_size_too_large_rejected(
        self,
        tmp_path: Path,
        fake_thread_manager: _FakeThreadManager,
    ) -> None:
        """直接调 endpoint 函数，注入一个 size=100MB 的 fake UploadFile，
        验证 ``read()`` 不会被调用就抛 413。
        """

        from fastapi import HTTPException

        from web.routers import uploads as uploads_router_mod

        storage = AssetStorage(base_dir=tmp_path / "uploads")
        registry = AssetRegistry()
        validator = MediaUploadValidator(fake_thread_manager)

        class _FakeUploadFile:
            """最小 ``UploadFile``-like：``size`` 报很大，``read()`` 一调就 raise。"""

            content_type = "image/png"
            size = 100 * 1024 * 1024  # 100 MiB（远超 5 MiB 上限）
            filename = "huge.png"

            async def read(self) -> bytes:
                # 早拒绝失败时这里会被调用 → 测试失败
                raise AssertionError("file.read() was called before declared-size early reject")

        # 构造 minimal request.state 以满足 _current_user_id + get_*
        class _State:
            session_payload = SessionTokenPayload(user_id="default", iat=0)

        class _AppState:
            asset_storage = storage
            asset_registry = registry
            upload_validator = validator
            thread_manager = fake_thread_manager

        class _App:
            state = _AppState()

        class _Request:
            app = _App()
            state = _State()

        import asyncio

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                uploads_router_mod.upload_image(
                    request=_Request(),  # type: ignore[arg-type]
                    file=_FakeUploadFile(),  # type: ignore[arg-type]
                    thread_id=fake_thread_manager.list_threads()[0].id,
                )
            )

        assert exc_info.value.status_code == 413
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("code") == "too_large"
        # 错误消息必须含"declared"字样以区分事后校验
        assert "declared" in detail.get("message", "")

    def test_post_image_declared_size_within_limit_proceeds(
        self,
        app_and_client: Any,
        png_payload: bytes,
    ) -> None:
        """正面回归：合法大小（5MB 以内）的上传不被 declared-size 早拒。

        TestClient 用 BytesIO 时 ``file.size`` 通常已被 starlette 计算好，
        所以这条用例顺带验证早拒绝路径不会误伤正常上传。
        """

        _app, client, thread_id = app_and_client
        resp = client.post(
            "/api/uploads/images",
            data={"thread_id": thread_id},
            files={"file": ("foo.png", png_payload, "image/png")},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# §6 P1 #2 R2 boundary fix: ThreadManager.delete_thread 同步清理 asset storage
#
# 旧实现 ThreadManager.delete_thread 只删 metadata 目录,asset 文件持续留盘,
# 长期使用磁盘膨胀。修复:ThreadManager 注入 AssetStorage,delete_thread 末尾
# 调 delete_thread_assets。
# ---------------------------------------------------------------------------


class TestThreadManagerDeleteThreadCleansAssets:
    """P1 #2: delete_thread → 同步删除 thread 名下所有 image/video/file 资产。"""

    async def test_delete_thread_removes_asset_files(
        self,
        tmp_path: Path,
        png_payload: bytes,
    ) -> None:
        """create_thread → 上传 1 张图 → delete_thread → 资产目录被清空。"""

        from unittest.mock import AsyncMock, MagicMock

        from config_loader.models import Config
        from web.threads.manager import ThreadManager

        cfg = Config.model_validate(
            {
                "model": {
                    "name": "fake",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key": "",
                },
                "web": {
                    "enabled": True,
                    "idle_timeout_seconds": 1800,
                    "idle_check_interval_seconds": 60,
                    "pending_approval_timeout_seconds": 60,
                },
            }
        )

        async def factory(
            thread_id: str,
            preset_id: str,
            adapter: Any,
            sinks: list[Any],
        ) -> tuple[Any, Any]:
            runtime = MagicMock()
            runtime.aclose = AsyncMock(return_value=None)
            bridge = MagicMock()
            return runtime, bridge

        storage = AssetStorage(base_dir=tmp_path / "uploads")
        mgr = ThreadManager(
            cfg,
            kongming_home=tmp_path,
            runtime_factory=factory,
            asset_storage=storage,
        )
        meta = await mgr.create_thread("with-image", "p1")

        # 给 thread 写 2 张图模拟资产
        storage.write_asset(
            asset_id="img1",
            thread_id=meta.id,
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata={"k": "v"},
        )
        storage.write_asset(
            asset_id="img2",
            thread_id=meta.id,
            kind="image",
            ext=".png",
            payload=png_payload,
            metadata={"k": "v"},
        )
        # 验证文件确实落盘
        thread_image_dir = storage.base_dir / "images" / meta.id
        assert thread_image_dir.is_dir()
        assert (thread_image_dir / "img1.png").is_file()

        # 执行 delete_thread
        await mgr.delete_thread(meta.id)

        # 验证目录被整个清除
        assert not thread_image_dir.exists(), (
            f"asset dir still exists after delete_thread: {thread_image_dir}"
        )
        # storage.read_asset_bytes 找不到文件
        with pytest.raises(FileNotFoundError):
            storage.read_asset_bytes(
                asset_id="img1",
                thread_id=meta.id,
                kind="image",
                ext=".png",
            )

    async def test_delete_thread_without_asset_storage_skips_cleanup(
        self,
        tmp_path: Path,
    ) -> None:
        """ThreadManager 不注入 asset_storage(CLI / 老路径) → delete_thread 不抛、不清理。

        向后兼容:asset_storage=None 默认值必须让老调用方继续工作。
        """

        from unittest.mock import AsyncMock, MagicMock

        from config_loader.models import Config
        from web.threads.manager import ThreadManager

        cfg = Config.model_validate(
            {
                "model": {
                    "name": "fake",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key": "",
                },
                "web": {
                    "enabled": True,
                    "idle_timeout_seconds": 1800,
                    "idle_check_interval_seconds": 60,
                    "pending_approval_timeout_seconds": 60,
                },
            }
        )

        async def factory(
            thread_id: str,
            preset_id: str,
            adapter: Any,
            sinks: list[Any],
        ) -> tuple[Any, Any]:
            runtime = MagicMock()
            runtime.aclose = AsyncMock(return_value=None)
            bridge = MagicMock()
            return runtime, bridge

        mgr = ThreadManager(
            cfg,
            kongming_home=tmp_path,
            runtime_factory=factory,
            # 故意不传 asset_storage
        )
        meta = await mgr.create_thread("no-storage", "p1")

        # delete_thread 不抛(asset_storage 为 None 时跳过清理分支)
        await mgr.delete_thread(meta.id)
        # metadata 目录确实被删
        assert mgr.get_cell(meta.id) is None
