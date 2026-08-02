"""MiniMax M3 raw usage live e2e。

本测试用于抓取真实 MiniMax M3 Anthropic-compatible 流式返回，验证 raw SSE、
trace usage、file session usage 三层数据是否一致。默认跳过，显式设置
``KONGMING_E2E_REAL_MODEL=1`` 后才会发起真实模型请求。

输出目录默认是 ``log/m3-raw-usage-<utc-ts>/``，目录内包含：
- ``debug/raw-llm-*.json``：provider 原始 request + response SSE chunks
- ``trace.jsonl``：Runner 事件 trace
- ``sessions/<session_id>/<session_id>.jsonl``：FileSession 写入记录
- ``summary.json``：usage 相关字段摘录
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.agent_spec import AgentSpec
from core.contracts import ProviderUsageSnapshot
from infrastructure.config import load_config
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.tracing import JsonlTraceSink
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tools import AutoAllowApproval

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real MiniMax M3 raw usage e2e",
)


_SESSION_ID = "m3-raw-usage-live"


def _default_run_root() -> Path:
    """生成本次测试的日志根目录，输入为空，输出为仓库内 log 子目录路径。"""
    env_dir = os.getenv("KONGMING_E2E_M3_RAW_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("log") / f"m3-raw-usage-{ts}"


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，输入为路径，输出为字典。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _raw_usage_summary(raw_path: Path) -> dict[str, Any]:
    """摘取 raw SSE 中的 usage 字段，输入为 raw 文件，输出为 usage 摘要。"""
    record = _load_json(raw_path)
    request_payload = record.get("request", {}).get("payload", {})
    response = record.get("response", {})
    body = response.get("body", {})
    chunks = body.get("chunks", []) if isinstance(body, dict) else []

    message_start_usage: dict[str, Any] | None = None
    message_delta_usages: list[dict[str, Any]] = []
    message_start: dict[str, Any] | None = None

    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            data = chunk.get("data")
            if not isinstance(data, dict):
                continue
            event = chunk.get("event") or data.get("type")
            if event == "message_start":
                raw_message = data.get("message")
                if isinstance(raw_message, dict):
                    message_start = raw_message
                    raw_usage = raw_message.get("usage")
                    if isinstance(raw_usage, dict):
                        message_start_usage = dict(raw_usage)
            elif event == "message_delta":
                raw_usage = data.get("usage")
                if isinstance(raw_usage, dict):
                    message_delta_usages.append(dict(raw_usage))

    messages = request_payload.get("messages")
    return {
        "file": str(raw_path),
        "provider": record.get("provider"),
        "status_code": response.get("status_code"),
        "error": record.get("error"),
        "request": {
            "model": request_payload.get("model"),
            "stream": request_payload.get("stream"),
            "message_count": len(messages) if isinstance(messages, list) else None,
            "system_chars": len(request_payload.get("system", "") or ""),
            "has_tools": bool(request_payload.get("tools")),
            "has_thinking": "thinking" in request_payload,
        },
        "raw_message": {
            "id": message_start.get("id") if isinstance(message_start, dict) else None,
            "model": message_start.get("model") if isinstance(message_start, dict) else None,
            "stop_reason": message_start.get("stop_reason")
            if isinstance(message_start, dict)
            else None,
        },
        "message_start_usage": message_start_usage,
        "message_delta_usages": message_delta_usages,
        "chunk_count": len(chunks) if isinstance(chunks, list) else None,
    }


def _trace_response_summaries(trace_path: Path) -> list[dict[str, Any]]:
    """摘取 trace 中的 llm.response usage，输入为 trace 路径，输出为响应摘要列表。"""
    if not trace_path.exists():
        return []
    responses: list[dict[str, Any]] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") != "llm.response":
            continue
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        responses.append(
            {
                "run_id": record.get("run_id"),
                "turn": record.get("turn"),
                "usage": payload.get("usage"),
                "provider_metadata": payload.get("provider_metadata"),
                "response": payload.get("response"),
            }
        )
    return responses


def _session_assistant_usage(session_path: Path) -> list[dict[str, Any]]:
    """摘取 file session 中 assistant usage，输入为 session jsonl，输出为 usage 列表。"""
    if not session_path.exists():
        return []
    usages: list[dict[str, Any]] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        message = record.get("message", {})
        if isinstance(message, dict) and message.get("role") == "assistant":
            usages.append(
                {
                    "message_id": record.get("message_id"),
                    "usage": record.get("usage"),
                    "content_chars": len(message.get("content") or ""),
                    "tool_call_count": len(message.get("tool_calls") or []),
                }
            )
    return usages


def _effective_anthropic_usage(raw_summary: dict[str, Any]) -> dict[str, Any]:
    """合并 Anthropic 累计 usage，输入为 raw 摘要，输出为字段级 latest-present。"""
    effective: dict[str, Any] = {}
    message_start_usage = raw_summary.get("message_start_usage")
    if isinstance(message_start_usage, dict):
        effective.update(message_start_usage)
    delta_usages = raw_summary.get("message_delta_usages")
    if isinstance(delta_usages, list):
        for usage in delta_usages:
            if isinstance(usage, dict):
                effective.update(usage)
    return effective


def _assert_usage_reconciliation(summary: dict[str, Any]) -> None:
    """对账 raw/trace/session，输入为完整摘要，输出为空或抛断言。"""
    trace_responses = summary.get("trace_responses")
    session_usages = summary.get("session_assistant_usage")
    raw_usages = summary.get("raw_usage")
    assert isinstance(trace_responses, list)
    assert isinstance(session_usages, list)
    assert isinstance(raw_usages, list)

    trace_by_response_id: dict[str, dict[str, Any]] = {}
    for trace_response in trace_responses:
        if not isinstance(trace_response, dict):
            continue
        provider_metadata = trace_response.get("provider_metadata")
        if isinstance(provider_metadata, dict):
            response_id = provider_metadata.get("id")
            if isinstance(response_id, str):
                trace_by_response_id[response_id] = trace_response

    session_by_response_id: dict[str, dict[str, Any]] = {}
    for session_usage in session_usages:
        if not isinstance(session_usage, dict):
            continue
        payload = session_usage.get("usage")
        if not isinstance(payload, dict):
            continue
        snapshot = ProviderUsageSnapshot.from_payload(payload)
        if snapshot.provider_response_id is not None:
            session_by_response_id[snapshot.provider_response_id] = payload

    assert len(trace_by_response_id) >= 2
    assert len(session_by_response_id) >= 2
    for raw_usage in raw_usages:
        assert isinstance(raw_usage, dict)
        raw_message = raw_usage.get("raw_message")
        assert isinstance(raw_message, dict)
        response_id = raw_message.get("id")
        assert isinstance(response_id, str) and response_id
        effective = _effective_anthropic_usage(raw_usage)

        trace_response = trace_by_response_id[response_id]
        trace_payload = trace_response.get("usage")
        assert isinstance(trace_payload, dict)
        session_payload = session_by_response_id[response_id]
        assert trace_payload == session_payload
        nested_response = trace_response.get("response")
        assert isinstance(nested_response, dict)
        assert nested_response.get("usage") == trace_payload

        snapshot = ProviderUsageSnapshot.from_payload(trace_payload)
        assert snapshot.raw_usage == effective
        assert snapshot.input_uncached_tokens.value == effective.get("input_tokens")
        assert snapshot.cache_read_tokens.value == effective.get("cache_read_input_tokens")
        assert snapshot.cache_write_tokens.value == effective.get("cache_creation_input_tokens")
        assert snapshot.output_total_tokens.value == effective.get("output_tokens")


async def test_minimax_m3_raw_usage_two_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续跑两轮真实 M3，输入为短 prompt，输出为 raw/trace/session/summary 文件。"""
    base_cfg = load_config(Path("config/setting.yaml"))
    preset_id = "minimax-m3"
    resolved_model = ModelCatalogManager().resolve_runtime(base_cfg.model, preset_id=preset_id)
    if resolved_model.api_key_env and not os.getenv(resolved_model.api_key_env, "").strip():
        pytest.skip(f"{resolved_model.api_key_env} is required for {preset_id} preset")

    run_root = _default_run_root().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KONGMING_HOME", str(run_root))
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    cfg = base_cfg.model_copy(
        update={
            "model": base_cfg.model.model_copy(
                update={"preset_id": preset_id, "reasoning_effort": "high"}
            ),
            "runner": base_cfg.runner.model_copy(update={"max_turns": 3}),
            "session": base_cfg.session.model_copy(
                update={"backend": "file", "file_store_path": ".kongming/sessions"}
            ),
            "trace": base_cfg.trace.model_copy(
                update={
                    "raw_llm": True,
                    "output_path": ".kongming/trace.jsonl",
                    "auto_flush": True,
                }
            ),
            "scheduler": base_cfg.scheduler.model_copy(update={"enabled": False}),
            "stream": base_cfg.stream.model_copy(
                update={"enabled": True, "delta_sampling": "none"}
            ),
        }
    )

    instructions = "You are a diagnostic test assistant. Answer in one short sentence."
    bootstrap = SessionBootstrap(
        agent_name="m3-raw-usage-live",
        model_name=resolved_model.name,
        instruction_sources=["tests/e2e/test_minimax_m3_raw_usage_live.py"],
        instruction_text_hash=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        instruction_text=instructions,
        created_at=time.time(),
        cwd=str(Path.cwd().resolve()),
    )

    def _session_factory(session_id: str):
        """构造 file session，输入为 session_id，输出为持久化 Session。"""
        return build_session(cfg, session_id, bootstrap=bootstrap)

    trace_sink = JsonlTraceSink(".kongming/trace.jsonl", auto_flush=True)
    runtime = SessionEngine.build(
        cfg,
        event_sinks=[trace_sink],
        approval=AutoAllowApproval(),
        tools={},
        enabled_tool_names=[],
        session_factory=_session_factory,
        agent_spec=AgentSpec(
            name="m3-raw-usage-live",
            instructions=instructions,
            default_model=resolved_model.name,
            tool_names=(),
            max_turns=3,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )

    prompts = [
        "Reply with exactly: M3 raw usage turn one.",
        "Reply with exactly: M3 raw usage turn two.",
    ]
    results: list[dict[str, Any]] = []
    try:
        for prompt in prompts:
            result = await runtime.run(prompt, session_id=_SESSION_ID)
            results.append(
                {
                    "run_id": result.run_id,
                    "status": result.status,
                    "turn_count": result.turn_count,
                    "error": str(result.error) if result.error else None,
                    "metadata": result.metadata,
                }
            )
            assert result.status == "completed", (
                f"M3 run failed: status={result.status} error={result.error}"
            )
    finally:
        await runtime.aclose()

    raw_dir = run_root / "debug"
    raw_files = sorted(raw_dir.glob("raw-llm-*.json"))
    trace_path = run_root / "trace.jsonl"
    session_path = run_root / "sessions" / _SESSION_ID / f"{_SESSION_ID}.jsonl"

    assert len(raw_files) >= 2, f"expected at least 2 raw dumps under {raw_dir}"
    assert trace_path.exists(), f"trace file missing: {trace_path}"
    assert session_path.exists(), f"session jsonl missing: {session_path}"

    summary = {
        "run_root": str(run_root),
        "model": resolved_model.name,
        "base_url": resolved_model.base_url,
        "session_id": _SESSION_ID,
        "results": results,
        "raw_files": [str(path) for path in raw_files],
        "raw_usage": [_raw_usage_summary(path) for path in raw_files],
        "trace_responses": _trace_response_summaries(trace_path),
        "session_assistant_usage": _session_assistant_usage(session_path),
        "paths": {
            "trace": str(trace_path),
            "session": str(session_path),
            "raw_dir": str(raw_dir),
        },
    }
    _assert_usage_reconciliation(summary)
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"m3_raw_log_dir={run_root}")
    print(f"m3_raw_summary={summary_path}")
    print(f"m3_raw_files={len(raw_files)}")
