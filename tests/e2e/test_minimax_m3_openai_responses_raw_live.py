"""MiniMax M3 OpenAI Responses raw usage live e2e。

本测试直接调用 MiniMax 官方 OpenAI Responses API：
``POST https://api.minimax.io/v1/responses``。

它绕过项目内 Anthropic-compatible provider，用于对照官方 Responses API 的
usage 字段拆分。默认跳过，显式设置 ``KONGMING_E2E_REAL_MODEL=1`` 后才会发起
真实请求。

输出目录默认是 ``log/minimax-openai-responses-<utc-ts>/``，目录内包含：
- ``raw-response-01.json`` / ``raw-response-02.json``：完整 request + response
- ``summary.json``：usage 相关字段摘录
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from infrastructure.config import load_config

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real MiniMax OpenAI Responses e2e",
)


_ENDPOINT = "https://api.minimax.io/v1/responses"


def _default_run_root() -> Path:
    """生成本次测试的日志根目录，输入为空，输出为仓库内 log 子目录路径。"""
    env_dir = os.getenv("KONGMING_E2E_MINIMAX_OPENAI_RESPONSES_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("log") / f"minimax-openai-responses-{ts}"


def _redacted_headers() -> dict[str, str]:
    """生成脱敏后的 headers 记录，输入为空，输出为日志安全字典。"""
    return {"Authorization": "<redacted>", "Content-Type": "application/json"}


def _usage_summary(response_body: dict[str, Any]) -> dict[str, Any]:
    """摘取 Responses API usage，输入为响应 JSON，输出为 usage 摘要。"""
    usage = response_body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return {
        "id": response_body.get("id"),
        "model": response_body.get("model"),
        "status": response_body.get("status"),
        "usage": usage,
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": input_details.get("cached_tokens")
        if isinstance(input_details, dict)
        else None,
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": output_details.get("reasoning_tokens")
        if isinstance(output_details, dict)
        else None,
        "total_tokens": usage.get("total_tokens"),
        "output_text_chars": len(response_body.get("output_text") or ""),
    }


async def _post_response(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    payload: dict[str, Any],
) -> httpx.Response:
    """发送一次 Responses API 请求，输入为 client/key/payload，输出为 HTTP 响应。"""
    return await client.post(
        _ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )


@pytest.mark.asyncio
async def test_minimax_m3_openai_responses_raw_usage_two_calls() -> None:
    """连续跑两次官方 Responses API，输入为短 prompt，输出为 raw/summary 文件。"""
    load_config(Path("config/setting.yaml"))
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        pytest.skip("MINIMAX_API_KEY is required for MiniMax OpenAI Responses API")

    run_root = _default_run_root().resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    instructions_path = Path("src/prompts/templates/SITIAN_ANALYZER.md")
    instructions = instructions_path.read_text(encoding="utf-8")
    common_payload: dict[str, Any] = {
        "model": "MiniMax-M3",
        "instructions": instructions,
        "max_output_tokens": 512,
        "temperature": 0.0,
        "reasoning": {"effort": "high"},
        "prompt_cache_key": "kongming-minimax-openai-responses-raw-live",
        "store": False,
    }
    payloads = [
        {**common_payload, "input": "hi"},
        {**common_payload, "input": "你好"},
    ]

    raw_records: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for index, payload in enumerate(payloads, start=1):
            response = await _post_response(client=client, api_key=api_key, payload=payload)
            try:
                body: Any = response.json()
            except ValueError:
                body = {"__raw_text__": response.text}
            record = {
                "provider": "minimax_openai_responses",
                "url": _ENDPOINT,
                "request": {
                    "payload": payload,
                    "headers": _redacted_headers(),
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": body,
                },
                "error": f"HTTP {response.status_code}" if response.status_code >= 400 else None,
            }
            raw_path = run_root / f"raw-response-{index:02d}.json"
            raw_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            raw_records.append({"path": str(raw_path), "record": record})
            assert response.status_code < 400, (
                f"MiniMax Responses API failed HTTP {response.status_code}: {response.text[:500]}"
            )

    summary = {
        "run_root": str(run_root),
        "endpoint": _ENDPOINT,
        "model": "MiniMax-M3",
        "system_prompt_path": str(instructions_path.resolve()),
        "system_prompt_chars": len(instructions),
        "raw_files": [item["path"] for item in raw_records],
        "responses": [
            _usage_summary(item["record"]["response"]["body"])
            for item in raw_records
            if isinstance(item["record"]["response"]["body"], dict)
        ],
        "requests": [
            {
                "model": payload["model"],
                "input": payload["input"],
                "has_instructions": "instructions" in payload,
                "instructions_chars": len(payload["instructions"]),
                "reasoning": payload.get("reasoning"),
                "prompt_cache_key": payload.get("prompt_cache_key"),
            }
            for payload in payloads
        ],
    }
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"minimax_openai_responses_log_dir={run_root}")
    print(f"minimax_openai_responses_summary={summary_path}")
    print(f"minimax_openai_responses_raw_files={len(raw_records)}")
