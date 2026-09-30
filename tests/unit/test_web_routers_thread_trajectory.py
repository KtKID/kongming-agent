"""unit：GET /api/threads/{thread_id}/trajectory 路由契约测试（track-timeline-v0.1）。

走真实 FastAPI ``create_app`` + 真实 ``TrajectoryManager``（临时目录落盘
真实 trace.jsonl），只 stub ``ThreadManager.list_threads``（thread 生命周期
不是本测试目标）。覆盖：

1. 200 完整轨迹（runs / records / summary 投影贯通）
2. 200 空轨迹（thread 存在但无 trace 文件）
3. 404 thread 不存在
4. 200 + 损坏占位（trace 文件含坏行）
5. 200 + 进行中（run.start 无 run.end）
6. 422 thread_id 不合法

关键函数：

- ``_trace_line``：构造与 JsonlTraceSink 落盘形状一致的 trace 行。
- ``_write_trace``：把行写进 ``<home>/.kongming/sessions/<tid>/trace.jsonl``。
- ``_login_client``：登录后的 TestClient（复用 threads 路由测试模式）。
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


def _make_cfg() -> Config:
    """最小可用配置（session 走默认 file backend + 默认存储路径）。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _trace_line(
    kind: str,
    *,
    run_id: str = "t-1",
    turn: int | None = None,
    ts: int = 0,
    payload: dict[str, Any] | None = None,
) -> str:
    """构造一条与 JsonlTraceSink 落盘形状一致的 trace 行。"""
    event = Event(
        kind=kind,
        run_id=run_id,
        turn=turn,
        payload=payload or {},
        timestamp_ms=1_700_000_000_000 + ts,
    )
    return json.dumps(dataclasses.asdict(event), ensure_ascii=False)


