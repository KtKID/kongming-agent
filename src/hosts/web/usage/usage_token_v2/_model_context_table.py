"""模型 context window 上限内置表（v2，从 v1 迁过来一字不改）。

⚠️ **占位值**：除 ``claude-opus-4`` (1M) 由用户确认外，其余 12 个是基于 2026-05
公开文档/估计的占位值。

命中规则（与前端 ``lookupContextWindow`` 对齐）：
- exact match 优先
- 否则按 key 长度倒序尝试 prefix match（避免短前缀误命中）
- 未命中返回 ``None``，前端 StatusLine 退化为不显示百分比
"""

from __future__ import annotations

# key 跟前端 ``web/src/lib/model-context ts`` 字典 key 严格一致
DEFAULT_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # === OpenAI ===
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    # === Anthropic ===
    "claude-opus-4": 1_000_000,  # 用户 2026-05-14 确认
    "claude-sonnet-4": 200_000,  # placeholder
    "claude-haiku-4": 200_000,  # placeholder
    "claude-3-5-sonnet": 200_000,  # placeholder
    "claude-3-opus": 200_000,  # placeholder
    # === 国产 ===
    # GLM-5.2 官方支持 1M context；远端 model 名是 glm-5.2（catalog 里 preset 的
    # model 字段不带 [1m] 后缀，[1m] 仅在 display_name 作本地 UI 标识）。
    # 来源：https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2
    "glm-5.2": 1_000_000,
    "glm-5.1": 128_000,  # placeholder
    "glm-4": 128_000,  # placeholder
    "MiniMax-M3": 200_000,  # placeholder
    "deepseek-chat": 64_000,  # placeholder
    # === 本地 / 小模型 ===
    "gemma-4-e4b-it": 8_192,  # placeholder
}


def lookup_context_window(model_name: str) -> int:
    """按 v2 派生器需要的 int 接口（默认 0=未知）。

    Args:
        model_name: 模型名（来自 SDK message.model）

    Returns:
        context window 上限；未命中或空名返回 0
    """
    if not model_name:
        return 0
    # exact match
    if model_name in DEFAULT_MODEL_CONTEXT_WINDOWS:
        return DEFAULT_MODEL_CONTEXT_WINDOWS[model_name]
    # prefix match（按 key 长度倒序，避免短前缀误命中）
    for key in sorted(DEFAULT_MODEL_CONTEXT_WINDOWS.keys(), key=len, reverse=True):
        if model_name.startswith(key):
            return DEFAULT_MODEL_CONTEXT_WINDOWS[key]
    return 0


__all__ = ["DEFAULT_MODEL_CONTEXT_WINDOWS", "lookup_context_window"]
