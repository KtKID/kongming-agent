"""Web 路由层（v0.1.5）。

子模块：

- :mod:`web.routers.auth`：登录 / 登出 / 当前用户
- :mod:`web.routers.threads`：thread CRUD
- :mod:`web.routers.presets`：LLM preset 列表（脱敏）
- :mod:`web.routers.manage`：管理页 cell 列表 + 手动停止
- :mod:`web.routers.health`：``GET /api/health`` 重启探测端点（公开，无需登录）

每个子模块导出一个 ``router: APIRouter``，由 :func:`web.app.create_app`
统一挂载。
"""
