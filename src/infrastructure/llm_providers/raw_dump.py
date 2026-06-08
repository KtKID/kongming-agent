"""Provider 原始 HTTP 交互的 opt-in 落盘工具。

**为什么存在**：项目当前的 ``llm.response`` 事件 payload 为了控制 trace.jsonl
体积只存了 ``finish_reason`` / ``has_tool_calls`` / ``usage`` 三个字段；想看
provider 完整返回（含厂商扩展字段如 ``logprobs`` / ``reasoning_content`` /
真实 ``tool_calls[].id`` / ``system_fingerprint`` 等）目前没地方看。

**工作流**：

1. 调用方在解析完 ``response.json()`` 之后调用 :func:`dump_raw_llm_interaction`
2. 函数先检查 env ``KONGMING_TRACE_RAW_LLM=1``；没开就立刻返回 ``None``
3. 开了就把 request payload + response body + status + headers 打包写到
   ``.kongming/debug/raw-llm-<UTC timestamp>-<nonce>.json``
4. 任何异常都静默（return ``None``），永远不污染主链路

**安全约束**：
- request headers 的 ``Authorization`` / ``X-API-Key`` / ``Api-Key`` 字段
  落盘前被替换为 ``<redacted>``，防止 dump 文件变成 API key 泄露源
- dump 目录 ``.kongming/`` 已在 ``.gitignore`` 里，不会进仓库
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

_ENV_FLAG = "KONGMING_TRACE_RAW_LLM"
_DEFAULT_DIR = Path(".kongming") / "debug"
_REDACTED_KEYS = frozenset({"authorization", "x-api-key", "api-key"})


def is_enabled() -> bool:
    """fallback 便利：只检查 env 变量。

    通常由 provider 在调用 :func:`dump_raw_llm_interaction` 时显式传入
    ``enabled=cfg.trace.raw_llm`` —— 此时不走本函数。
    env 来源保留做"命令行临时一次性开启"场景：
    ``KONGMING_TRACE_RAW_LLM=1 uv run python -m hosts.cli.main``。
    """
    return os.getenv(_ENV_FLAG) == "1"


def dump_raw_llm_interaction(
    *,
    provider: str,
    url: str,
    request_payload: dict[str, Any],
    request_headers: dict[str, str],
    response_status: int | None,
    response_headers: dict[str, str] | None,
    response_body: Any,
    error: str | None = None,
    dump_dir: Path | None = None,
    enabled: bool | None = None,
) -> Path | None:
    """把一次 LLM 交互的完整 request + response 落盘。

    Args:
        provider: provider 标识，例如 ``"openai_responses"`` / ``"anthropic_messages"``，
            用来区分同一批 dump 里不同 provider 的记录。
        url: 实际请求 URL，便于定位是哪个 endpoint。
        request_payload: 发给 provider 的 JSON body。
        request_headers: 发出去的 HTTP headers。函数内部脱敏 Authorization
            之类的敏感字段后再写入磁盘。
        response_status: HTTP 状态码，``None`` 代表没收到响应（例如连接层错误）。
        response_headers: HTTP 响应头；可为 ``None``。
        response_body: 解析后的响应体（通常是 ``dict``），如果解析失败调用方
            可以传 ``{"__raw_text__": response.text}`` 类占位。
        error: 错误摘要字符串；成功路径传 ``None``，4xx/5xx 路径传诸如
            ``"HTTP 401"`` 的简短标识。
        dump_dir: dump 根目录，默认 ``.kongming/debug``；测试可注入
            ``tmp_path`` 做隔离。
        enabled: 是否启用。调用方（provider）应传入 ``cfg.trace.raw_llm``；
            ``None`` 时 fallback 到 :func:`is_enabled` 读 env 变量。

    Returns:
        写出的文件路径；未开启 dump / 失败时返回 ``None``。
    """
    resolved_enabled = enabled if enabled is not None else is_enabled()
    if not resolved_enabled:
        return None

    target_dir = dump_dir or _DEFAULT_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        nonce = uuid.uuid4().hex[:6]
        path = target_dir / f"raw-llm-{ts}-{nonce}.json"

        record: dict[str, Any] = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider,
            "url": url,
            "request": {
                "payload": request_payload,
                "headers": _redact_headers(request_headers),
            },
            "response": {
                "status_code": response_status,
                "headers": dict(response_headers) if response_headers is not None else None,
                "body": response_body,
            },
            "error": error,
        }

        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)
        return path
    except Exception:
        # dump 永远不能拖垮主链路；调试工具失败静默。
        return None


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """脱敏敏感 headers。不区分大小写匹配。"""
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _REDACTED_KEYS:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


__all__ = ["dump_raw_llm_interaction", "is_enabled"]
