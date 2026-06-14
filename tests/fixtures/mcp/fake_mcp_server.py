#!/usr/bin/env python3
"""fake MCP stdio server 测试脚本。

脚本功能：用 line-delimited JSON-RPC 模拟最小 MCP server。
关键流程：从 stdin 逐行读取请求，按 initialize、tools/list、tools/call 返回响应。
关键函数：
- main：解析模式参数并启动 stdin 循环。
- _handle_request：按 JSON-RPC method 分派 fake 响应。
- _write_response：把 JSON-RPC response 写回 stdout 并刷新。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any


def main() -> int:
    """脚本入口：读取命令行模式，持续处理 stdin 中的 JSON-RPC 请求。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    args = parser.parse_args()
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        request = json.loads(raw_line)
        response = _handle_request(request, args.mode)
        if response is not None:
            _write_response(response)
    return 0


def _handle_request(request: dict[str, Any], mode: str) -> dict[str, Any] | None:
    """请求处理：根据 method 和 mode 返回对应 JSON-RPC 响应对象。"""

    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        if mode == "list-timeout":
            time.sleep(10)
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "web_search",
                        "title": "Fake Web Search",
                        "description": "Return a deterministic fake search result.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        if mode == "call-timeout":
            time.sleep(10)
            return None
        if mode == "call-error":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fake tool error"},
            }
        arguments = request.get("params", {}).get("arguments", {})
        query = arguments.get("query", "")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": f"Fake result for {query}: https://example.com/result",
                    }
                ],
                "data": {
                    "url": "https://example.com/result",
                    "title": "Fake Search Result",
                    "snippet": f"Snippet for {query}",
                    "provider": "fake_mcp",
                },
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def _write_response(response: dict[str, Any]) -> None:
    """响应输出：将 JSON-RPC 对象编码为单行 JSON 并写入 stdout。"""

    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
