"""AttachmentPrefixBuilder 单测（claude-code-channel-image-paste §B #1）。

覆盖：

- 空 / None attachments → 空字符串
- 单图：基本 ``@<abs>`` 格式
- 多图：保持顺序 + 末尾空行
- 路径含空格：走 ``@"..."`` quoted 语法
- mime 未知（不在 EXT_BY_MIME）→ 跳过 + warning log
- 物理路径不存在 → 跳过 + warning log
- asset_storage.asset_path 抛错 → 跳过（防御性容错）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.claude_code._attachment_prefix import AttachmentPrefixBuilder
from web.protocol.rest_models import UserInputAttachment
from web.uploads.storage import AssetStorage


def _make_att(
    *,
    asset_id: str = "abcdef0123456789abcdef0123456789",
    mime_type: str = "image/png",
    kind: str = "image",
) -> UserInputAttachment:
    """构造合法的 ``UserInputAttachment``。"""
    return UserInputAttachment(
        asset_id=asset_id,
        kind=kind,  # type: ignore[arg-type]
        mime_type=mime_type,
        size_bytes=100,
        preview_url=f"/api/uploads/{asset_id}",
        status="ready",
    )


def _write_asset(
    storage: AssetStorage,
    *,
    asset_id: str,
    thread_id: str,
    mime_type: str,
    ext: str,
    payload: bytes = b"\x89PNG\r\n\x1a\n",
) -> Path:
    """在 tmp storage 写一个真实 asset 文件，返回路径。"""
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
            "status": "ready",
        },
    )
    return storage.asset_path(
        asset_id=asset_id,
        thread_id=thread_id,
        kind="image",
        ext=ext,
    )


class TestBuildEmpty:
    """空入参应返回空字符串。"""

    def test_none_returns_empty_string(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = AttachmentPrefixBuilder(storage)
        assert builder.build(None, thread_id="thread-x") == ""

    def test_empty_list_returns_empty_string(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = AttachmentPrefixBuilder(storage)
        assert builder.build([], thread_id="thread-x") == ""


class TestBuildSingle:
    """单个合法附件的基本格式。"""

    def test_single_png_atfile_prefix(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-abc12345"
        asset_id = "1234567890abcdef1234567890abcdef"
        path = _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        # 单行 @<abs_path> + 末尾两个换行（一个行尾，一个隔离 user text）
        assert result == f"@{path}\n\n"

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
        """4 种支持的图片 mime 都能输出对应路径。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-fedc12345678"
        asset_id = "abcdef0123456789abcdef0123456789"
        _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type=mime_type,
            ext=ext,
        )
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type=mime_type)],
            thread_id=thread_id,
        )

        assert ext in result
        assert result.startswith("@")
        assert result.endswith("\n\n")


