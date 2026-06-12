"""manage-config-tab dev-checklist #6 — router 薄壳单测。

只覆盖"manager → HTTP status"翻译逻辑，不真实读写 yaml / 不 spawn 子进程：

1. ``GET /api/manage/config/schema``           → 200 + 含 fields/groups
2. ``POST /api/manage/config/save`` ConflictError      → 409
3. ``POST /api/manage/config/save`` ValidationFailedError → 422 + errors 透传
4. ``POST /api/manage/config/restart`` RestartScriptNotFoundError → 503
5. ``POST /api/manage/config/restart`` happy → 200 + restarting/pid

完整的"manager 行为"由 ``test_manager.py`` 覆盖；本文件只断**异常翻译表**。

测试构造 mini FastAPI app，直接挂 :data:`router` 并把 mock ConfigManager
塞进 ``app.state.config_manager``，绕过 auth/CSRF 中间件——保持 router 单
测的隔离性。完整端点（带 create_app + 真 ConfigManager）由 Wave 5 #8 task
做 e2e 覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.dashboard.config.restart import RestartScriptNotFoundError
from hosts.web.dashboard.config.router import router as dashboard_config_router
from infrastructure.config.manager import (
    EffectiveResponse,
    RawResponse,
    SavePatchResponse,
    SchemaResponse,
)
from infrastructure.config.writer import (
    ConflictError,
    PatchItem,
    ValidationFailedError,
)

router_mod = import_module("hosts.web.dashboard.config.router")

# ---------------------------------------------------------------------------
# fake ConfigManager
# ---------------------------------------------------------------------------


@dataclass
class _FakeConfigManager:
    """最小 ConfigManager 桩：每个方法可单独配置返回值或异常。

    与真 :class:`ConfigManager` 保持 duck-typed 兼容（被 router 通过
    ``request.app.state.config_manager`` 访问）。`raise_on_*` 字段命中即
    抛对应异常，否则返回 `*_return` 桩值。
    """

    schema_return: SchemaResponse | None = None
    effective_return: EffectiveResponse | None = None
    raw_return: RawResponse | None = None
    save_return: SavePatchResponse | None = None
    raise_on_save: Exception | None = None
    last_save_patch: list[PatchItem] | None = None
    last_save_expected_mtime: float | None = None

    def read_schema(self) -> SchemaResponse:
        assert self.schema_return is not None
        return self.schema_return

    def read_effective(self) -> EffectiveResponse:
        assert self.effective_return is not None
        return self.effective_return

    def read_raw(self) -> RawResponse:
        assert self.raw_return is not None
        return self.raw_return

    def save_patch(self, patch: list[PatchItem], expected_mtime: float) -> SavePatchResponse:
        # 记录调用参数，便于断言 router 是否把 PatchItemBody 正确转 PatchItem。
        self.last_save_patch = list(patch)
        self.last_save_expected_mtime = expected_mtime
        if self.raise_on_save is not None:
            raise self.raise_on_save
        assert self.save_return is not None
        return self.save_return


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_manager() -> _FakeConfigManager:
    return _FakeConfigManager()


@pytest.fixture
def client(fake_manager: _FakeConfigManager) -> TestClient:
    """挂 router 到一个最小 FastAPI app，注入 fake ConfigManager。

    不走 create_app（避免依赖 secrets / threads / lifespan）；router 只
    通过 ``request.app.state.config_manager`` 取实例，桩注入即可。
    """
    app = FastAPI()
    app.state.config_manager = fake_manager
    app.state.config_restart_repo_root = "/tmp/fake_repo"
    app.include_router(dashboard_config_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 端点 smoke 用例（5 个 HTTP status 映射）
# ---------------------------------------------------------------------------


def test_get_schema_returns_200_with_fields(
    client: TestClient, fake_manager: _FakeConfigManager
) -> None:
    """GET /schema 200，body 含 fields 与 groups 两个 key。

    schema 内部由 :mod:`infrastructure.config.schema` 真源生成，这里桩
    一个最小 SchemaResponse（fields/groups 都给空 list）即可断 200 +
    结构正确。
    """
    fake_manager.schema_return = SchemaResponse(fields=[], groups=[])
    resp = client.get("/api/manage/config/schema")
    assert resp.status_code == 200
    payload = resp.json()
    assert "fields" in payload
    assert "groups" in payload
    assert payload["fields"] == []
    assert payload["groups"] == []


def test_save_conflict_returns_409(client: TestClient, fake_manager: _FakeConfigManager) -> None:
    """save_patch 抛 :class:`ConflictError` → 409。

    同时断 router 正确把请求体的 ``patch`` 数组反序列化成 ``PatchItem``
    再传给 manager（last_save_patch 记录调用值）。
    """
    from pathlib import Path

    fake_manager.raise_on_save = ConflictError(
        expected_mtime=1.0,
        actual_mtime=2.0,
        path=Path("/tmp/fake.yaml"),
    )
    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "model.temperature", "value": 0.7}],
            "expected_mtime": 1.0,
        },
    )
    assert resp.status_code == 409
    assert "mtime conflict" in resp.json()["detail"]
    # 断 router 转 PatchItem 正确（不要静默吞参数）
    assert fake_manager.last_save_patch is not None
    assert len(fake_manager.last_save_patch) == 1
    assert fake_manager.last_save_patch[0].path == "model.temperature"
    assert fake_manager.last_save_patch[0].value == 0.7
    assert fake_manager.last_save_expected_mtime == 1.0


def test_save_validation_failed_returns_422(
    client: TestClient, fake_manager: _FakeConfigManager
) -> None:
    """save_patch 抛 :class:`ValidationFailedError` → 422 + errors 透传。

    422 body 形态 ``{"detail": {"errors": [...]}}``——前端按 detail.errors
    渲染每字段错误。
    """
    errors: list[dict[str, Any]] = [
        {"path": "model.temperature", "message": "must be in [0, 2]"},
    ]
    fake_manager.raise_on_save = ValidationFailedError(errors)
    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "model.temperature", "value": 99.0}],
            "expected_mtime": 1.0,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["errors"] == errors


def test_restart_script_not_found_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """trigger_restart 抛 :class:`RestartScriptNotFoundError` → 503。"""
    monkeypatch.setattr(
        router_mod,
        "trigger_web_restart",
        lambda repo_root: (_ for _ in ()).throw(
            RestartScriptNotFoundError("./start.sh not found at /tmp/fake_repo")
        ),
    )
    resp = client.post("/api/manage/config/restart")
    assert resp.status_code == 503
    assert "start.sh not found" in resp.json()["detail"]


def test_restart_happy_returns_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """trigger_restart 成功 → 200 + restarting/pid 字段。"""
    monkeypatch.setattr(
        router_mod,
        "trigger_web_restart",
        lambda repo_root: 12345,
    )
    resp = client.post("/api/manage/config/restart")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restarting"] is True
    assert payload["pid"] == 12345
