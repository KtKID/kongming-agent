"""MiniMax M3 OpenAI-compatible Chat Completions raw usage live e2e。

本测试直接调用 MiniMax OpenAI-compatible Chat Completions：
``POST https://api.minimaxi.com/v1/chat/completions``。

用途是对照 Anthropic-compatible endpoint 与 OpenAI-compatible chat endpoint
的 usage 字段拆分差异。默认跳过，显式设置 ``KONGMING_E2E_REAL_MODEL=1`` 后
才会发起真实请求。

输出目录默认是 ``log/minimax-openai-chat-<utc-ts>/``，目录内包含：
- ``raw-chat-01.json`` / ``raw-chat-02.json``：完整 request + response
- ``summary.json``：usage 相关字段摘录
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from infrastructure.config import load_config

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real MiniMax OpenAI chat e2e",
)


_ENDPOINT = "https://api.minimaxi.com/v1/chat/completions"


def _default_run_root() -> Path:
    """生成本次测试的日志根目录，输入为空，输出为仓库内 log 子目录路径。"""
    env_dir = os.getenv("KONGMING_E2E_MINIMAX_OPENAI_CHAT_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("log") / f"minimax-openai-chat-{ts}"


def _usage_summary(response_body: dict[str, Any]) -> dict[str, Any]:
    """摘取 Chat Completions usage，输入为响应 JSON，输出为 usage 摘要。"""
    usage = response_body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    choices = response_body.get("choices")
    message: dict[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        raw_message = choices[0].get("message")
        if isinstance(raw_message, dict):
            message = raw_message
    return {
        "id": response_body.get("id"),
        "model": response_body.get("model"),
        "usage": usage,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_input_tokens": prompt_details.get("cached_tokens")
        if isinstance(prompt_details, dict)
        else None,
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_output_tokens": completion_details.get("reasoning_tokens")
        if isinstance(completion_details, dict)
        else None,
        "total_tokens": usage.get("total_tokens"),
        "content_chars": len(message.get("content") or ""),
        "reasoning_content_chars": len(message.get("reasoning_content") or ""),
        "reasoning_details_count": len(message.get("reasoning_details") or []),
    }


async def _post_chat(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    payload: dict[str, Any],
) -> httpx.Response:
    """发送一次 Chat Completions 请求，输入为 client/key/payload，输出为 HTTP 响应。"""
    return await client.post(
        _ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )


@pytest.mark.asyncio
async def test_minimax_m3_openai_chat_raw_usage_two_calls() -> None:
    """连续跑两次 OpenAI-compatible chat，输入为短 prompt，输出为 raw/summary 文件。"""
    load_config(Path("config/setting.yaml"))
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        pytest.skip("MINIMAX_API_KEY is required for MiniMax OpenAI Chat API")

    run_root = _default_run_root().resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    system_prompt_path = Path("src/prompts/templates/SITIAN_ANALYZER.md")
    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "hi"},
    ]
    common_payload: dict[str, Any] = {
        "model": "MiniMax-M3",
        "max_completion_tokens": 512,
        "temperature": 0.0,
        "thinking": {"type": "adaptive"},
        "reasoning_split": True,
        "stream": False,
    }

    raw_records: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for index in (1, 2):
            if index == 2:
                messages = [*messages, {"role": "user", "content": "你好"}]
            payload = {**common_payload, "messages": messages}
            response = await _post_chat(client=client, api_key=api_key, payload=payload)
            try:
                body: Any = response.json()
            except ValueError:
                body = {"__raw_text__": response.text}
            record = {
                "provider": "minimax_openai_chat",
                "url": _ENDPOINT,
                "request": {
                    "payload": deepcopy(payload),
                    "headers": {
                        "Authorization": "<redacted>",
                        "Content-Type": "application/json",
                    },
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": body,
                },
                "error": f"HTTP {response.status_code}" if response.status_code >= 400 else None,
            }
            raw_path = run_root / f"raw-chat-{index:02d}.json"
            raw_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            raw_records.append({"path": str(raw_path), "record": record})
            assert response.status_code < 400, (
                f"MiniMax OpenAI chat failed HTTP {response.status_code}: {response.text[:500]}"
            )
            if isinstance(body, dict):
                choices = body.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    message = choices[0].get("message")
                    if isinstance(message, dict):
                        messages.append(message)

    summary = {
        "run_root": str(run_root),
        "endpoint": _ENDPOINT,
        "model": "MiniMax-M3",
        "system_prompt_path": str(system_prompt_path.resolve()),
        "system_prompt_chars": len(system_prompt),
        "raw_files": [item["path"] for item in raw_records],
        "responses": [
            _usage_summary(item["record"]["response"]["body"])
            for item in raw_records
            if isinstance(item["record"]["response"]["body"], dict)
        ],
        "requests": [
            {
                "message_count": len(item["record"]["request"]["payload"]["messages"]),
                "message_roles": [
                    message.get("role")
                    for message in item["record"]["request"]["payload"]["messages"]
                    if isinstance(message, dict)
                ],
                "model": item["record"]["request"]["payload"].get("model"),
                "thinking": item["record"]["request"]["payload"].get("thinking"),
                "reasoning_split": item["record"]["request"]["payload"].get("reasoning_split"),
            }
            for item in raw_records
        ],
    }
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"minimax_openai_chat_log_dir={run_root}")
    print(f"minimax_openai_chat_summary={summary_path}")
    print(f"minimax_openai_chat_raw_files={len(raw_records)}")
