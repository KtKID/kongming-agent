"""智能审批审计日志（独立 JSONL 追加）。

存储位置::

    <kongming_home>/web/auto_approval/audit.jsonl

设计要点：

- 单文件追加（不按 cwd / channel 分文件——跨 cwd 审计才方便）
- ``O_APPEND`` 单行写入：POSIX 下 PIPE_BUF 内的 write() 是原子的，
  并发追加无 interleave 风险
- 每行严格 JSON（最后跟 ``\n``），失败行写入时不影响后续行
- v1 不做 rotation；长期使用 README 注明手动归档
- 不感知 policy / rules——只接受 dict 写文件
- v1 不做 schema 强制，但用 ``log_request`` / ``log_decision`` 两个 helper
  保证字段完整（避免散落的 dict.set 漏字段）

字段 schema（参见 README）::

    {
      "ts_ms": int,
      "channel": str,                  # "claude_code" / "generic_chat"
      "thread_id": str | null,
      "cwd": str,
      "tool_name": str,
      "arguments": dict | str,
      "rule_evaluation": {
        "matched": str | null,         # 命中的 rule.id；null=未命中
        "all_rules": list[str]         # 当前 cwd 启用的 rule.id 列表
      },
      "decision": {
        "outcome": "denied" | "allowed_manual" | "allowed_auto_timeout" | "blocked_then_manual",
        "decided_at_ms": int | null,
        "decided_by": "user" | "timeout" | null,
        "timeout_ms": int
      }
    }
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

# 合法 outcome（用于 helper 校验，运行时只警告不抛）
Outcome = Literal[
    "denied",
    "allowed_manual",
    "allowed_auto_timeout",
    "blocked_then_manual",
    "pending",  # request 阶段尚未决策，仅用于 log_request 内部
]

_VALID_OUTCOMES: frozenset[str] = frozenset(
    [
        "denied",
        "allowed_manual",
        "allowed_auto_timeout",
        "blocked_then_manual",
        "pending",
    ],
)


class AuditLogger:
    """JSONL 追加器（单实例可被多 connection 复用，线程/进程安全）。"""

    def __init__(self, jsonl_path: Path) -> None:
        self._path = Path(jsonl_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 确保文件存在（避免首次 stat 失败）
        if not self._path.exists():
            self._path.touch()

    # ----- 公共接口 -----

    @property
    def path(self) -> Path:
        return self._path

    def log_request(
        self,
        *,
        channel: str,
        thread_id: str | None,
        cwd: str,
        tool_name: str,
        arguments: Any,
        matched_rule: str | None,
        all_enabled_rules: list[str],
        timeout_ms: int,
        ts_ms: int | None = None,
    ) -> None:
        """审批请求到达时记一条（outcome=pending）。

        这一行用于审计「这次请求经过了规则评估、当时启用的规则集是什么」，
        即使后续 decision 因连接断开等原因未落盘，也能查到 request 本身。
        """
        record = {
            "ts_ms": ts_ms if ts_ms is not None else int(time.time() * 1000),
            "channel": channel,
            "thread_id": thread_id,
            "cwd": cwd,
            "tool_name": tool_name,
            "arguments": arguments,
            "rule_evaluation": {
                "matched": matched_rule,
                "all_rules": list(all_enabled_rules),
            },
            "decision": {
                "outcome": "pending",
                "decided_at_ms": None,
                "decided_by": None,
                "timeout_ms": int(timeout_ms),
            },
        }
        self._append_line(record)

    def log_decision(
        self,
        *,
        channel: str,
        thread_id: str | None,
        cwd: str,
        tool_name: str,
        arguments: Any,
        matched_rule: str | None,
        all_enabled_rules: list[str],
        outcome: str,
        decided_by: str | None,
        timeout_ms: int,
        ts_ms: int | None = None,
        decided_at_ms: int | None = None,
    ) -> None:
        """审批决策落定时记一条。"""
        # 不抛——审计不能因字段错误阻断主流程；非白名单值带 unknown: 前缀
        outcome_safe = outcome if outcome in _VALID_OUTCOMES else f"unknown:{outcome}"

        now_ms = int(time.time() * 1000)
        record = {
            "ts_ms": ts_ms if ts_ms is not None else now_ms,
            "channel": channel,
            "thread_id": thread_id,
            "cwd": cwd,
            "tool_name": tool_name,
            "arguments": arguments,
            "rule_evaluation": {
                "matched": matched_rule,
                "all_rules": list(all_enabled_rules),
            },
            "decision": {
                "outcome": outcome_safe,
                "decided_at_ms": decided_at_ms if decided_at_ms is not None else now_ms,
                "decided_by": decided_by,
                "timeout_ms": int(timeout_ms),
            },
        }
        self._append_line(record)

    def read_all(self) -> list[dict[str, Any]]:
        """读取所有审计行（测试用，生产慎用——文件可能极大）。"""
        result: list[dict[str, Any]] = []
        if not self._path.exists():
            return result
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    # 损坏行：跳过（生产环境可能因磁盘满写一半）
                    continue
        return result

    # ----- 内部 -----

    def _append_line(self, record: dict[str, Any]) -> None:
        """用 ``O_APPEND`` 单次 write 追加单行 JSON。

        POSIX 保证：PIPE_BUF（一般 4096 bytes）以内的 write 到 O_APPEND
        打开的文件是原子的，并发追加不会 interleave。

        record 编码不应超 4KB；超出时仍会写入但理论上可能与并发行交错（极小概率）。
        """
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")

        fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o644,
        )
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)


__all__ = ["AuditLogger", "Outcome"]
