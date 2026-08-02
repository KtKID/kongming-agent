"""``web.usage.usage_token_v2`` —— Token 用量唯一访问入口（v2 façade 模式）。

### 5 大硬性约束（``.importlinter`` Contract 9 强制）

1. **manager 是 façade**：不持有状态、不写盘、不缓存
2. **真源在 SDK**：所有 token 数据**唯一**来自 SDK 自己写的 jsonl/rollout
3. **分通道不统一**：每个通道（claude / openai）有自己的 DTO，1:1 映射 SDK 原生字段
4. **manager 是唯一入口**：外部只调 ``manager.get_thread_usage()``
5. **import-linter 强制**：CI gate；外部禁止直接 import 派生器 / 私有 DTO

### 外部可用的符号

公共 DTO（外部接收返回值，不可主动构造）：

- ``ClaudeUsage`` / ``ClaudeCacheCreation``
- ``CodexUsage`` / ``CodexTokenBreakdown`` / ``CodexRateLimits``
  / ``CodexRateLimitWindow``
- ``GenericChatAnthropicUsage`` / ``GenericChatOpenAIUsage``
- ``ThreadUsage``（union 别名）

manager + 注入 Protocol（装配层用）：

- ``UsageTokenManager``
- ``ThreadMetadataReader`` / ``ClaudeJsonlLocator`` / ``CodexRolloutLocator``
  / ``GenericChatSessionLocator``

### 故意不 export

- ``_derive_claude`` / ``_derive_codex`` / ``_derive_generic``：私有派生器
- ``_models``：DTO 实现细节
- ``_model_context_table``：模型 context window 表

设计依据：[`docs/usage-token-v2/`](../../../docs/usage-token-v2/README.md)
"""

from hosts.web.usage.usage_token_v2._models import (
    ClaudeCacheCreation,
    ClaudeUsage,
    CodexRateLimits,
    CodexRateLimitWindow,
    CodexTokenBreakdown,
    CodexUsage,
    GenericChatAnthropicUsage,
    GenericChatOpenAIUsage,
    ThreadUsage,
)
from hosts.web.usage.usage_token_v2.manager import (
    ClaudeJsonlLocator,
    CodexRolloutLocator,
    GenericChatSessionLocator,
    ThreadMetadataReader,
    UsageTokenManager,
)

__all__ = [
    # 公共 DTO
    "ClaudeCacheCreation",
    "ClaudeUsage",
    "CodexRateLimits",
    "CodexRateLimitWindow",
    "CodexTokenBreakdown",
    "CodexUsage",
    "GenericChatAnthropicUsage",
    "GenericChatOpenAIUsage",
    "ThreadUsage",
    # manager + 注入 Protocol
    "ClaudeJsonlLocator",
    "CodexRolloutLocator",
    "GenericChatSessionLocator",
    "ThreadMetadataReader",
    "UsageTokenManager",
]
