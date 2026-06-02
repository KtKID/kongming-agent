"""智能审批 WS 共用 handler（toggle / query / state-msg）。

claude_code 与 generic_chat 两条通道都需要 ``auto-approval-toggle`` /
``auto-approval-query`` 入站命令以及连接建立时主动 push 一次 state 帧。
把三段逻辑抽到本模块，让调用方（``web.claude_code.route`` /
``web.ws._dispatch_frame`` 等）只负责把 ``policy`` 从 ``app.state`` 取出后透传。

设计取舍
----------

- ``policy`` **显式参数注入**：不在 handler 里从 ``websocket.app.state`` 抠
  ``auto_approval_policy``，让 handler 可独立单测（mock policy 即可，不需
  要构造 fastapi app）。route 侧调用前自行 ``getattr(ws.app.state,
  "auto_approval_policy", None)`` 并做 None 判断。
- ``error`` / ``state`` 字面值与原 ``web.claude_code.route`` 完全对齐：
  ``"auto_approval_policy not configured"``、
  ``"auto-approval-toggle.cwd required"`` / ``"auto-approval-query.cwd required"``、
  ``kind="auto_approval_state"``。
- **通道参数化（v0.5 task #5 引入）**：``channel: str = "claude_code"``
  作为三个公共函数的可选关键字参数，默认值兼容原 claude_code 调用点，
  无需改 ``claude_code/route.py`` 装配。``generic_chat`` 通道（``/ws/threads``）
  在 dispatch 时显式传 ``channel="generic_chat"``，state 帧的 ``channel``
  字段与 error 帧的 ``provider`` / ``channel`` 字段会随之切换，前端按通道
  区分 UI 状态。
- ``_send_error`` 同步加 ``channel`` 参数：``provider`` 字段由 ``channel``
  按映射决定（``claude_code → "claude"``，``generic_chat → "generic"``），
  额外追加一个独立的 ``channel`` 字段方便前端/测试直接断言通道。
  claude_code 老前端只读 ``provider="claude"``，行为完全不变。
- error 发送走本模块内的 ``_send_error`` 辅助；与
  ``route.py::_send_error`` 行为完全一致（``send_json`` 抛错时 suppress）。
"""

from __future__ import annotations

from typing import Any

from fastapi import WebSocket

from network.network_log import log_network_exception

# channel → error 帧 provider 字段的映射。保持 claude_code 走 "claude"
# 字面值（与老前端 100% 兼容），generic_chat 走 "generic"，与后端日志惯例
# 一致；未知 channel 兜底直接用 channel 字符串本身，避免悄悄把错通道
# 当 claude 处理。
_PROVIDER_BY_CHANNEL: dict[str, str] = {
    "claude_code": "claude",
    "generic_chat": "generic",
}


async def handle_auto_approval_toggle(
    websocket: WebSocket,
    data: dict[str, Any],
    policy: Any,
    *,
    channel: str = "claude_code",
) -> None:
    """处理 ``auto-approval-toggle`` 入站命令。

    流程：
    1. ``policy`` 为 ``None`` → 回 ``error("auto_approval_policy not configured")``
    2. ``data["cwd"]`` 缺失 / 非字符串 / 空串 → 回 ``error("auto-approval-toggle.cwd required")``
    3. 调 ``policy.set_enabled(cwd, enabled)`` 写盘
    4. 通过 ``websocket.send_json`` 回执 ``auto_approval_state`` 帧

    Args:
        websocket: 当前 WS 连接，用于回执 state / error。
        data: 入站消息原始 dict（含 ``cwd`` / ``enabled``）。
        policy: ``AutoApprovalPolicy`` 实例；可为 ``None`` 表示未装配。
        channel: 通道标识，默认 ``"claude_code"`` 兼容原 route。
            ``generic_chat`` 走 ``/ws/threads`` 时显式传 ``"generic_chat"``。
    """
    if policy is None:
        await _send_error(
            websocket,
            "auto_approval_policy not configured",
            channel=channel,
        )
        return
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        await _send_error(
            websocket,
            "auto-approval-toggle.cwd required",
            channel=channel,
        )
        return
    enabled = bool(data.get("enabled", False))
    policy.set_enabled(cwd, enabled)
    try:
        await websocket.send_json(
            build_auto_approval_state_msg(policy, cwd, channel=channel),
        )
    except Exception as exc:
        log_network_exception(
            "web.auto_approval.ws_handlers",
            "toggle_state_send_failed",
            exc,
            channel=channel,
            cwd=cwd,
        )


