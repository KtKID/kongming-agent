"""unit：轨迹跨端 smoke（track-timeline-v0.1 · task-trajectory-frontend T10）。

薄链路：真实 create_app + 真实双源落盘（trace.jsonl + system_prompt.json +
session jsonl）→ GET trajectory → 断言前端消费的关键字段名与结构（与
``web/src/modules/trajectory/__tests__/TrajectoryPanel.test.tsx`` 的 fixture
同源对齐——后端产出结构变化时，两侧 fixture 必须同步改）。

关键函数：

- ``_trace_line``：构造与 JsonlTraceSink 落盘形状一致的 trace 行
  （llm.request / llm.response 均为脱敏形状，无消息正文）。
- ``_write_sources``：落盘轨迹双源（trace + manifest + session jsonl +
  system_prompt.json）。
- ``_login_client``：登录后的 TestClient（复用 threads 路由测试装配模式）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from core.contracts import Event
from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
THREAD_ID = "thread-abcdef012345"


class _FakeTM:
    """只支持（同步）list_threads + 生命周期的最小 ThreadManager 替身。"""

    def __init__(self, thread_ids: list[str]) -> None:
        self._metas = [SimpleNamespace(id=tid) for tid in thread_ids]

    def list_threads(self) -> list[SimpleNamespace]:
        return list(self._metas)

    async def start(self) -> None:
        return None

    async def aclose_all(self) -> None:
        return None


def _trace_line(kind: str, *, ts: int = 0, payload: dict[str, Any] | None = None) -> str:
    """构造一条与 JsonlTraceSink 落盘形状一致的 trace 行。"""
    event = Event(
        kind=kind,
        run_id="t-1",
        turn=1,
        payload=payload or {},
        timestamp_ms=1_700_000_000_000 + ts,
    )
    return json.dumps(dataclasses.asdict(event), ensure_ascii=False)


def _write_sources(home: Path, thread_id: str) -> None:
    """落盘轨迹双源：trace.jsonl + session 真源三件套。"""
    session_dir = home / "sessions" / thread_id
    session_dir.mkdir(parents=True, exist_ok=True)
    base_ts = 1_700_000_000.0
    (session_dir / "trace.jsonl").write_text(
        "\n".join(
            [
                _trace_line("run.start"),
                _trace_line(
                    "llm.request",
                    ts=10,
                    payload={"request": {"message_roles": ["system", "user"]}},
                ),
                _trace_line("llm.chunk.first", ts=130, payload={"elapsed_ms": 120}),
                _trace_line(
                    "llm.response",
                    ts=810,
                    payload={
                        "response": {
                            "message": {
                                "role": "assistant",
                                "content_chars": 5,
                                "tool_call_count": 0,
                            }
                        },
                        "finish_reason": "stop",
                    },
                ),
                _trace_line(
                    "usage",
                    ts=811,
                    payload={
                        "metrics": {
                            "input_total_tokens": {"value": 100},
                            "output_total_tokens": {"value": 30},
                            "reasoning_tokens": {"value": 5},
                        }
                    },
                ),
                _trace_line(
                    "tool.call.start",
                    ts=900,
                    payload={
                        "call_id": "c1",
                        "tool_name": "read_file",
                        "arguments": {"path": "setting.yaml"},
                    },
                ),
                _trace_line(
                    "tool.call.end",
                    ts=1000,
                    payload={
                        "call_id": "c1",
                        "tool_name": "read_file",
                        "ok": True,
                        "content": "debug: true",
                    },
                ),
                _trace_line("run.end", ts=1100, payload={"status": "completed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (session_dir / "manifest.json").write_text(
        json.dumps({"format": f"{thread_id}.jsonl"}), encoding="utf-8"
    )
    messages = [
        {
            "record_type": "message",
            "created_at": base_ts + 0.5,
            "message": {"role": "user", "content": "帮我读配置"},
        },
        {
            "record_type": "message",
            "created_at": base_ts + 1.5,
            "message": {"role": "assistant", "content": "我来读配置"},
            "reasoning_content": "要读配置",
        },
    ]
    (session_dir / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
        encoding="utf-8",
    )
    (session_dir / "system_prompt.json").write_text(
        json.dumps(
            {"content": "# 孔明系统提示词", "created_at": base_ts},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _login_client(tmp_path: Path, tm: _FakeTM) -> TestClient:
    """登录后的 TestClient（复用 threads 路由测试装配模式）。"""
    _seed_password(tmp_path, "pwd")
    cfg = Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
        }
    )
    app = create_app(cfg, tm, home_dir=tmp_path)  # type: ignore[arg-type]
    client = TestClient(app)
    client.__enter__()
    resp = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    return client


def test_trajectory_smoke_frontend_consumed_fields(tmp_path: Path) -> None:
    """端点产出结构与前端 fixture 消费字段一致（跨端 smoke 最低链路）。"""
    _write_sources(tmp_path, THREAD_ID)

    client = _login_client(tmp_path, _FakeTM([THREAD_ID]))
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()

        # 前端 Panel / Ledger / Timeline / Inspector 消费的顶层字段
        assert body["thread_id"] == THREAD_ID
        assert {
            "status",
            "total_duration_ms",
            "turn_count",
            "tool_call_count",
            "corrupted_count",
            "tokens",
        } <= set(body["summary"])
        assert body["runs"][0]["status"] == "completed"
        assert body["runs"][0]["duration_ms"] == 1100

        records = body["records"]
        assert [r["kind"] for r in records] == [
            "system",
            "user_input",
            "llm",
            "tool",
        ]
        system, user, llm, tool = records
        # system：系统提示词全文（Inspector 系统详情）
        assert system["seq"] == -1
        assert system["input_detail"] == "# 孔明系统提示词"
        # user_input：全文来自 session 真源
        assert user["input_detail"] == "帮我读配置"
        # llm：trace 的 ttft / tokens + session 回填的正文与思考
        assert llm["ttft_ms"] == 120
        assert llm["tokens"]["input_total"] == 100
        assert llm["output_detail"] == "我来读配置"
        assert llm["reasoning_detail"] == "要读配置"
        # tool：入参 JSON 字符串 + 输出 + 耗时
        assert tool["input_detail"] == '{"path": "setting.yaml"}'
        assert tool["output_detail"] == "debug: true"
        assert tool["duration_ms"] == 100
        # 时间线消费：started_at_ms / duration_ms 齐备（为 v0.2 实际时间视角预留）
        for record in records:
            assert record["started_at_ms"] is not None
    finally:
        client.__exit__(None, None, None)
