"""scheduler v0.1 — ``[SILENT]`` 静默标记纯函数。

本模块对应 ``docs/agent-cron-module-v0.1/05-hermes-feature-tradeoffs.md`` §1
``[SILENT]`` 静默标记，参考 Hermes ``cron/scheduler.py:932`` 的判定。

规则：
- 命中条件：``final_message`` 经 ``lstrip + upper`` 后以 ``SILENT_MARKER`` 开头
- 不处理"内嵌中间"的 marker（保守判定）
- 命中后 Execution Bridge 将 ``ScheduledRun.status = SILENT`` 并跳过对外投递；
  ``final_message_excerpt`` 仍含 marker 本身写入审计

所有函数均为纯函数，不调 ``print`` / ``logging``，不做 IO。
"""

from __future__ import annotations

from scheduler.domain import SILENT_MARKER


def is_silent(final_message: str | None) -> bool:
    """``final_message`` 是否命中 ``[SILENT]`` 静默标记。

    规则（参考 Hermes ``scheduler.py:932``）：
    - ``None`` / 空串 → ``False``
    - ``lstrip + upper`` 后以 :data:`SILENT_MARKER` 开头 → ``True``
    - 内嵌中间不算（保守判定）
    """
    if not final_message:
        return False
    head = final_message.lstrip().upper()
    return head.startswith(SILENT_MARKER)


def strip_silent_prefix(final_message: str) -> str:
    """剥掉前缀 ``[SILENT]``（不改大小写之外的字符）。

    用于在审计日志里保留实际内容；通常 ``[SILENT]`` 单独成行不带后续，
    但 LLM 可能写 ``"[SILENT] no diff"``，本函数把前缀去掉，保留剩余文本。
    若不命中则原样返回。
    """
    head = final_message.lstrip()
    if head[: len(SILENT_MARKER)].upper() == SILENT_MARKER:
        idx = final_message.upper().find(SILENT_MARKER)
        return final_message[:idx] + final_message[idx + len(SILENT_MARKER) :].lstrip()
    return final_message


__all__ = [
    "is_silent",
    "strip_silent_prefix",
]
