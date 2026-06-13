"""Health 探测路由（manage-config-tab #7）。

端点：

- ``GET /health`` — sidecar / desktop host 启动探测端点。
- ``GET /api/health`` — 仅返回 ``200 + {"status": "ok"}``，**不依赖任何
  ``app.state.*``**，用于"重启按钮"流程的恢复探测。

设计要点：

- **公开访问**：必须在 :func:`web.auth.middleware._is_path_allowlisted` 中加白名单。
  重启后 cookie 可能仍有效，但前端在 lifespan 完成前轮询时若被中间件挡掉
  会拿到 401，永远不会自动 reload。
- **零依赖**：lifespan 未跑完时 ``app.state.serializer`` / ``thread_manager``
  等都可能缺失，本端点不读它们也不抛。
- **幂等无副作用**：纯返回常量 JSON，可被任意频繁轮询。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/api/health")
async def get_health() -> dict[str, str]:
    """健康探测端点：返回 ``200 + {"status": "ok"}``。

    Returns:
        固定 ``{"status": "ok"}``；宿主只需断言 HTTP 200 即可继续启动流程。
    """
    return {"status": "ok"}


__all__ = ["router"]
