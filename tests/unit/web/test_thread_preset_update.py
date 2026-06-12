"""Generic Chat thread preset 切换单测。

覆盖：
1. ``ThreadManager.update_thread_preset`` 写盘并同步已 boot cell metadata。
2. idle cell 切换后立即重建 runtime，下一次发送可用新 preset。
3. running cell 切换时先跳过热切换，run 完成后懒刷新 runtime。
4. ``PATCH /api/threads/{tid}/preset`` 校验 preset 存在并返回更新后 DTO。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from hosts.web.threads.metadata import read_thread_metadata
from infrastructure.config.models import Config


class _FakeRuntime:
    """测试 runtime：只记录是否关闭。"""

    def __init__(self, preset_id: str) -> None:
        self.preset_id = preset_id
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "llm_presets": [
                    {
                        "id": "preset-1",
                        "display_name": "Preset 1",
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "model-1",
                    },
                    {
                        "id": "preset-2",
                        "display_name": "Preset 2",
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "model-2",
                    },
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_update_thread_preset_rebuilds_idle_cell_runtime(tmp_path: Path) -> None:
    """idle cell 切换 preset 后 runtime 立即重建。"""
    from hosts.web.threads.manager import ThreadManager

    builds: list[str] = []

    async def factory(
        tid: str,
        pid: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, object]:
        del tid, adapter, sinks
        builds.append(pid)
        return _FakeRuntime(pid), object()

    tm = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await tm.create_thread("t1", "preset-1")
    cell = await tm.boot_or_attach(meta.id)
    old_runtime = cell.runtime

    updated = await tm.update_thread_preset(meta.id, "preset-2")

    assert updated.preset_id == "preset-2"
    assert cell.metadata.preset_id == "preset-2"
    assert cell.runtime_preset_id == "preset-2"
    assert isinstance(old_runtime, _FakeRuntime)
    assert old_runtime.closed is True
    assert builds == ["preset-1", "preset-2"]
    on_disk = read_thread_metadata(tmp_path, meta.id)
    assert on_disk is not None
    assert on_disk.preset_id == "preset-2"


@pytest.mark.asyncio
async def test_update_thread_preset_running_cell_refreshes_after_run(
    tmp_path: Path,
) -> None:
    """running cell 切换时不热切，下一次发送前懒刷新。"""
    from hosts.web.threads.manager import ThreadManager

    builds: list[str] = []

    async def factory(
        tid: str,
        pid: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, object]:
        del tid, adapter, sinks
        builds.append(pid)
        return _FakeRuntime(pid), object()

    tm = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await tm.create_thread("t1", "preset-1")
    cell = await tm.boot_or_attach(meta.id)

    release = asyncio.Event()

    async def running_until_released() -> None:
        await release.wait()

    task = asyncio.create_task(running_until_released())
    cell.current_run_task = task
    await tm.update_thread_preset(meta.id, "preset-2")
    assert cell.metadata.preset_id == "preset-2"
    assert cell.runtime_preset_id == "preset-1"
    assert builds == ["preset-1"]

    release.set()
    await task
    refreshed = await tm.ensure_cell_runtime_preset_current(meta.id)

    assert refreshed is True
    assert cell.runtime_preset_id == "preset-2"
    assert builds == ["preset-1", "preset-2"]


@pytest.mark.asyncio
async def test_update_thread_preset_keeps_old_runtime_when_refresh_fails(
    tmp_path: Path,
) -> None:
    """runtime 刷新失败时回滚 metadata，旧 runtime 保留。"""
    from hosts.web.threads.manager import ThreadManager, ThreadPresetRefreshError

    builds: list[str] = []

    async def factory(
        tid: str,
        pid: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, object]:
        del tid, adapter, sinks
        builds.append(pid)
        if pid == "preset-2":
            raise RuntimeError("factory down")
        return _FakeRuntime(pid), object()

    tm = ThreadManager(_make_cfg(), kongming_home=tmp_path, runtime_factory=factory)
    meta = await tm.create_thread("t1", "preset-1")
    cell = await tm.boot_or_attach(meta.id)
    old_runtime = cell.runtime

    with pytest.raises(ThreadPresetRefreshError):
        await tm.update_thread_preset(meta.id, "preset-2")

    assert cell.metadata.preset_id == "preset-1"
    assert cell.runtime is old_runtime
    assert cell.runtime_preset_id == "preset-1"
    assert builds == ["preset-1", "preset-2"]
    on_disk = read_thread_metadata(tmp_path, meta.id)
    assert on_disk is not None
    assert on_disk.preset_id == "preset-1"


@pytest.mark.asyncio
async def test_patch_thread_preset_validates_and_returns_updated_dto(
    tmp_path: Path,
) -> None:
    """REST patch 校验 preset 存在，并返回更新后 thread metadata。"""
    from hosts.web.protocol import UpdateThreadPresetRequest
    from hosts.web.routers.threads import update_thread_preset as route_update_thread_preset
    from hosts.web.threads.manager import ThreadManager

    async def factory(
        tid: str,
        pid: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, object]:
        del tid, adapter, sinks
        return _FakeRuntime(pid), object()

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await tm.create_thread("t1", "preset-1")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=cfg, thread_manager=tm))
    )

    updated = await route_update_thread_preset(
        meta.id,
        UpdateThreadPresetRequest(preset_id="preset-2"),
        request,  # type: ignore[arg-type]
    )
    assert updated.preset_id == "preset-2"

    with pytest.raises(HTTPException) as exc_info:
        await route_update_thread_preset(
            meta.id,
            UpdateThreadPresetRequest(preset_id="missing-preset"),
            request,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 400
    assert "unknown preset_id" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_patch_thread_preset_returns_error_when_runtime_refresh_fails(
    tmp_path: Path,
) -> None:
    """REST patch 在 runtime 刷新失败时返回错误，并保留旧 preset。"""
    from hosts.web.protocol import UpdateThreadPresetRequest
    from hosts.web.routers.threads import update_thread_preset as route_update_thread_preset
    from hosts.web.threads.manager import ThreadManager

    async def factory(
        tid: str,
        pid: str,
        adapter: Any,
        sinks: list[Any],
    ) -> tuple[_FakeRuntime, object]:
        del tid, adapter, sinks
        if pid == "preset-2":
            raise RuntimeError("factory down")
        return _FakeRuntime(pid), object()

    cfg = _make_cfg()
    tm = ThreadManager(cfg, kongming_home=tmp_path, runtime_factory=factory)
    meta = await tm.create_thread("t1", "preset-1")
    await tm.boot_or_attach(meta.id)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=cfg, thread_manager=tm))
    )

    with pytest.raises(HTTPException) as exc_info:
        await route_update_thread_preset(
            meta.id,
            UpdateThreadPresetRequest(preset_id="preset-2"),
            request,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 500
    assert "failed to refresh runtime" in str(exc_info.value.detail)
    on_disk = read_thread_metadata(tmp_path, meta.id)
    assert on_disk is not None
    assert on_disk.preset_id == "preset-1"
