"""共享内部 system 注入文本过滤工具（normalizer + jsonl_history 共用）。

Claude Code CLI / SDK 会把若干 "系统注入" 文本以 ``UserMessage(content=<str>)``
或 ``AssistantMessage[TextBlock]`` 形式塞进对话流。这些文本仅供父 agent 在
prompt 里看到，**不应出现在 Web 前端**——live 流过滤（normalizer）和持久化
历史回放（jsonl_history）必须用同一份规则，否则会出现"刷新页面后突然冒出
一条用户没说过的话"这类不对称 bug。

设计要点：

- 只放标准库，可被 normalizer 和 jsonl_history 同时 import，不引入循环依赖
- prefix 列表保持小而准——宁可漏过未知 tag 让产品反馈出来后定向加，
  不要靠 "<" 这类宽匹配伤到真实用户输入
- 参考 ccui ``server/modules/providers/list/claude/claude-sessions.provider.ts``
  第 34 行 ``INTERNAL_CONTENT_PREFIXES``
"""

from __future__ import annotations

__all__ = ["INTERNAL_CONTENT_PREFIXES", "is_internal_content"]


INTERNAL_CONTENT_PREFIXES: tuple[str, ...] = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "<task-notification>",
    "Caveat:",
    "This session is being continued from a previous",
    "[Request interrupted",
)
"""命中任一前缀的文本块整条丢弃。

`<task-notification>` 是 Claude Code CLI 在 Task 子 agent 完成时注入的
``UserMessage(content=<str>)``，目的是给父 agent 看到"子任务结果"——前端
不应渲染（live 流由 normalizer 直接丢 str-content UserMessage，历史路径
由 jsonl_history 调本模块过滤）。
"""


def is_internal_content(text: str) -> bool:
    """检查 text 是否命中内部前缀（应丢弃）。

    空串安全：返回 ``False``。
    """
    if not text:
        return False
    return any(text.startswith(p) for p in INTERNAL_CONTENT_PREFIXES)