def _write_trace(home: Path, thread_id: str, lines: list[str]) -> None:
    """把 trace 行写进 <home>/sessions/<tid>/trace.jsonl。

    注意：``kongming_home`` 本身就是 ``.kongming`` 根目录，
    ``resolve_kongming_path(".kongming/sessions")`` 会剥前缀拼成
    ``<home>/sessions``——与 run.py 落盘路径同源。
    """
    trace_path = home / "sessions" / thread_id / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_session_sources(home: Path, thread_id: str) -> None:
    """写 session 真源三件套：manifest + 消息 jsonl + 系统提示词。"""
    session_dir = home / "sessions" / thread_id
    session_dir.mkdir(parents=True, exist_ok=True)
    base_ts = 1_700_000_000.0
    (session_dir / "manifest.json").write_text(
        json.dumps({"format": f"{thread_id}.jsonl"}), encoding="utf-8"
    )
    messages = [
        {
            "record_type": "message",
            "created_at": base_ts + 0.5,
            "message": {"role": "user", "content": "读配置"},
        },
        {
            "record_type": "message",
            "created_at": base_ts + 1.5,
            "message": {"role": "assistant", "content": "好，正在读"},
            "reasoning_content": "用户要读配置",
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
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)  # type: ignore[arg-type]
    client = TestClient(app)
    client.__enter__()
    resp = client.post("/api/auth/login", json={"password": "pwd"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    return client


def test_trajectory_full(tmp_path: Path) -> None:
    """thread 存在 + 完整双源 → 200，system/user/llm/tool 投影贯通。"""
    # trace 用真实脱敏形状（request 无 messages 正文，response 只带 content_chars）
    _write_trace(
        tmp_path,
        THREAD_ID,
        [
            _trace_line("run.start", ts=0),
            _trace_line(
                "llm.request",
                turn=1,
                ts=10,
                payload={"request": {"message_roles": ["system", "user"]}},
            ),
            _trace_line(
                "llm.response",
                turn=1,
                ts=110,
                payload={
                    "response": {"message": {"role": "assistant", "content_chars": 5}},
                    "finish_reason": "stop",
                },
            ),
            _trace_line(
                "tool.call.start",
                turn=1,
                ts=120,
                payload={
                    "call_id": "c1",
                    "tool_name": "read_file",
                    "arguments": {"path": "setting.yaml"},
                },
            ),
            _trace_line(
                "tool.call.end",
                turn=1,
                ts=180,
                payload={
                    "call_id": "c1",
                    "tool_name": "read_file",
                    "ok": True,
                    "content": "debug: true",
                },
            ),
            _trace_line("run.end", turn=1, ts=200, payload={"status": "completed"}),
        ],
    )
    _write_session_sources(tmp_path, THREAD_ID)
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["thread_id"] == THREAD_ID
        assert body["summary"]["status"] == "completed"
        assert body["summary"]["tool_call_count"] == 1
        assert [run["status"] for run in body["runs"]] == ["completed"]
        kinds = [record["kind"] for record in body["records"]]
        assert kinds == ["system", "user_input", "llm", "tool"]
        system = body["records"][0]
        assert system["seq"] == -1
        assert system["input_detail"] == "# 孔明系统提示词"
        user = body["records"][1]
        # 用户输入全文来自 session 真源（trace 事件不落正文）
        assert user["input_detail"] == "读配置"
        llm = body["records"][2]
        assert llm["output_detail"] == "好，正在读"
        assert llm["reasoning_detail"] == "用户要读配置"
        tool = body["records"][3]
        assert tool["input_detail"] == '{"path": "setting.yaml"}'
        assert tool["output_detail"] == "debug: true"
    finally:
        client.__exit__(None, None, None)


def test_trajectory_session_only_without_trace(tmp_path: Path) -> None:
    """老 thread 无 trace.jsonl → session 真源仍产出 system/user_input 记录。"""
    _write_session_sources(tmp_path, THREAD_ID)
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["runs"] == []
        kinds = [record["kind"] for record in body["records"]]
        assert kinds == ["system", "user_input"]
        assert body["records"][1]["input_detail"] == "读配置"
    finally:
        client.__exit__(None, None, None)


def test_trajectory_empty_when_no_trace_file(tmp_path: Path) -> None:
    """thread 存在但无 trace 文件 → 200 空轨迹（不是 404）。"""
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["runs"] == []
        assert body["records"] == []
        assert body["summary"]["tool_call_count"] == 0
    finally:
        client.__exit__(None, None, None)


def test_trajectory_thread_not_found(tmp_path: Path) -> None:
    """thread 不存在 → 404。"""
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-000000000001/trajectory")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_trajectory_corrupted_lines_placeholder(tmp_path: Path) -> None:
    """trace 含坏行 → 200 + corrupted 占位记录，不中断。"""
    _write_trace(
        tmp_path,
        THREAD_ID,
        [
            _trace_line("run.start", ts=0),
            "不是 JSON",
            _trace_line("run.end", turn=1, ts=50, payload={"status": "completed"}),
        ],
    )
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["corrupted_count"] == 1
        corrupted = [r for r in body["records"] if r["kind"] == "corrupted"]
        assert len(corrupted) == 1
        assert corrupted[0]["seq"] == 1
        assert body["runs"][0]["status"] == "completed"
    finally:
        client.__exit__(None, None, None)


def test_trajectory_running_run(tmp_path: Path) -> None:
    """run.start 无 run.end → summary 与 run 均 running。"""
    _write_trace(
        tmp_path,
        THREAD_ID,
        [
            _trace_line("run.start", ts=0),
            _trace_line(
                "llm.request",
                turn=1,
                ts=10,
                payload={"request": {"messages": [{"role": "user", "content": "进行中"}]}},
            ),
        ],
    )
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get(f"/api/threads/{THREAD_ID}/trajectory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["status"] == "running"
        assert body["runs"][0]["status"] == "running"
    finally:
        client.__exit__(None, None, None)


def test_trajectory_invalid_thread_id(tmp_path: Path) -> None:
    """thread_id 不匹配正则 → 422。"""
    tm = _FakeTM([THREAD_ID])
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/not-a-thread-id/trajectory")
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)
