"""Pytest for ``src/web/app.py::_bootstrap_projects_registry``.

仅覆盖 ``_bootstrap_projects_registry`` 函数本身的行为，不测整体
lifespan 启动（那是 e2e 范畴）。

worktree 隔离：用 ``tmp_path`` 作为 ``home``；函数体内对两个
projects_registry 模块的依赖通过 ``monkeypatch`` 替换。

任务：``web-projects-registry-v0.1`` 子任务 #14。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hosts.web.app import _bootstrap_projects_registry
from hosts.web.integrations.claude_code.projects_registry import (
    claude_projects_path,
)
from hosts.web.integrations.claude_code.projects_registry import (
    load_registry as load_claude_registry,
)
from hosts.web.integrations.codex.projects_registry import (
    codex_projects_path,
)
from hosts.web.integrations.codex.projects_registry import (
    load_registry as load_codex_registry,
)
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_meta(
    *,
    thread_id: str,
    backend_kind: str,
    cwd: str = "",
) -> ThreadMetadata:
    """构造 thread metadata。

    ``thread_id`` 必须形如 ``thread-<hex12>`` 才能通过 schema 校验。
    """
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="" if backend_kind != "generic_chat" else "p1",
        backend_kind=backend_kind,  # type: ignore[arg-type]
        cwd=cwd,
        created_at=1.0,
        updated_at=1.0,
    )


def _claude_cwds(home: Path) -> list[str]:
    return [e.cwd for e in load_claude_registry(home)]


def _codex_cwds(home: Path) -> list[str]:
    return [e.cwd for e in load_codex_registry(home)]


# ---------------------------------------------------------------------------
# 1. 首次启动自动登记当前 worktree
# ---------------------------------------------------------------------------


class TestFirstBootstrapRegistersSelf:
    def test_creates_both_registry_files_with_repo_root(self, tmp_path: Path) -> None:
        repo_root = "/abs/worktree/kongming-agent"

        _bootstrap_projects_registry(tmp_path, repo_root)

        # 两个 registry 文件都应被创建
        assert claude_projects_path(tmp_path).is_file()
        assert codex_projects_path(tmp_path).is_file()

        # 两个 registry 都应只含 repo_root
        assert _claude_cwds(tmp_path) == [repo_root]
        assert _codex_cwds(tmp_path) == [repo_root]


# ---------------------------------------------------------------------------
# 2. thread metadata 自动迁移
# ---------------------------------------------------------------------------


class TestMigrateFromThreadMetadata:
    def test_distinct_cwds_split_by_backend_kind(self, tmp_path: Path) -> None:
        repo_root = "/abs/worktree/kongming-agent"

        # claude_code, /path/a
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-aaaaaaaaaaaa",
                backend_kind="claude_code",
                cwd="/path/a",
            ),
        )
        # claude_code, /path/b
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-bbbbbbbbbbbb",
                backend_kind="claude_code",
                cwd="/path/b",
            ),
        )
        # codex, /path/c
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-cccccccccccc",
                backend_kind="codex",
                cwd="/path/c",
            ),
        )
        # generic_chat, "" → 应忽略
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-dddddddddddd",
                backend_kind="generic_chat",
                cwd="",
            ),
        )
        # claude_code, "" → 应忽略
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-eeeeeeeeeeee",
                backend_kind="claude_code",
                cwd="",
            ),
        )

        _bootstrap_projects_registry(tmp_path, repo_root)

        # claude registry 含 [/path/a, /path/b, repo_root]
        # bootstrap 先于 migrate 调用：repo_root 在前，迁出的 a/b 在后；
        # 但本测试只校验"集合等价"，不绑定顺序。
        assert set(_claude_cwds(tmp_path)) == {"/path/a", "/path/b", repo_root}
        # codex registry 含 [/path/c, repo_root]
        assert set(_codex_cwds(tmp_path)) == {"/path/c", repo_root}

        # 关键：codex 的 /path/c 不应进 claude registry；
        #      claude 的 /path/a /path/b 不应进 codex registry。
        assert "/path/c" not in _claude_cwds(tmp_path)
        assert "/path/a" not in _codex_cwds(tmp_path)
        assert "/path/b" not in _codex_cwds(tmp_path)


# ---------------------------------------------------------------------------
# 3. claude bootstrap 失败不阻塞 codex
# ---------------------------------------------------------------------------


class TestIsolationBetweenClaudeAndCodex:
    def test_claude_bootstrap_failure_does_not_block_codex(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo_root = "/abs/worktree/kongming-agent"

        def _boom(home: Path, repo_root: str) -> None:
            raise RuntimeError("simulated claude bootstrap failure")

        # 在 _bootstrap_projects_registry 函数内部 import 之前替换源模块属性。
        monkeypatch.setattr(
            "hosts.web.integrations.claude_code.projects_registry.bootstrap_register_self",
            _boom,
        )

        with caplog.at_level(logging.WARNING, logger="hosts.web.app"):
            _bootstrap_projects_registry(tmp_path, repo_root)

        # codex registry 仍应正确登记 repo_root
        assert codex_projects_path(tmp_path).is_file()
        assert _codex_cwds(tmp_path) == [repo_root]

        # caplog 应有 warning 提示 claude bootstrap 失败
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "bootstrap" in r.getMessage().lower() and "claude" in r.getMessage().lower()
            for r in warnings
        ), f"expected claude bootstrap warning, got: {[r.getMessage() for r in warnings]}"

        # claude registry 不应被创建（bootstrap 抛了，migrate 没找到任何
        # claude_code metadata 也不会写盘）
        assert not claude_projects_path(tmp_path).is_file()


# ---------------------------------------------------------------------------
# 4. migrate 失败不阻塞 bootstrap
# ---------------------------------------------------------------------------


class TestMigrateFailureDoesNotBlockBootstrap:
    def test_migrate_failure_keeps_bootstrap_registration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo_root = "/abs/worktree/kongming-agent"

        def _boom(home: Path) -> int:
            raise RuntimeError("simulated migrate failure")

        # 替换两个 registry 的 migrate（两个独立 try/except，但都应被容错）
        monkeypatch.setattr(
            "hosts.web.integrations.claude_code.projects_registry.migrate_from_thread_metadata",
            _boom,
        )
        monkeypatch.setattr(
            "hosts.web.integrations.codex.projects_registry.migrate_from_thread_metadata",
            _boom,
        )

        with caplog.at_level(logging.WARNING, logger="hosts.web.app"):
            # 不应抛
            _bootstrap_projects_registry(tmp_path, repo_root)

        # registry 仍登记了 repo_root
        assert _claude_cwds(tmp_path) == [repo_root]
        assert _codex_cwds(tmp_path) == [repo_root]

        # caplog 至少含一条 migrate 失败 warning
        warnings = [r.getMessage().lower() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("migrate" in m for m in warnings), f"expected migrate warning, got: {warnings}"

    def test_only_claude_migrate_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """单独让 claude migrate 抛——codex migrate 仍应正常工作。"""
        repo_root = "/abs/worktree/kongming-agent"

        # 给 codex 一条可迁的 metadata
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-cccccccccccc",
                backend_kind="codex",
                cwd="/codex/path",
            ),
        )

        def _boom_claude(home: Path) -> int:
            raise RuntimeError("simulated claude migrate failure")

        monkeypatch.setattr(
            "hosts.web.integrations.claude_code.projects_registry.migrate_from_thread_metadata",
            _boom_claude,
        )

        with caplog.at_level(logging.WARNING, logger="hosts.web.app"):
            _bootstrap_projects_registry(tmp_path, repo_root)

        # claude bootstrap 仍跑过 → 含 repo_root
        assert _claude_cwds(tmp_path) == [repo_root]
        # codex 完整：bootstrap + migrate 都跑过
        assert set(_codex_cwds(tmp_path)) == {"/codex/path", repo_root}


# ---------------------------------------------------------------------------
# 5. 幂等
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_double_bootstrap_no_duplicate_entries(self, tmp_path: Path) -> None:
        repo_root = "/abs/worktree/kongming-agent"

        _bootstrap_projects_registry(tmp_path, repo_root)
        _bootstrap_projects_registry(tmp_path, repo_root)

        # 不应重复
        assert _claude_cwds(tmp_path) == [repo_root]
        assert _codex_cwds(tmp_path) == [repo_root]

    def test_double_bootstrap_with_metadata_no_duplicates(self, tmp_path: Path) -> None:
        """带 metadata 的二次 bootstrap：迁移结果也不应重复。"""
        repo_root = "/abs/worktree/kongming-agent"
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-aaaaaaaaaaaa",
                backend_kind="claude_code",
                cwd="/path/a",
            ),
        )
        write_thread_metadata(
            tmp_path,
            _make_meta(
                thread_id="thread-cccccccccccc",
                backend_kind="codex",
                cwd="/path/c",
            ),
        )

        _bootstrap_projects_registry(tmp_path, repo_root)
        _bootstrap_projects_registry(tmp_path, repo_root)

        assert sorted(_claude_cwds(tmp_path)) == sorted(["/path/a", repo_root])
        assert sorted(_codex_cwds(tmp_path)) == sorted(["/path/c", repo_root])

        # 验落盘文件没增长出重复 projects 项
        claude_data = json.loads(claude_projects_path(tmp_path).read_text())
        codex_data = json.loads(codex_projects_path(tmp_path).read_text())
        claude_cwds_in_file = [p["cwd"] for p in claude_data["projects"]]
        codex_cwds_in_file = [p["cwd"] for p in codex_data["projects"]]
        assert len(claude_cwds_in_file) == len(set(claude_cwds_in_file))
        assert len(codex_cwds_in_file) == len(set(codex_cwds_in_file))
