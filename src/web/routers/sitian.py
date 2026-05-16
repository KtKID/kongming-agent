"""司天报告 REST 路由（sitian-web-report-dialog #2）。

端点：

- ``GET /api/sitian/report`` — 返回最新司天报告原始 JSON

依赖与设计：

- ``request.app.state.config``：由 :func:`web.app.create_app` 在 lifespan 启动
  时挂入，供路由解析司天产物目录（``cfg.sitian.output_subdir`` 支持单 channel
  分仓，如 ``"claude"`` → ``<root>/claude/`` ）
- 走默认 :class:`web.auth.AuthMiddleware` 与 :class:`web.csrf.CSRFMiddleware`；
  未登录访问由中间件统一返 401，**本路由不再显式 Depends**
- 出错语义：
    - 报告文件不存在 → ``404`` + ``{"error": "no_report"}``
    - JSON 损坏 / schema 非法 → ``500`` + ``{"error": "report_corrupted"}``
    - 其它意外异常 → 不在本层兜底，让 FastAPI 默认 500 返回

实现要点：

- 每次请求重新构造 :class:`sitian.store.SiTianRecordsStore`：成本只是 ``Path`` /
  ``asyncio.Lock`` 实例化，避免在 :mod:`web.app` 全局再持一份单例（司天根目录
  由 cfg 决定，cfg 是 lifespan-scoped 单例，与请求生命周期解耦即可）。
- 直接返回 ``dict``：FastAPI 默认 JSON 编码即可，不做 DTO 包装——前端按
  ``docs/sitian-v1/`` 的 schema 直接消费原始字段，DTO 包装反而引入字段漂移风险。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sitian.store import SiTianRecordsStore, resolve_sitian_root
from web.protocol import ErrorResponseDTO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sitian", tags=["sitian"])


_DEFAULT_CHANNEL = "claude"


@router.get("/report", response_model=None)
async def get_sitian_report(request: Request) -> JSONResponse | dict[str, object]:
    """读取最新司天报告 JSON。

    路径解析：
        ``<resolve_sitian_root()>/<cfg.sitian.output_subdir or "claude">``

    说明 channel 默认值：
        web server 和司天 CLI 走不同的 config 实例（web 用 ``app.state.config``，
        CLI 用 ``--config`` 传入），两边 ``output_subdir`` 经常不一致。本任务
        范围只覆盖 claude channel，故 web 路由侧 fallback 到 ``"claude"``，
        与盘上实际写入位置 ``.kongming/sitian/claude/`` 对齐。

    返回（错误统一走 :class:`ErrorResponseDTO` envelope，
    具体子类型放 ``details.reason``，前端按 HTTP status 区分大类、按 reason 区分细类）：
        - 200：报告 dict 原样返回
        - 404：报告不存在 → ``{"error_code":"internal","message":"...","details":{"reason":"no_report"}}``
        - 500：报告存在但 JSON 损坏 / 顶层非 object → ``details.reason="report_corrupted"``
    """
    cfg = request.app.state.config
    output_subdir = (cfg.sitian.output_subdir or _DEFAULT_CHANNEL).strip()
    root = resolve_sitian_root() / output_subdir

    store = SiTianRecordsStore(root)

    try:
        report = await store.load_sitian_report()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "sitian report corrupted at %s: %s",
            store.root_dir,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponseDTO(
                error_code="internal",
                message="sitian report file corrupted",
                details={"reason": "report_corrupted"},
            ).model_dump(),
        )

    if report is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponseDTO(
                error_code="internal",
                message="sitian report not generated yet",
                details={"reason": "no_report"},
            ).model_dump(),
        )

    return report


__all__ = ["router"]
