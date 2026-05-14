"""claude_code 通道 token 派生器：从 SDK jsonl 现场算 cumulative + last_snapshot。

⚠️ **架构边界**：本模块是 ``web.usage_token`` 包私有，外部禁止 import
（``.importlinter`` Contract ``usage-token-encapsulation`` 强制）；
只能通过 ``UsageTokenManager`` 间接消费派生结果。

设计目标（参见任务包 ``usage-token-derive-from-jsonl-v0.1``）：

claude_code 通道的 token 真源是 SDK 落盘的 jsonl —— 每条 ``type=assistant`` 行
都带 ``message.usage`` 字段。我们之前重复在 ``metadata.json`` 维护 cumulative
+ last_snapshot，引入了 fire-and-forget 写盘竞争和文件损坏。改成"打开 thread
时现场扫 jsonl 派生"后，写盘者只剩 ``record_run_usage`` （单写者，在 lock 内），
彻底消除竞争。

不在本模块的职责：

- 拼 jsonl 路径（由 manager 注入的 callback 负责，避免 import ``web.claude_code``）
- 缓存（由 manager 加 TTL 缓存）
- WS 帧 / DTO 序列化（manager 层）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web.usage_token._channel_anthropic import (
    context_usage,
    merge_cumulative,
    parse_raw_to_usage,
    to_extras_dict,
)
from web.usage_token._models import (
    UsageTokenSnapshot,
    _AnthropicTokenUsage,
)

logger = logging.getLogger(__name__)

__all__ = ["ClaudeCodeDerived", "derive_from_jsonl"]


@dataclass(frozen=True)
class ClaudeCodeDerived:
    """jsonl 派生结果 —— cumulative + 最后一条 assistant 的快照 + 模型名。"""

    cumulative: _AnthropicTokenUsage
    """所有 assistant entry 的 usage 累加。"""

    last_snapshot: UsageTokenSnapshot
    """最后一条 assistant entry 的 usage 转 snapshot（含 context_usage 派生值）。

    turn=0 / run_id=""：jsonl 不带 runner state 概念，占位。
    """

    last_model_name: str
    """最后一条 assistant entry 的 ``message.model`` 字段值；缺失为空字符串。"""


def derive_from_jsonl(jsonl_path: Path) -> ClaudeCodeDerived | None:
    """扫 jsonl 累加 usage + 取最后一条 assistant 的 last_snapshot。

    Args:
        jsonl_path: ``~/.claude/projects/<encoded-cwd>/<claude_thread_id>.jsonl``。
            由调用方（manager 注入的 callback）拼出；本函数不感知路径规则。

    Returns:
        ``ClaudeCodeDerived``，或 ``None``（文件不存在 / 不可读 /
        没有任何带 usage 的 assistant entry）。

    实现要点：

    - **流式按行读**，避免大文件 OOM
    - 单行 JSON 损坏 → ``warning`` 记录后 skip，**不抛**
    - ``type != "assistant"`` 的行直接 skip
    - ``message.usage`` 缺失或非 dict → skip 这一行（仍计入"是否有 assistant"）
    - 行级容错 + 静默兜底，跟 :func:`web.claude_code.jsonl_history.parse_jsonl_history` 风格一致
    """
    if not jsonl_path.is_file():
        return None

    cumulative = _AnthropicTokenUsage()  # 全 0 起点
    last_usage_payload: Mapping[str, Any] | None = None
    last_model: str = ""

    try:
        fh = jsonl_path.open("r", encoding="utf-8")
    except OSError as exc:
        logger.warning("derive_from_jsonl: cannot open %s: %s", jsonl_path, exc)
        return None

    try:
        with fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "derive_from_jsonl: skip malformed line in %s: %s",
                        jsonl_path,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue

                delta = parse_raw_to_usage(usage)
                cumulative = merge_cumulative(cumulative, delta)
                last_usage_payload = usage  # 保留原始 payload 算 last_snapshot
                model = message.get("model")
                if isinstance(model, str) and model:
                    last_model = model
    except OSError as exc:
        logger.warning("derive_from_jsonl: read error on %s: %s", jsonl_path, exc)
        return None

    if last_usage_payload is None:
        # 文件存在但没有任何带 usage 的 assistant entry
        return None

    last_usage = parse_raw_to_usage(last_usage_payload)
    last_snapshot = UsageTokenSnapshot(
        channel="anthropic",
        input_tokens=last_usage.input_tokens,
        output_tokens=last_usage.output_tokens,
        extras=to_extras_dict(last_usage),
        context_usage=context_usage(last_usage),
        turn=0,
        run_id="",
    )

    return ClaudeCodeDerived(
        cumulative=cumulative,
        last_snapshot=last_snapshot,
        last_model_name=last_model,
    )
