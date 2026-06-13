"""codex 通道 ``CodexImageCliArgsBuilder`` 单元测试（codex-channel-image-paste §1）。

覆盖 11 个场景：

- 空 / 单图 / 多图保序
- 4 mime 参数化（png/jpeg/webp/gif）
- 4 条输入防线（kind / status / mime / 路径）单独触发的跳过
- 部分失败保留有效项

设计要点：
- **真 ``AssetStorage(base_dir=tmp_path)`` 注入**（不 mock 被测对象自己）
- **真写 PNG bytes 到磁盘**（path.is_file() 真返回 True，不 monkeypatch）
- 失败用例用 ``caplog`` 验证 warning log 出现
- 断言写死 expected list（不用业务函数反算）
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hosts.web.integrations.codex._image_cli_args import CodexImageCliArgsBuilder
from hosts.web.protocol.rest_models import UserInputAttachment
from hosts.web.uploads.storage import AssetStorage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_MIN_PNG = bytes.fromhex(
    # 1x1 透明 PNG（最小合法）
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000169d8a4dd0000000049454e44"
    "ae426082",
)


def _make_att(
    *,
    asset_id: str = "0011223344556677001122334455667788",
    kind: str = "image",
    mime_type: str = "image/png",
    status: str = "ready",
    size_bytes: int = 1024,
    width: int | None = 100,
    height: int | None = 80,
) -> UserInputAttachment:
    """构造合法的 ``UserInputAttachment``。"""
    return UserInputAttachment(
        asset_id=asset_id,
        kind=kind,  # type: ignore[arg-type]
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_ms=None,
        preview_url=f"/api/uploads/{asset_id}",
        status=status,  # type: ignore[arg-type]
    )


def _write_asset(
    storage: AssetStorage,
    *,
    asset_id: str,
    thread_id: str,
    mime_type: str = "image/png",
    ext: str = ".png",
    payload: bytes = _MIN_PNG,
) -> Path:
    """把 bytes 真写到 storage 物理路径，返回实际路径。"""
    storage.write_asset(
        asset_id=asset_id,
        thread_id=thread_id,
        kind="image",
        ext=ext,
        payload=payload,
        metadata={
            "asset_id": asset_id,
            "thread_id": thread_id,
            "kind": "image",
            "mime_type": mime_type,
            "size_bytes": len(payload),
            "sha256": "",
            "storage_path": "",
            "preview_url": "",
            "status": "ready",
            "created_at": 0.0,
            "width": None,
            "height": None,
            "duration_ms": None,
        },
    )
    return storage.asset_path(
        asset_id=asset_id,
        thread_id=thread_id,
        kind="image",
        ext=ext,
    )


# ---------------------------------------------------------------------------
# §1 空输入
# ---------------------------------------------------------------------------


class TestBuildEmpty:
    def test_none_returns_empty_list(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = CodexImageCliArgsBuilder(storage)
        assert builder.build(None, thread_id="thread-abc") == []

    def test_empty_list_returns_empty_list(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = CodexImageCliArgsBuilder(storage)
        assert builder.build([], thread_id="thread-abc") == []


# ---------------------------------------------------------------------------
# §2 happy path 单图
# ---------------------------------------------------------------------------


class TestBuildSingle:
    def test_single_png_args(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-aaaaaaaaaaaa"
        asset_id = "abcdefabcdefabcdefabcdefabcdefab"
        path = _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        builder = CodexImageCliArgsBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        assert result == ["--image", str(path)]
        assert len(result) == 2

    @pytest.mark.parametrize(
        ("mime_type", "ext"),
        [
            ("image/png", ".png"),
            ("image/jpeg", ".jpg"),
            ("image/webp", ".webp"),
            ("image/gif", ".gif"),
        ],
    )
    def test_supported_mimes(self, tmp_path: Path, mime_type: str, ext: str) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-bbbbbbbbbbbb"
        asset_id = f"{mime_type.replace('/', '_'):0<32}"[:32]
        path = _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type=mime_type,
            ext=ext,
        )
        builder = CodexImageCliArgsBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type=mime_type)],
            thread_id=thread_id,
        )

        assert result == ["--image", str(path)]
        assert str(path).endswith(ext)


# ---------------------------------------------------------------------------
# §3 多图保序
# ---------------------------------------------------------------------------


class TestBuildMultiple:
    def test_two_attachments_keep_order(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-cccccccccccc"
        asset_a = "a" * 32
        asset_b = "b" * 32
        path_a = _write_asset(storage, asset_id=asset_a, thread_id=thread_id)
        path_b = _write_asset(storage, asset_id=asset_b, thread_id=thread_id)
        builder = CodexImageCliArgsBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_a), _make_att(asset_id=asset_b)],
            thread_id=thread_id,
        )

        assert result == [
            "--image",
            str(path_a),
            "--image",
            str(path_b),
        ]
        # 顺序断言：a 在 b 前
        assert result.index(str(path_a)) < result.index(str(path_b))

    def test_three_attachments(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-dddddddddddd"
        ids = [c * 32 for c in ("1", "2", "3")]
        paths = [_write_asset(storage, asset_id=aid, thread_id=thread_id) for aid in ids]
        builder = CodexImageCliArgsBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=aid) for aid in ids],
            thread_id=thread_id,
        )

        # 3 个 --image flag + 3 个 path = 6 项
        assert len(result) == 6
        assert result == [
            "--image",
            str(paths[0]),
            "--image",
            str(paths[1]),
            "--image",
            str(paths[2]),
        ]


# ---------------------------------------------------------------------------
# §4 输入防线
# ---------------------------------------------------------------------------


class TestInputDefenses:
    """4 条输入防线：kind / status / mime / 路径，任一失败即跳过 + warning。"""

    def test_kind_not_image_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """防线 1：kind != "image" 跳过（拒未来 video / file 扩展）。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-eeeeeeeeeeee"
        # 物理文件存在，但 kind=video → builder 应拒
        _write_asset(storage, asset_id="e" * 32, thread_id=thread_id)
        builder = CodexImageCliArgsBuilder(storage)

        with caplog.at_level(
            logging.WARNING, logger="hosts.web.integrations.codex._image_cli_args"
        ):
            result = builder.build(
                [_make_att(asset_id="e" * 32, kind="video")],
                thread_id=thread_id,
            )

        assert result == []
        assert any("is not image" in r.message for r in caplog.records)

    def test_status_not_ready_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """防线 2：status != "ready" 跳过（拒 uploading / failed）。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-ffffffffffff"
        _write_asset(storage, asset_id="f" * 32, thread_id=thread_id)
        builder = CodexImageCliArgsBuilder(storage)

        with caplog.at_level(
            logging.WARNING, logger="hosts.web.integrations.codex._image_cli_args"
        ):
            result = builder.build(
                [_make_att(asset_id="f" * 32, status="processing")],
                thread_id=thread_id,
            )

        assert result == []
        assert any("is not ready" in r.message for r in caplog.records)

    def test_unknown_mime_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """防线 3：mime 不在 EXT_BY_MIME 白名单 → 跳过。

        注意：pydantic Literal 校验 mime_type 是 str 不限值；EXT_BY_MIME
        的限制是 builder 内部白名单。
        """
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-99999999aaaa"
        builder = CodexImageCliArgsBuilder(storage)

        with caplog.at_level(
            logging.WARNING, logger="hosts.web.integrations.codex._image_cli_args"
        ):
            result = builder.build(
                [_make_att(asset_id="9" * 32, mime_type="image/bmp")],
                thread_id=thread_id,
            )

        assert result == []
        assert any("not in EXT_BY_MIME" in r.message for r in caplog.records)

    def test_missing_file_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """防线 4：路径反推但磁盘不存在 → 跳过。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-aaaa11112222"
        # **不**写盘 —— path.is_file() 返 False
        builder = CodexImageCliArgsBuilder(storage)

        with caplog.at_level(
            logging.WARNING, logger="hosts.web.integrations.codex._image_cli_args"
        ):
            result = builder.build(
                [_make_att(asset_id="a" * 31 + "0", mime_type="image/png")],
                thread_id=thread_id,
            )

        assert result == []
        assert any("file not found" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# §5 部分失败保留有效项
# ---------------------------------------------------------------------------


class TestPartialFailure:
    def test_partial_failure_keeps_valid(self, tmp_path: Path) -> None:
        """3 个附件中 1 个 mime 不支持 + 1 个 status 失败 + 1 个合法 → 只剩合法那个。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-bbbb22223333"
        # 合法附件
        good_id = "1" * 32
        good_path = _write_asset(storage, asset_id=good_id, thread_id=thread_id)
        # 第 2 个：mime 不在白名单（不写盘也没关系，防线 3 先于防线 4）
        bad_mime_id = "2" * 32
        # 第 3 个：status 非 ready（写盘但 status=failed）
        bad_status_id = "3" * 32
        _write_asset(storage, asset_id=bad_status_id, thread_id=thread_id)

        builder = CodexImageCliArgsBuilder(storage)
        result = builder.build(
            [
                _make_att(asset_id=bad_mime_id, mime_type="image/bmp"),
                _make_att(asset_id=good_id, mime_type="image/png"),
                _make_att(asset_id=bad_status_id, status="failed"),
            ],
            thread_id=thread_id,
        )

        # 只有合法那个进入结果
        assert result == ["--image", str(good_path)]


# ---------------------------------------------------------------------------
# §6 跨平台：路径含空格 / 特殊字符（subprocess args 不需要 quoted，验证天然支持）
# ---------------------------------------------------------------------------


class TestCrossPlatform:
    """与 claude_code 通道不同：CLI args 走 ``list[str]`` 给
    ``asyncio.create_subprocess_exec``，shell 不再 tokenize，**任意字符天然支持**。
    本测试验证含空格 / 中文 / 特殊字符的 base_dir 路径不需要任何额外转义。
    """

    def test_path_with_space_passes_through(self, tmp_path: Path) -> None:
        """base_dir 含空格 → path 含空格，直接进 args list，不需 quoted。"""
        spaced_base = tmp_path / "folder with space"
        spaced_base.mkdir()
        storage = AssetStorage(base_dir=spaced_base)
        thread_id = "thread-spaced012345"
        asset_id = "c" * 32
        path = _write_asset(storage, asset_id=asset_id, thread_id=thread_id)
        builder = CodexImageCliArgsBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id)],
            thread_id=thread_id,
        )

        # path 含空格但 args list 形态下 subprocess 不会 tokenize
        assert result == ["--image", str(path)]
        assert " " in result[1]  # 确实含空格，未被转义

    def test_storage_injection_isolation(self, tmp_path: Path) -> None:
        """不同 storage 实例（base_dir 不同）反推路径必然不同 → 注入有效。"""
        base_a = tmp_path / "a"
        base_b = tmp_path / "b"
        base_a.mkdir()
        base_b.mkdir()
        storage_a = AssetStorage(base_dir=base_a)
        storage_b = AssetStorage(base_dir=base_b)
        thread_id = "thread-injectabcde0"
        asset_id = "d" * 32
        path_a = _write_asset(storage_a, asset_id=asset_id, thread_id=thread_id)
        path_b = _write_asset(storage_b, asset_id=asset_id, thread_id=thread_id)

        builder_a = CodexImageCliArgsBuilder(storage_a)
        builder_b = CodexImageCliArgsBuilder(storage_b)
        att = _make_att(asset_id=asset_id)

        result_a = builder_a.build([att], thread_id=thread_id)
        result_b = builder_b.build([att], thread_id=thread_id)

        assert result_a == ["--image", str(path_a)]
        assert result_b == ["--image", str(path_b)]
        assert path_a != path_b
