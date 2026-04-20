"""Pytest 根 conftest。

第一版先只放最小 async fixture 骨架占位。具体单测 / e2e fixture 由后续批次补齐，
避免过早把测试工具层设计死。

当前约定：

- ``asyncio_mode = auto``（在 pyproject.toml 中配置），所以直接写 ``async def test_xxx``
  即可，不需要额外装饰。
- 需要跨模块共享的 fixture 才放到此文件；仅模块内部复用的 fixture 应放到
  对应目录下的 ``conftest.py``。
"""

from __future__ import annotations

# placeholder: 后续批次按需补充共享 fixture。
