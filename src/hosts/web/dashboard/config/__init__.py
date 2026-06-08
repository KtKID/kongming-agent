"""配置管理子模块（dashboard/config）— 单一对外入口。

本子模块对外仅暴露：

- :class:`ConfigManager`: 操作配置的唯一类（5 个方法 + 一组 DTO + 异常）
- ``router``: FastAPI router，挂 ``/api/manage/config/*`` 5 个 HTTP 端点

sibling 模块禁止跨过这两个符号直接 import ``schema`` / ``writer`` /
``restart`` 内部细节——所有触达统一收口到 :class:`ConfigManager`，便于：

- 后续加缓存 / 锁 / 审计层时只动一处；
- 单测 mock 时只 patch 一个 facade；
- 装配点（router 注册）只持有一个 :class:`ConfigManager` 实例即可。

子文件分工（仅在本子模块内部互相 import）：

- ``schema.py``: ``FieldMeta`` + 字段元数据真源
- ``writer.py``: ruamel.yaml round-trip 写回 + 文件锁 + 校验回滚
- ``restart.py``: detached subprocess 调 ``./start.sh web restart``
- ``manager.py``: :class:`ConfigManager` 类（聚合上面三者）
- ``router.py``: FastAPI 薄壳，把 5 个方法映射成 HTTP 端点
"""

from hosts.web.dashboard.config.manager import ConfigManager
from hosts.web.dashboard.config.router import router

__all__ = ["ConfigManager", "router"]
