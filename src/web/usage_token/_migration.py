"""v7 → v8 lazy upgrade：把旧 5 个 ``cumulative_*_tokens`` 字段映射到嵌套
``cumulative_usage`` dict。

集成方式（task#2 落实）：在 ``web.thread_metadata.read_thread_metadata`` 现存
懒升级链末尾追加：

.. code-block:: python

    if data.get("schema_version") == 7:
        data = upgrade_v7_to_v8(data)

⚠️ **架构边界**：本模块是 ``web.usage_token`` 包私有，外部禁止 import。
但 ``upgrade_v7_to_v8`` 是纯函数（无副作用），task#2 在 ``thread_metadata.py``
里需要调用——调用方式是 ``from web.usage_token._migration import upgrade_v7_to_v8``，
该 import **由 .importlinter ignore_imports 单条白名单放行**（不放整个 package
对外）。

设计依据：[`docs/usage-token/03-core-workflows.md`](../../../docs/usage-token/03-core-workflows.md#流程-3v7v8-lazy-upgrade)
"""

from __future__ import annotations

from typing import Any


def upgrade_v7_to_v8(data: dict[str, Any]) -> dict[str, Any]:
    """v7 → v8 字段重组（纯函数，不修改入参）。

    输入：v7 形态 dict（``cumulative_prompt_tokens`` 等 5 字段 + ``backend_kind``
    + 其他 thread 字段）
    输出：v8 形态 dict（``cumulative_usage`` 嵌套 dict + 旧 5 字段彻底删除 +
    ``schema_version=8``）

    映射规则（按 ``backend_kind``）：

    - ``claude_code`` → ``channel="anthropic"``
        - cumulative_prompt_tokens          → input_tokens
        - cumulative_cache_read_tokens      → cache_read_input_tokens
        - cumulative_cache_creation_tokens  → cache_creation_input_tokens
        - cumulative_completion_tokens      → output_tokens

    - ``codex`` → ``channel="openai"``
        - cumulative_prompt_tokens          → input_tokens
        - cumulative_cache_read_tokens      → cached_input_tokens（lossy 别名修正）
        - cumulative_cache_creation_tokens  → 丢弃（OpenAI 系无此概念）
        - cumulative_completion_tokens      → output_tokens
        - cumulative_reasoning_output_tokens → 0（决策 4N：历史不补）

    - ``generic_chat`` → 启发式判断底层 provider：
        - 旧 ``cumulative_cache_creation_tokens > 0`` → 当作 ``anthropic`` 系
          （cache_creation 是 Anthropic 独有概念，OpenAI 不报）
        - 否则 → ``openai`` 系

    旧 ``cumulative_total_tokens`` 是派生量，**直接丢弃**（manager 内部按需算）。

    Args:
        data: v7 形态 dict（含 schema_version=7 + 旧 5 字段）

    Returns:
        新 dict（不修改入参；含 cumulative_usage 嵌套 + schema_version=8 +
        旧字段彻底 pop 掉）

    注意：本函数对**任何输入**都幂等容错——
    - v8 输入直接原样返回（schema_version!=7）
    - 缺旧字段视为 0
    - backend_kind 缺失视为 ``generic_chat``
    """
    # 拷贝输入，不修改原 dict（lazy upgrade 链可能链式调用）
    data = dict(data)

    if data.get("schema_version") != 7:
        # 非 v7 → 不处理（让调用方继续走自己的升级链）
        return data

    backend_kind = data.get("backend_kind", "generic_chat")

    # 提取并删除旧字段（pop 默认 0；用 `or 0` 防御 None / null）
    old_prompt = int(data.pop("cumulative_prompt_tokens", 0) or 0)
    old_completion = int(data.pop("cumulative_completion_tokens", 0) or 0)
    old_cache_read = int(data.pop("cumulative_cache_read_tokens", 0) or 0)
    old_cache_creation = int(data.pop("cumulative_cache_creation_tokens", 0) or 0)
    # 派生量 cumulative_total_tokens 直接丢弃
    data.pop("cumulative_total_tokens", None)

    # 决定 channel
    if backend_kind == "claude_code":
        channel = "anthropic"
    elif backend_kind == "codex":
        channel = "openai"
    else:  # generic_chat 或未知值 → 启发式
        # Anthropic 独有概念 cache_creation；若历史出现非 0 值，按 anthropic 处理
        channel = "anthropic" if old_cache_creation > 0 else "openai"

    # 构造 cumulative_usage dict
    if channel == "anthropic":
        cumulative_usage: dict[str, Any] = {
            "channel": "anthropic",
            "input_tokens": old_prompt,
            "cache_read_input_tokens": old_cache_read,
            "cache_creation_input_tokens": old_cache_creation,
            "output_tokens": old_completion,
        }
    else:  # openai
        cumulative_usage = {
            "channel": "openai",
            "input_tokens": old_prompt,
            # Codex 老 cache_read 实际是 cached_input 的 lossy 别名（task#1 修正）
            "cached_input_tokens": old_cache_read,
            "output_tokens": old_completion,
            "reasoning_output_tokens": 0,  # 决策 4N：历史不补
        }

    data["cumulative_usage"] = cumulative_usage
    data["schema_version"] = 8
    return data


__all__ = ["upgrade_v7_to_v8"]
