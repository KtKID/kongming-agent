"""``web.usage_token`` —— Token 用量唯一访问入口（封装包）。

### 5 大硬性约束（``.importlinter`` Contract ``usage-token-encapsulation`` 强制）

1. **唯一入口**：``UsageTokenManager`` 是 token 用量的唯一对外接口
2. **数据结构封装**：``TokenUsage`` 内部类型禁止外部 import / 实例化
3. **manager 持有读写**：任何模块读 token 必须调 manager 查询 API；
   写 token 必须调 ``record_run_usage``
4. **channel 分发内部化**：anthropic / openai 字段映射、累加、派生在 manager 内部
5. **import-linter 强制**：CI gate，未通过不能 merge

### 外部可用的符号

仅以下 5 个：

- ``UsageTokenManager``：核心 manager
- ``UsageTokenSnapshot``：一轮快照 DTO
- ``ThreadUsageSummary``：thread 级累计 summary DTO
- ``UsageFrame``：WS S2C 帧
- ``ThreadMetadataIO``：落盘抽象 Protocol（外部 adapter 用）

### 故意不 export

- ``TokenUsage`` / ``_AnthropicTokenUsage`` / ``_OpenAITokenUsage`` —— 私有类型
- ``to_persisted_dict`` / ``from_persisted_dict`` / ``empty_usage_for_channel``
  —— 私有 helper
- ``upgrade_v7_to_v8`` —— 由 ``thread_metadata.py`` 单点白名单 import（任务#2 wiring）

设计依据：[`docs/usage-token/`](../../../docs/usage-token/README.md)
"""

from web.usage_token._models import (
    ThreadUsageSummary,
    UsageFrame,
    UsageTokenSnapshot,
)
from web.usage_token._persistence import ThreadMetadataIO
from web.usage_token.manager import UsageTokenManager

__all__ = [
    "UsageTokenManager",
    "UsageTokenSnapshot",
    "ThreadUsageSummary",
    "UsageFrame",
    "ThreadMetadataIO",
]
