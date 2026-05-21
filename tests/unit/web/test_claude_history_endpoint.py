"""GET /api/threads/{id}/claude_history endpoint 单测（v0.2 #6）。

覆盖：
1. 未登录 → 401
2. thread 不存在 → 400
3. backend_kind=generic_chat → 400
4. claude_code 但 claude_thread_id="" → 400
5. jsonl 文件不存在 → 404
6. happy path → messages
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_web_routers_threads import FakeTM, _login_client
from web.thread_metadata import ThreadMetadata


def _add_thread(
    tm: FakeTM,
    thread_id: str = "thread-aaaaaaaaaaaa",
    backend_kind: str = "claude_code",
    claude_thread_id: str = "",
    cwd: str = "",
) -> ThreadMetadata:
    meta = ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="",
        backend_kind=backend_kind,  # type: ignore[arg-type]
        claude_thread_id=claude_thread_id,
        cwd=cwd,
        created_at=1.0,
        updated_at=1.0,
        message_count=0,
    )
    tm._threads[thread_id] = meta
    return meta


def test_unauthenticated_returns_401(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from tests.unit.test_web_app_lifespan import _seed_password
    from tests.unit.test_web_routers_threads import _make_cfg
    from web.app import create_app

    _seed_password(tmp_path, "pwd")
    tm = FakeTM()
    app = create_app(_make_cfg(), tm, home_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 401


def test_thread_not_found_returns_400(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_backend_kind_mismatch_returns_400(tmp_path: Path) -> None:
    tm = FakeTM()
    _add_thread(tm, backend_kind="generic_chat")
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_no_claude_thread_id_returns_400(tmp_path: Path) -> None:
    tm = FakeTM()
    _add_thread(tm, backend_kind="claude_code", claude_thread_id="")
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_jsonl_missing_returns_404(tmp_path: Path, monkeypatch) -> None:
    tm = FakeTM()
    _add_thread(
        tm,
        backend_kind="claude_code",
        claude_thread_id="not-exist-uuid",
        cwd="/tmp/nonexistent",
    )
    # mock jsonl_path_for 返回不存在的路径
    monkeypatch.setattr(
        "web.routers.threads.jsonl_path_for",
        lambda cwd, sid: tmp_path / "nope.jsonl",
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_happy_path_returns_messages(tmp_path: Path, monkeypatch) -> None:
    tm = FakeTM()
    sid = "sid-xyz"
    _add_thread(
        tm,
        backend_kind="claude_code",
        claude_thread_id=sid,
        cwd="/tmp/work",
    )
    # 写一份小 jsonl
    jsonl = tmp_path / "history.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:00:00Z",
                    "message": {"role": "user", "content": "hi"},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:01:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi!"}],
                    },
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        "web.routers.threads.jsonl_path_for",
        lambda cwd, sid_: jsonl,
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body
        msgs = body["messages"]
        assert len(msgs) == 2
        assert msgs[0]["kind"] == "text"
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hi"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Hi!"
    finally:
        client.__exit__(None, None, None)


def test_history_endpoint_returns_recent_tail_only(tmp_path: Path, monkeypatch) -> None:
    tm = FakeTM()
    sid = "sid-tail"
    _add_thread(
        tm,
        backend_kind="claude_code",
        claude_thread_id=sid,
        cwd="/tmp/work",
    )
    jsonl = tmp_path / "history-tail.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for i in range(5):
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": f"u{i}",
                        "sessionId": sid,
                        "timestamp": f"2026-05-02T10:0{i}:00Z",
                        "message": {"role": "user", "content": f"msg-{i}"},
                    }
                )
                + "\n"
            )
    monkeypatch.setattr(
        "web.routers.threads.jsonl_path_for",
        lambda cwd, sid_: jsonl,
    )
    monkeypatch.setattr("web.routers.threads.CLAUDE_HISTORY_MAX_MESSAGES", 2)
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 200
        body = resp.json()
        assert [m["content"] for m in body["messages"]] == ["msg-3", "msg-4"]
    finally:
        client.__exit__(None, None, None)


def test_history_endpoint_filters_tool_entries_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tm = FakeTM()
    sid = "sid-tools"
    _add_thread(
        tm,
        backend_kind="claude_code",
        claude_thread_id=sid,
        cwd="/tmp/work",
    )
    jsonl = tmp_path / "history-tools.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a0",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "before tool"}],
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:01:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:02:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": "result text",
                                "is_error": False,
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:03:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "after tool"}],
                    },
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        "web.routers.threads.jsonl_path_for",
        lambda cwd, sid_: jsonl,
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history")
        assert resp.status_code == 200
        body = resp.json()
        assert [m["kind"] for m in body["messages"]] == ["text", "text"]
        assert [m["content"] for m in body["messages"]] == ["before tool", "after tool"]
    finally:
        client.__exit__(None, None, None)


def test_history_endpoint_include_tools_true_restores_tool_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tm = FakeTM()
    sid = "sid-tools-full"
    _add_thread(
        tm,
        backend_kind="claude_code",
        claude_thread_id=sid,
        cwd="/tmp/work",
    )
    jsonl = tmp_path / "history-tools-full.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:01:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:02:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": "result text",
                                "is_error": False,
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        "web.routers.threads.jsonl_path_for",
        lambda cwd, sid_: jsonl,
    )
    client = _login_client(tmp_path, tm)
    try:
        resp = client.get("/api/threads/thread-aaaaaaaaaaaa/claude_history?include_tools=true")
        assert resp.status_code == 200
        body = resp.json()
        assert [m["kind"] for m in body["messages"]] == ["tool_use", "tool_result"]
    finally:
        client.__exit__(None, None, None)
