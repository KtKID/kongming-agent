"""jsonl_history.py 单测（v0.2）。

覆盖：
1. jsonl_path_for 路径计算（默认 home / 注入 home）
2. user content=string → frame_type=text role=user
3. user content=[tool_result] → frame_type=tool_result
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
    assert out[0]["frame_type"] == "text"
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello world"
    assert out[0]["sessionId"] == sid


def test_parse_user_string_task_notification_dropped(tmp_path: Path) -> None:
    """CLI 注入的 ``<task-notification>`` UserMessage 必须被丢弃——
    否则前端历史回放渲染成右侧用户气泡（"用户没说却出现"bug）。

    live 流由 normalizer ``_handle_user`` 直接 ``return []`` 隔离；本测试守住
    history 路径的对齐行为（共用 ``_content_filter`` 模块）。
    """
    sid = "sid-task-notif"
    path = tmp_path / "f.jsonl"
    content = (
        "<task-notification>\n"
        "  <task-id>a61683b77a198a224</task-id>\n"
        "  <tool-use-id>toolu_01GnGirqTjMZ9J4B1DANb1ou</tool-use-id>\n"
        "  <status>completed</status>\n"
        "  <summary>Agent x-dev #5+#6 completed</summary>\n"
        "</task-notification>"
    )
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "u-tn-1",
                "sessionId": sid,
                "timestamp": "2026-05-17T10:00:00Z",
                "message": {"role": "user", "content": content},
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert out == []


def test_parse_user_string_system_reminder_dropped(tmp_path: Path) -> None:
    """同一过滤机制覆盖 ``<system-reminder>``——证明过滤是基于共享 prefix 列表，
    不是只对 ``<task-notification>`` 硬编码。"""
    sid = "sid-sysrem"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "u-sr-1",
                "sessionId": sid,
                "timestamp": "2026-05-17T10:00:01Z",
                "message": {
                    "role": "user",
                    "content": "<system-reminder>auto-injected guidance</system-reminder>",
                },
            }
        ],
    )
    assert parse_jsonl_history(path, sid) == []


def test_parse_user_string_plain_text_still_passes(tmp_path: Path) -> None:
    """对照：普通用户文本（不命中前缀）仍正常吐出 ``text role=user`` — 不退化。"""
    sid = "sid-plain"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "u-pln-1",
                "sessionId": sid,
                "timestamp": "2026-05-17T10:00:02Z",
                "message": {"role": "user", "content": "你好，帮我搜一下白板代码"},
            }
        ],
    )
    out = parse_jsonl_history(path, sid)
    assert len(out) == 1
    assert out[0]["frame_type"] == "text"
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "你好，帮我搜一下白板代码"


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
    assert out[0]["frame_type"] == "tool_result"
    assert out[0]["toolId"] == "tu-1"
    assert out[0]["content"] == "result text"
    assert out[0]["isError"] is False


def test_parse_user_tool_result_block_skipped_when_include_tools_false(tmp_path: Path) -> None:
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
    assert parse_jsonl_history(path, sid, include_tools=False) == []


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
    assert out[0]["frame_type"] == "text"
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
    assert out[0]["frame_type"] == "thinking"
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
    assert out[0]["frame_type"] == "tool_use"
    assert out[0]["toolName"] == "Bash"
    assert out[0]["toolInput"] == {"command": "ls"}
    assert out[0]["toolId"] == "tu-1"


def test_parse_assistant_tool_use_block_skipped_when_include_tools_false(
    tmp_path: Path,
) -> None:
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
    assert parse_jsonl_history(path, sid, include_tools=False) == []


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
    assert out[0]["frame_type"] == "session_created"
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


def test_max_messages_keeps_recent_tail(tmp_path: Path) -> None:
    sid = "sid-tail"
    path = tmp_path / "f.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": sid,
                "timestamp": f"2026-05-02T10:0{i}:00Z",
                "message": {"role": "user", "content": f"msg-{i}"},
            }
            for i in range(5)
        ],
    )
    out = parse_jsonl_history(path, sid, max_messages=2)
    assert [o["content"] for o in out] == ["msg-3", "msg-4"]


def test_real_fixture_smoke() -> None:
    """真实 fixture 端到端 smoke。

    阈值历史：旧值 ``>= 4`` 时，fixture 里 2 条 ``<task-notification>`` 伪装
    UserMessage 被错误透传成 user-role text；2026-05-17 引入 ``_content_filter``
    后这 2 条被正确丢弃，预期输出降到 ``>= 2``（至少 1 个真实用户文本 + 1 个
    assistant 回复）。
    """
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
    assert len(out) >= 2
    for r in out:
        assert r["sessionId"] == sid
        assert r["provider"] == "claude"
        assert r["frame_type"] in {"text", "thinking", "tool_use", "tool_result", "session_created"}
        # 核心回归：内部前缀不应再出现在 history 输出（与 normalizer live 流对齐）
        if r["frame_type"] == "text" and r.get("role") == "user":
            content = r.get("content")
            assert isinstance(content, str)
            assert not content.startswith("<task-notification>"), (
                "fixture 残留的 <task-notification> 必须被 _content_filter 过滤"
            )
