"""Slash catalog REST route 合同测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.routers.slash_candidates import router as slash_candidates_router
from hosts.web.routers.slash_catalog import router as slash_catalog_router
from hosts.web.slash_catalog import (
    SlashCatalogGroupDTO,
    SlashCatalogGroupNotFound,
    SlashCatalogItemDTO,
)


class _FakeSlashCatalogManager:
    """测试用 catalog manager，记录请求上下文并返回固定 DTO。"""

    def __init__(self) -> None:
        self.thread_ids: list[str | None] = []
        self.backend_kinds: list[str | None] = []

    async def list_groups(self, context):  # type: ignore[no-untyped-def]
        """返回固定 groups，输入为 context，输出为 group DTO。"""
        self.thread_ids.append(context.thread_id)
        self.backend_kinds.append(context.backend_kind)
        return [
            SlashCatalogGroupDTO(
                id="workflow",
                title="Workflow",
                description="Workflow group",
                order=10,
                item_count=1,
            )
        ]

    async def list_group_items(self, group_id, context):  # type: ignore[no-untyped-def]
        """返回固定 group items，输入为 group_id/context，输出为 group/items。"""
        self.thread_ids.append(context.thread_id)
        self.backend_kinds.append(context.backend_kind)
        if group_id != "workflow":
            raise SlashCatalogGroupNotFound(group_id, ("workflow", "command", "skill"))
        group = SlashCatalogGroupDTO(
            id="workflow",
            title="Workflow",
            description="Workflow group",
            order=10,
            item_count=1,
        )
        item = SlashCatalogItemDTO(
            id="workflow:fake",
            group_id="workflow",
            kind="workflow_strategy",
            title="Fake Workflow",
            description="Fake workflow summary",
            source_ref="workflow_strategy:fake",
            order=0,
            section_id="registered",
            insert_text="/workflow fake ",
            metadata={"mode": "fake"},
        )
        return group, [item]

    async def list_legacy_candidates(self, context):  # type: ignore[no-untyped-def]
        """返回 legacy candidates，输入为 context，输出为兼容 DTO。"""
        self.thread_ids.append(context.thread_id)
        self.backend_kinds.append(context.backend_kind)
        return [
            {
                "slash": "/clear",
                "title": "Clear",
                "description": "Clear command",
                "source": "command",
            }
        ]


class _FakeThreadManager:
    """提供 slash route 解析 backend_kind 所需的最小 ThreadManager 门户。"""

    def __init__(self, backend_kind: str) -> None:
        self._metadata = SimpleNamespace(id="thread-1", backend_kind=backend_kind)

    def get_cell(self, _thread_id: str) -> None:
        return None

    def list_threads(self) -> list[SimpleNamespace]:
        return [self._metadata]


def test_list_slash_catalog_passes_thread_id_and_returns_groups(tmp_path: Path) -> None:
    """验证 catalog groups route，输入为 thread_id query，输出为 groups DTO。"""
    manager = _FakeSlashCatalogManager()
    client = TestClient(_app(tmp_path, manager))

    response = client.get("/api/slash-catalog?thread_id=thread-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["groups"][0]["id"] == "workflow"
    assert payload["groups"][0]["item_count"] == 1
    assert manager.thread_ids == ["thread-1"]
    assert manager.backend_kinds == ["generic_chat"]


def test_list_slash_catalog_group_returns_items(tmp_path: Path) -> None:
    """验证 group route，输入为 workflow group，输出为 workflow_strategy item。"""
    client = TestClient(_app(tmp_path, _FakeSlashCatalogManager()))

    response = client.get("/api/slash-catalog/groups/workflow?thread_id=thread-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["group"]["id"] == "workflow"
    assert payload["items"][0]["kind"] == "workflow_strategy"
    assert payload["items"][0]["insert_text"] == "/workflow fake "


def test_unknown_slash_catalog_group_returns_404(tmp_path: Path) -> None:
    """验证未知 group 错误，输入为 missing group，输出为 404 和可用 group 列表。"""
    client = TestClient(_app(tmp_path, _FakeSlashCatalogManager()))

    response = client.get("/api/slash-catalog/groups/missing")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "slash_catalog.group_not_found"
    assert detail["available_group_ids"] == ["workflow", "command", "skill"]


def test_legacy_slash_candidates_uses_catalog_manager(tmp_path: Path) -> None:
    """验证 legacy route，输入为空请求，输出为 command+skill flat DTO。"""
    client = TestClient(_app(tmp_path, _FakeSlashCatalogManager()))

    response = client.get("/api/slash-candidates")

    assert response.status_code == 200
    assert response.json() == [
        {
            "slash": "/clear",
            "title": "Clear",
            "description": "Clear command",
            "source": "command",
        }
    ]


def _app(
    tmp_path: Path,
    manager: _FakeSlashCatalogManager,
    *,
    backend_kind: str = "generic_chat",
) -> FastAPI:
    """构造轻量 FastAPI app，输入为 manager，输出为测试 app。"""
    app = FastAPI()
    app.state.slash_catalog_manager = manager
    app.state.kongming_home = tmp_path / "home"
    app.state.workspace_root = tmp_path
    app.state.thread_manager = _FakeThreadManager(backend_kind)
    app.include_router(slash_catalog_router)
    app.include_router(slash_candidates_router)
    return app
