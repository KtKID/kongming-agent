"""Web runtime factory 的审批门户和 thread cwd 回落集成测试。

关键流程：验证 generic_chat 装配复用 app 级 PermissionsManager，并验证
thread cwd 缺失时按 workspace root、KONGMING_HOME 顺序解析稳定默认目录。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.run import _build_manager_and_inbox_sink, _resolve_default_cwd_for_thread
from hosts.web.threads.metadata import ThreadMetadata
from safety.approval.manager import ApprovalManager, reset_for_testing
from safety.approval.permissions_manager import PermissionsManager


@pytest.fixture(autouse=True)
def _reset_manager_singleton() -> Iterator[None]:
    """隔离 ApprovalManager 单例，避免组合运行互相污染。"""
    reset_for_testing()
    yield
    reset_for_testing()


def _make_thread_meta(thread_id: str = "thread-abc123def456", *, cwd: str = "") -> ThreadMetadata:
    """构造满足 thread id 合同的最小 metadata。"""
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="default",
        cwd=cwd,
        created_at=time.time(),
        updated_at=time.time(),
    )


class _StubThreadManager:
    """提供 factory 所需的 thread 列表和审批门户注入入口。"""

    def __init__(self, metas: list[ThreadMetadata]) -> None:
        self._metas = metas
        self.approval_manager: ApprovalManager | None = None

    def list_threads(self) -> list[ThreadMetadata]:
        """返回隔离副本，模拟真实 ThreadManager 门户。"""
        return list(self._metas)

    def set_approval_manager(self, manager: ApprovalManager) -> None:
        """记录 runtime factory 注入的审批门户。"""
        self.approval_manager = manager


def _make_app(
    *,
    permissions_manager: PermissionsManager,
    thread_metas: list[ThreadMetadata] | None = None,
    workspace_root: Path | None = None,
) -> SimpleNamespace:
    """构造带 permissions 真源和 workspace 状态的最小 app。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            thread_manager=_StubThreadManager(thread_metas or []),
            workspace_root=workspace_root or Path("/proj/server-root"),
            permissions_manager=permissions_manager,
        )
    )


def test_manager_build_reuses_app_permissions_manager(tmp_path: Path) -> None:
    """generic_chat 装配复用 app 级 per-thread permissions 真源。"""
    permissions_manager = PermissionsManager(tmp_path)
    app = _make_app(permissions_manager=permissions_manager)

    manager = _build_manager_and_inbox_sink(app=app)

    assert isinstance(manager, ApprovalManager)
    assert manager.permissions_manager is permissions_manager
    assert app.state.approval_manager is manager
    assert app.state.thread_manager.approval_manager is manager
    assert not hasattr(app.state, "auto_approval_policy")


@pytest.mark.asyncio
async def test_adapter_close_before_manager_build_keeps_singleton_empty(tmp_path: Path) -> None:
    """adapter 提前关闭不会抢先创建 ApprovalManager 单例。"""
    import safety.approval.manager as approval_manager_mod

    adapter = WebHostAdapter(object())
    await adapter.close()
    assert approval_manager_mod._singleton is None

    manager = _build_manager_and_inbox_sink(
        app=_make_app(permissions_manager=PermissionsManager(tmp_path))
    )
    assert isinstance(manager, ApprovalManager)


def test_resolve_default_cwd_uses_thread_meta_when_set(tmp_path: Path) -> None:
    """thread cwd 有值时直接作为运行目录。"""
    meta = _make_thread_meta(cwd="/proj/bound")
    app = _make_app(
        permissions_manager=PermissionsManager(tmp_path),
        thread_metas=[meta],
    )
    assert _resolve_default_cwd_for_thread(app, meta.id) == "/proj/bound"


def test_resolve_default_cwd_falls_back_to_server_root(tmp_path: Path) -> None:
    """thread cwd 为空时使用 app workspace root。"""
    meta = _make_thread_meta(cwd="")
    server_root = tmp_path / "server-root"
    app = _make_app(
        permissions_manager=PermissionsManager(tmp_path),
        thread_metas=[meta],
        workspace_root=server_root,
    )
    assert _resolve_default_cwd_for_thread(app, meta.id) == server_root.resolve().as_posix()


def test_resolve_default_cwd_falls_back_when_metadata_missing(tmp_path: Path) -> None:
    """thread metadata 缺失时使用 app workspace root。"""
    server_root = tmp_path / "server-root"
    app = _make_app(
        permissions_manager=PermissionsManager(tmp_path),
        workspace_root=server_root,
    )
    assert (
        _resolve_default_cwd_for_thread(app, "thread-abc123def456")
        == server_root.resolve().as_posix()
    )


def test_resolve_default_cwd_falls_back_to_kongming_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app 尚未回挂时使用 KONGMING_HOME 作为稳定目录。"""
    home = tmp_path / "kongming-home"
    monkeypatch.setenv("KONGMING_HOME", str(home))
    assert Path(_resolve_default_cwd_for_thread(None, "thread-abc123def456")) == home.resolve()
