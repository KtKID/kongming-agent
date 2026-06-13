"""manage 配置页 FastAPI 薄壳。

把 :class:`infrastructure.config.manager.ConfigManager` 的公开方法
按 1:1 映射成 HTTP endpoint，挂到 ``/api/manage/config/*``：

================  ==================================  ====================
HTTP method+path  ConfigManager 方法                  status 映射
================  ==================================  ====================
GET  /schema      :meth:`ConfigManager.read_schema`   200
GET  /effective   :meth:`ConfigManager.read_effective` 200
GET  /raw         :meth:`ConfigManager.read_raw`      200
POST /save        :meth:`ConfigManager.save_patch`    200 / 409 / 422
POST /restart     Web restart adapter                200 / 503
================  ==================================  ====================

设计原则：

- **不在本文件做业务判断**——所有"读 yaml / 写 yaml / spawn restart"逻辑
  全部下放到 :class:`ConfigManager`。router 只做协议层：参数解析 + 异常
  翻译成 HTTP status。
- **不持自有状态**——通过 ``request.app.state.config_manager`` 单一引用
  取实例。装配在 :func:`web.app.create_app` 完成（与
  ``app.state.config`` 同一段）。
- **异常翻译表（透传 manager 抛出的语义异常）**：

  - :class:`ConflictError` (writer mtime 冲突)        → 409
  - :class:`ValidationFailedError` (pydantic 校验失败) → 422
  - :class:`RestartScriptNotFoundError` (start.sh 缺失) → 503
  - 其他未捕获 → FastAPI 默认 500

  **不**用 :class:`web.errors.KongmingWebError` 体系——manager 抛的是
  writer / restart 自定义异常，不属于 KongmingWebError 家族，统一在本
  router 用 :class:`fastapi.HTTPException` 翻译，单一翻译出口。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hosts.web.dashboard.config.restart import RestartScriptNotFoundError
from hosts.web.dashboard.config.restart import trigger_restart as trigger_web_restart
from infrastructure.config.manager import (
    ConfigManager,
    EffectiveResponse,
    RawResponse,
    SavePatchResponse,
    SchemaResponse,
)
from infrastructure.config.writer import ConflictError, PatchItem, ValidationFailedError

router = APIRouter(prefix="/api/manage/config", tags=["manage-config"])


# ---------------------------------------------------------------------------
# 请求体 DTO
# ---------------------------------------------------------------------------


class PatchItemBody(BaseModel):
    """单条 patch 请求体（与 :class:`PatchItem` dataclass 镜像）。

    用 pydantic BaseModel 而不是直接接 ``PatchItem`` dataclass：FastAPI 对
    request body 的反序列化路径走 pydantic，BaseModel 表达力更稳。``value``
    用 ``Any`` 是因为 setting.yaml 的字段类型动态——既有 bool / float / str，
    也有 list / dict（dict-leaf 整段编辑）。具体类型校验由 writer 层 pydantic
    Config 二次校验兜底（失败抛 :class:`ValidationFailedError` → 422）。
    """

    path: str
    value: Any


class SavePatchRequestBody(BaseModel):
    """``POST /save`` 请求体。

    ``expected_mtime`` 由前端从上一次 ``GET /raw`` 拿到的 mtime 透传回来，
    供 writer 做冲突检测（不一致 → 409）。
    """

    patch: list[PatchItemBody]
    expected_mtime: float


@dataclass(frozen=True)
class RestartResponse:
    """Web restart 响应。"""

    restarting: bool
    pid: int


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def _manager(request: Request) -> ConfigManager:
    """从 ``app.state`` 取 :class:`ConfigManager` 单例。

    单独抽 helper 是为了：

    - 让 5 个端点的依赖获取统一一处，将来换 Depends() 风格只动这里；
    - 便于测试时通过 ``app.state.config_manager = fake`` 注入桩。

    Raises:
        HTTPException 500: ``app.state.config_manager`` 未装配（应该在
            :func:`web.app.create_app` 启动期就绪，缺失说明装配漂移）。
    """
    mgr = getattr(request.app.state, "config_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=500,
            detail="config_manager not configured on app.state",
        )
    return mgr  # type: ignore[no-any-return]


@router.get("/schema")
async def get_schema(request: Request) -> SchemaResponse:
    """字段元数据 + group 顺序。

    不接受任何参数；幂等可缓存。具体 schema 内容由
    :func:`infrastructure.config.schema.list_field_metas` 决定。
    """
    return _manager(request).read_schema()


@router.get("/effective")
async def get_effective(request: Request) -> EffectiveResponse:
    """当前生效配置 + 每字段来源 + env override 命中清单。"""
    return _manager(request).read_effective()


@router.get("/raw")
async def get_raw(request: Request) -> RawResponse:
    """yaml 原文 + path + mtime；编辑器视图与冲突检测同一份数据源。"""
    return _manager(request).read_raw()


@router.post("/save")
async def save_patch(body: SavePatchRequestBody, request: Request) -> SavePatchResponse:
    """差量写回 + 校验 + 计算需重启字段清单。

    异常翻译：

    - :class:`ConflictError` → 409 (mtime 冲突，前端应重新拉 /raw 再提交)
    - :class:`ValidationFailedError` → 422 (含每字段 errors 清单)
    """
    mgr = _manager(request)
    patch = [PatchItem(path=item.path, value=item.value) for item in body.patch]
    try:
        return mgr.save_patch(patch, expected_mtime=body.expected_mtime)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationFailedError as exc:
        raise HTTPException(
            status_code=422,
            detail={"errors": exc.errors},
        ) from exc


@router.post("/restart")
async def trigger_restart(request: Request) -> RestartResponse:
    """spawn ``./start.sh web restart``（detached）。

    异常翻译：

    - :class:`RestartScriptNotFoundError` → 503 (脚本缺失，服务不可用)

    不在本端点 sleep——是否给 HTTP 响应留时间 flush 由调用方编排。
    """
    repo_root = getattr(request.app.state, "config_restart_repo_root", None)
    if repo_root is None:
        raise HTTPException(
            status_code=500,
            detail="config_restart_repo_root not configured on app.state",
        )
    try:
        pid = trigger_web_restart(Path(repo_root))
        return RestartResponse(restarting=True, pid=pid)
    except RestartScriptNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


__all__ = ["router"]