async def handle_auto_approval_query(
    websocket: WebSocket,
    data: dict[str, Any],
    policy: Any,
    *,
    channel: str = "claude_code",
) -> None:
    """处理 ``auto-approval-query`` 入站命令。

    前端在切 thread / 重连后通过该命令主动拉一次 state，确保 UI toggle 与
    后端配置同步。

    Args:
        websocket: 当前 WS 连接，用于回执 state / error。
        data: 入站消息原始 dict（含 ``cwd``）。
        policy: ``AutoApprovalPolicy`` 实例；可为 ``None`` 表示未装配。
        channel: 通道标识，默认 ``"claude_code"`` 兼容原 route。
            ``generic_chat`` 走 ``/ws/threads`` 时显式传 ``"generic_chat"``。
    """
    if policy is None:
        await _send_error(
            websocket,
            "auto_approval_policy not configured",
            channel=channel,
        )
        return
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        await _send_error(
            websocket,
            "auto-approval-query.cwd required",
            channel=channel,
        )
        return
    try:
        await websocket.send_json(
            build_auto_approval_state_msg(policy, cwd, channel=channel),
        )
    except Exception as exc:
        log_network_exception(
            "web.auto_approval.ws_handlers",
            "query_state_send_failed",
            exc,
            channel=channel,
            cwd=cwd,
        )


def build_auto_approval_state_msg(
    policy: Any,
    cwd: str,
    *,
    channel: str = "claude_code",
) -> dict[str, Any]:
    """构造 ``auto_approval_state`` S2C 帧。

    单一真源：连接建立 push / toggle 回执 / query 应答都走该 helper，避免
    字段漂移。``timeoutMs`` 优先用 per-cwd 覆盖，未配则回退到
    ``rule_set.default_timeout_ms``。

    Args:
        policy: ``AutoApprovalPolicy`` 实例（必须非 None）。
        cwd: 当前会话的 cwd（兜底 ``cfg.cwd or cwd``）。
        channel: 通道标识，默认 ``"claude_code"`` 兼容原 route。
            generic_chat 通道传 ``"generic_chat"``，前端按 ``channel``
            字段区分 UI 状态。

    Returns:
        ``kind="auto_approval_state"`` 帧 dict，与 claude_code v1 schema 对齐
        （仅 ``channel`` 字段会跟随调用方切换）。
    """
    cfg = policy.get_config(cwd)
    effective_timeout = cfg.timeout_ms or policy.rule_set.default_timeout_ms
    return {
        "kind": "auto_approval_state",
        "channel": channel,
        "cwd": cfg.cwd or cwd,
        "enabled": cfg.enabled,
        "timeoutMs": effective_timeout,
        "ruleOverrides": dict(cfg.rule_overrides),
    }


async def _send_error(
    websocket: WebSocket,
    error_message: str,
    *,
    channel: str = "claude_code",
) -> None:
    """发送 ``kind="error"`` 帧。

    向后兼容策略：
    - ``provider`` 字段保留原 ``claude_code/route._send_error`` 的字面值
      （claude_code → ``"claude"``，generic_chat → ``"generic"``）；
      未知 channel 兜底用 channel 字符串本身。
    - 同步追加独立的 ``channel`` 字段，让前端 / 测试可直接断言通道，
      不必依赖 ``provider`` 反推。

    ``send_json`` 抛错时 suppress——WS 已断开属正常路径。

    Args:
        websocket: 当前 WS 连接。
        error_message: 错误文本（与原版字面值对齐）。
        channel: 通道标识，决定 ``provider`` / ``channel`` 字段。
    """
    provider = _PROVIDER_BY_CHANNEL.get(channel, channel)
    try:
        await websocket.send_json(
            {
                "kind": "error",
                "provider": provider,
                "channel": channel,
                "error": error_message,
            },
        )
    except Exception as exc:
        log_network_exception(
            "web.auto_approval.ws_handlers",
            "error_send_failed",
            exc,
            channel=channel,
            error_message=error_message,
        )


__all__ = [
    "build_auto_approval_state_msg",
    "handle_auto_approval_query",
    "handle_auto_approval_toggle",
]
