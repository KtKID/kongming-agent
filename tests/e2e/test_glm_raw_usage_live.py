"""GLM raw usage live e2e。

本测试用于抓取真实 GLM OpenAI-compatible 流式返回，验证 raw chunks、
trace usage、file session usage 三层数据是否一致。默认跳过，显式设置
``KONGMING_E2E_REAL_MODEL=1`` 后才会发起真实模型请求。

输出目录默认是 ``log/glm-raw-usage-<utc-ts>/``，目录内包含：
- ``debug/raw-llm-*.json``：provider 原始 request + response chunks
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
from infrastructure.config import load_config
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.tracing import JsonlTraceSink
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tools import AutoAllowApproval

pytestmark = pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="set KONGMING_E2E_REAL_MODEL=1 to run real GLM raw usage e2e",
)


_SESSION_ID = "glm-raw-usage-live"


def _default_run_root() -> Path:
    """生成本次测试的日志根目录，输入为空，输出为仓库内 log 子目录路径。"""
    env_dir = os.getenv("KONGMING_E2E_GLM_RAW_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("log") / f"glm-raw-usage-{ts}"


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，输入为路径，输出为字典。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _raw_usage_summary(raw_path: Path) -> dict[str, Any]:
    """摘取 OpenAI-compatible raw chunk usage，输入为 raw 文件，输出为 usage 摘要。"""
    record = _load_json(raw_path)
    request_payload = record.get("request", {}).get("payload", {})
    response = record.get("response", {})
    body = response.get("body", {})
    chunks = body.get("chunks", []) if isinstance(body, dict) else []

    usage_chunks: list[dict[str, Any]] = []
    choice_finish_reasons: list[Any] = []
    response_ids: list[str] = []
    response_models: list[str] = []

    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_id = chunk.get("id")
            if isinstance(chunk_id, str):
                response_ids.append(chunk_id)
            chunk_model = chunk.get("model")
            if isinstance(chunk_model, str):
                response_models.append(chunk_model)
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage_chunks.append(dict(raw_usage))
            choices = chunk.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                        choice_finish_reasons.append(choice.get("finish_reason"))

    messages = request_payload.get("messages")
    message_roles: list[str] = []
    system_chars = 0
    system_message_count = 0
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if isinstance(role, str):
                message_roles.append(role)
            if role == "system":
                system_message_count += 1
                content = message.get("content")
                if isinstance(content, str):
                    system_chars += len(content)
    return {
        "file": str(raw_path),
        "provider": record.get("provider"),
        "status_code": response.get("status_code"),
        "error": record.get("error"),
        "request": {
            "model": request_payload.get("model"),
            "stream": request_payload.get("stream"),
            "stream_options": request_payload.get("stream_options"),
            "message_count": len(messages) if isinstance(messages, list) else None,
            "message_roles": message_roles,
            "system_message_count": system_message_count,
            "system_chars": system_chars,
            "has_tools": bool(request_payload.get("tools")),
            "has_thinking": "thinking" in request_payload,
        },
        "raw_response": {
            "ids": sorted(set(response_ids)),
            "models": sorted(set(response_models)),
            "finish_reasons": choice_finish_reasons,
        },
        "usage_chunks": usage_chunks,
        "final_usage": usage_chunks[-1] if usage_chunks else None,
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


async def test_glm_raw_usage_two_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续跑两轮真实 GLM，输入为短 prompt，输出为 raw/trace/session/summary 文件。"""
    base_cfg = load_config(Path("config/setting.yaml"))
    preset_id = "bigmodel-glm5-1m"
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

    system_prompt_path = Path("src/prompts/templates/SITIAN_ANALYZER.md")
    instructions = system_prompt_path.read_text(encoding="utf-8")
    bootstrap = SessionBootstrap(
        agent_name="glm-raw-usage-live",
        model_name=resolved_model.name,
        instruction_sources=[str(system_prompt_path)],
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
            name="glm-raw-usage-live",
            instructions=instructions,
            default_model=resolved_model.name,
            tool_names=(),
            max_turns=3,
            reasoning_effort=cfg.model.reasoning_effort,
        ),
    )

    prompts = ["hi", "你好"]
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
                f"GLM run failed: status={result.status} error={result.error}"
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
        "system_prompt_path": str(system_prompt_path.resolve()),
        "system_prompt_chars": len(instructions),
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
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"glm_raw_log_dir={run_root}")
    print(f"glm_raw_summary={summary_path}")
    print(f"glm_raw_files={len(raw_files)}")