class TestBuildMultiple:
    """多附件保持顺序、合并输出。"""

    def test_two_attachments_keep_order(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-multi0001"
        asset_id_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        asset_id_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        path_a = _write_asset(
            storage,
            asset_id=asset_id_a,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        path_b = _write_asset(
            storage,
            asset_id=asset_id_b,
            thread_id=thread_id,
            mime_type="image/jpeg",
            ext=".jpg",
        )
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(
            [
                _make_att(asset_id=asset_id_a, mime_type="image/png"),
                _make_att(asset_id=asset_id_b, mime_type="image/jpeg"),
            ],
            thread_id=thread_id,
        )

        # 期望两行 + 末尾空行
        assert result == f"@{path_a}\n@{path_b}\n\n"

    def test_three_attachments(self, tmp_path: Path) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-three0001"
        atts = []
        expected_paths = []
        for i in range(3):
            asset_id = f"{i:032x}"
            path = _write_asset(
                storage,
                asset_id=asset_id,
                thread_id=thread_id,
                mime_type="image/png",
                ext=".png",
            )
            atts.append(_make_att(asset_id=asset_id, mime_type="image/png"))
            expected_paths.append(path)
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(atts, thread_id=thread_id)

        for path in expected_paths:
            assert f"@{path}" in result
        # 顺序
        idx0 = result.index(f"@{expected_paths[0]}")
        idx1 = result.index(f"@{expected_paths[1]}")
        idx2 = result.index(f"@{expected_paths[2]}")
        assert idx0 < idx1 < idx2


class TestPathWithSpaces:
    """路径含空格走 quoted 语法。"""

    def test_thread_id_with_space_uses_quoted(self, tmp_path: Path) -> None:
        # 通过把 base_dir 设成含空格的路径，制造一个实际路径含空格的 asset
        spaced_base = tmp_path / "folder with space"
        spaced_base.mkdir()
        storage = AssetStorage(base_dir=spaced_base)
        thread_id = "thread-spaced01"
        asset_id = "0011223344556677001122334455667788"[:32]
        _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        # 含空格 → 走 @"..." quoted
        assert result.startswith('@"')
        assert result.endswith('"\n\n')
        assert "folder with space" in result


class TestPathWithSpecialChars:
    """跨平台路径处理（Tauri macOS + Windows 套壳）。

    Windows path 含 ``:`` (drive letter) 和 ``\\`` (分隔符)；macOS / Linux 路径
    理论可含这些字符但极罕见。任一字符出现都走 quoted 语法避开 CLI
    ``regular regex (@[^\s]+\b)`` 的边界歧义。
    """

    def test_path_with_backslash_uses_quoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows 反斜杠路径 → quoted（模拟 ``str(Path)`` 在 Windows 上的输出）。"""

        # 不能真在 Unix 上构造 WindowsPath（文件 IO 会失败），改 monkeypatch
        # AssetStorage.asset_path 直接返回 fake Windows path
        from web.uploads import storage as storage_mod

        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-winpath01"
        asset_id = "ffeeddccbbaa99887766554433221100"[:32]
        _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )

        # patch asset_path 返回 Windows 风格 str (含 \\)
        fake_win_path = Path(
            r"C:\Users\foo\.kongming\web\uploads\images\thread-winpath01"
            f"\\{asset_id}.png",
        )
        # 让 is_file 返回 True（绕过真实文件系统）
        monkeypatch.setattr(storage_mod.Path, "is_file", lambda self: True)
        monkeypatch.setattr(storage, "asset_path", lambda **_: fake_win_path)

        builder = AttachmentPrefixBuilder(storage)
        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        # 含 \\ 和 : → 走 @"..." quoted
        assert result.startswith('@"')
        assert result.endswith('"\n\n')
        assert "C:" in result  # drive letter 保留

    def test_path_with_double_quote_escaped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路径含 ``"`` → quoted 且内嵌引号被反斜杠转义。"""

        from web.uploads import storage as storage_mod

        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-quote0001"
        asset_id = "1234567890abcdef1234567890abcdef"
        _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )

        fake_path_with_quote = Path('/tmp/weird"name/img.png')
        monkeypatch.setattr(storage_mod.Path, "is_file", lambda self: True)
        monkeypatch.setattr(storage, "asset_path", lambda **_: fake_path_with_quote)

        builder = AttachmentPrefixBuilder(storage)
        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        # 内嵌 " 被转义为 \"
        assert result.startswith('@"')
        assert '\\"name' in result

    def test_plain_unix_path_uses_regular(self, tmp_path: Path) -> None:
        """纯 ASCII Unix 路径（无空格/反斜杠/冒号）走 regular 语法不变。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-plain0001"
        asset_id = "abcdefabcdefabcdefabcdefabcdefab"
        _write_asset(
            storage,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        builder = AttachmentPrefixBuilder(storage)

        result = builder.build(
            [_make_att(asset_id=asset_id, mime_type="image/png")],
            thread_id=thread_id,
        )

        # 纯路径走 @<path> regular（无引号包裹）
        assert result.startswith("@/")
        assert not result.startswith('@"')


class TestFallbackSkip:
    """失败模式：mime 未知 / 路径不存在 / storage 抛错 → 跳过 + warning log。"""

    def test_unknown_mime_skipped_with_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = AttachmentPrefixBuilder(storage)
        att = _make_att(mime_type="image/bmp")  # bmp 不在 EXT_BY_MIME

        with caplog.at_level("WARNING"):
            result = builder.build([att], thread_id="thread-bad")

        assert result == ""
        assert "image/bmp" in caplog.text
        assert "not in EXT_BY_MIME" in caplog.text

    def test_missing_file_skipped_with_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = AssetStorage(base_dir=tmp_path)
        builder = AttachmentPrefixBuilder(storage)
        # mime 合法但物理文件没写
        att = _make_att(
            asset_id="cccccccccccccccccccccccccccccccc",
            mime_type="image/png",
        )

        with caplog.at_level("WARNING"):
            result = builder.build([att], thread_id="thread-ghost")

        assert result == ""
        assert "file not found" in caplog.text
        assert "cccccc" in caplog.text  # asset_id 出现在日志便于定位

    def test_partial_failure_keeps_valid(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """一个 attachment 合法 + 一个不合法 → 输出合法那条，跳过不合法那条。"""
        storage = AssetStorage(base_dir=tmp_path)
        thread_id = "thread-partial01"
        good_id = "dddddddddddddddddddddddddddddddd"
        ghost_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        good_path = _write_asset(
            storage,
            asset_id=good_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        builder = AttachmentPrefixBuilder(storage)

        with caplog.at_level("WARNING"):
            result = builder.build(
                [
                    _make_att(asset_id=ghost_id, mime_type="image/png"),
                    _make_att(asset_id=good_id, mime_type="image/png"),
                ],
                thread_id=thread_id,
            )

        assert result == f"@{good_path}\n\n"
        assert "eeeeee" in caplog.text


class TestStorageInjection:
    """注入不同 storage 实例时正确反推路径。"""

    def test_different_base_dirs_yield_different_paths(self, tmp_path: Path) -> None:
        base_a = tmp_path / "a"
        base_b = tmp_path / "b"
        base_a.mkdir()
        base_b.mkdir()
        storage_a = AssetStorage(base_dir=base_a)
        storage_b = AssetStorage(base_dir=base_b)
        thread_id = "thread-injection01"
        asset_id = "ffffffffffffffffffffffffffffffff"
        _write_asset(
            storage_a,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )
        _write_asset(
            storage_b,
            asset_id=asset_id,
            thread_id=thread_id,
            mime_type="image/png",
            ext=".png",
        )

        att = _make_att(asset_id=asset_id, mime_type="image/png")
        result_a = AttachmentPrefixBuilder(storage_a).build([att], thread_id=thread_id)
        result_b = AttachmentPrefixBuilder(storage_b).build([att], thread_id=thread_id)

        assert str(base_a) in result_a
        assert str(base_b) in result_b
        assert result_a != result_b
