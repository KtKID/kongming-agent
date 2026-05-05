"""jsonl_history.py 单测（v0.2）。

覆盖：
1. jsonl_path_for 路径计算（默认 home / 注入 home）
2. user content=string → kind=text role=user
3. user content=[tool_result] → kind=tool_result
4. assistant.content text/thinking/tool_use block 三类
5. system + subtype=init → session_created
6. 异常 type 全 skip（attachment / queue-operation / file-history-snapshot 等）
7. sessionId 不匹配 skip
8. 行 JSON 损坏 skip 不抛
9. 真实 fixture smoke
10. 排序按 timestamp 升序
"""

from __future__ import annotations

import json
from pathlib import Path

from web.claude_code.jsonl_history import jsonl_path_for, parse_jsonl_history


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_jsonl_path_for_default_home(tmp_path: Path) -> None:
    # 注入 home 验证编码 + 拼接
    p = jsonl_path_for("/foo/bar", "abc-123", claude_home=tmp_path)
    assert p == tmp_path / "projects" / "-foo-bar" / "abc-123.jsonl"


def test_jsonl_path_for_encodes_underscore_dot_as_dash(tmp_path: Path) -> None:
    """SDK 编码把 / _ . 都换成 - ；- 已是分隔符保留。"""
    p = jsonl_path_for(
        "/Volumes/machub_app/proj/kongming-agent",
        "sid-1",
        claude_home=tmp_path,
    )
    expected = tmp_path / "projects" / "-Volumes-machub-app-proj-kongming-agent" / "sid-1.jsonl"
    assert p == expected
    # 含 . 的路径
    p2 = jsonl_path_for("/foo/x.skillhub", "s", claude_home=tmp_path)
    assert p2 == tmp_path / "projects" / "-foo-x-skillhub" / "s.jsonl"


def test_jsonl_path_for_default_uses_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = jsonl_path_for("/foo/bar", "sid-xyz")
    assert str(p).startswith(str(tmp_path))
    assert p.name == "sid-xyz.jsonl"


def test_parse_user_text_message(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
                "message": {"role": "user", "content": "hello world"},
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "text"
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello world"
    assert out[0]["sessionId"] == sid


def test_parse_user_tool_result_block(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
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
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "tool_result"
    assert out[0]["toolId"] == "tu-1"
    assert out[0]["content"] == "result text"
    assert out[0]["isError"] is False


def test_parse_assistant_text_block(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi there"}],
                },
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "text"
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "hi there"


def test_parse_assistant_thinking_block(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "let me think...", "signature": "sig"}
                    ],
                },
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "thinking"
    assert out[0]["content"] == "let me think..."


def test_parse_assistant_tool_use_block(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
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
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "tool_use"
    assert out[0]["toolName"] == "Bash"
    assert out[0]["toolInput"] == {"command": "ls"}
    assert out[0]["toolId"] == "tu-1"


def test_parse_system_init(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "system",
                "subtype": "init",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["kind"] == "session_created"
    assert out[0]["newSessionId"] == sid


def test_skip_unknown_types(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "attachment", "sessionId": sid, "timestamp": "2026-05-02T10:00:00Z"},
            {"type": "queue-operation", "sessionId": sid, "timestamp": "2026-05-02T10:01:00Z"},
            {
                "type": "file-history-snapshot",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:02:00Z",
            },
            {"type": "permission-mode", "sessionId": sid, "timestamp": "2026-05-02T10:03:00Z"},
            {"type": "last-prompt", "sessionId": sid, "timestamp": "2026-05-02T10:04:00Z"},
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert out == []


def test_skip_session_id_mismatch(tmp_path: Path) -> None:
    sid = "sid-target"
    other = "sid-other"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": other,
                "timestamp": "2026-05-02T10:00:00Z",
                "message": {"role": "user", "content": "from other session"},
            },
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:01:00Z",
                "message": {"role": "user", "content": "from target"},
            },
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["content"] == "from target"


def test_skip_corrupt_json_line(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("not json {{{\n")
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": sid,
                    "timestamp": "2026-05-02T10:00:00Z",
                    "message": {"role": "user", "content": "valid"},
                }
            )
            + "\n"
        )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["content"] == "valid"


def test_sort_by_timestamp_ascending(tmp_path: Path) -> None:
    sid = "sid-1"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:02:00Z",
                "message": {"role": "user", "content": "second"},
            },
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:00:00Z",
                "message": {"role": "user", "content": "first"},
            },
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-02T10:01:00Z",
                "message": {"role": "user", "content": "middle"},
            },
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert [o["content"] for o in out] == ["first", "middle", "second"]


def test_real_fixture_smoke() -> None:
    """真实 fixture 端到端："""
    fixture = Path(
        "/Users/kid/.claude/projects/"
        "-Volumes-machub-app-proj-test/"
        "2c3c0aeb-f154-4f28-88a9-cd0fc20e6a14.jsonl"
    )
    if not fixture.exists():
        # 跳过：fixture 文件可能不在所有环境
        return
    sid = "2c3c0aeb-f154-4f28-88a9-cd0fc20e6a14"
    out = parse_jsonl_history(fixture, sid)
    # fixture 含 4 user + 1 assistant + system(init/其他)
    assert len(out) >= 4
    for r in out:
        assert r["sessionId"] == sid
        assert r["provider"] == "claude"
        assert r["kind"] in {"text", "thinking", "tool_use", "tool_result", "session_created"}
