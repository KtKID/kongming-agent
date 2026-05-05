"""projects_scanner.py 单测（v0.2）。

覆盖：
1. claude_home / projects_dir 不存在 → []
2. agent-*.jsonl 跳过
3. 空 jsonl 跳过
4. 损坏 jsonl → title="(无法解析)"
5. 没 user 消息 → title="(空会话)"
6. user 消息 title 截断 + 去换行
7. message_count = jsonl 总行数（不过滤）
8. sessions 按 mtime desc
9. projects 按 max(session.mtime) desc
10. cwd 解码 + display_name
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from web.claude_code.projects_scanner import list_projects


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_no_projects_dir_returns_empty(tmp_path: Path) -> None:
    # claude_home 存在但 projects 子目录不存在
    assert list_projects(claude_home=tmp_path) == []


def test_no_claude_home_returns_empty(tmp_path: Path) -> None:
    assert list_projects(claude_home=tmp_path / "missing") == []


def test_skip_agent_files(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo-bar"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "agent-abc.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}],
    )
    _write_jsonl(
        proj / "real.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "real"}}],
    )
    out = list_projects(claude_home=tmp_path)
    assert len(out) == 1
    sids = [s.sdk_session_id for s in out[0].sessions]
    assert "real" in sids
    assert not any(s.startswith("agent-") for s in sids)


def test_empty_jsonl_skipped(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    (proj / "empty.jsonl").write_text("", encoding="utf-8")
    _write_jsonl(
        proj / "valid.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "hi"}}],
    )
    out = list_projects(claude_home=tmp_path)
    assert len(out) == 1
    assert len(out[0].sessions) == 1
    assert out[0].sessions[0].sdk_session_id == "valid"


def test_corrupt_jsonl_title_unparsable(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    (proj / "bad.jsonl").write_text("garbage {{ not json\nmore garbage\n", encoding="utf-8")
    out = list_projects(claude_home=tmp_path)
    # 损坏文件计入但 title="(无法解析)" 或 "(空会话)"——具体看实现策略
    assert len(out) == 1
    assert len(out[0].sessions) == 1
    # 既不是空字符串也不是合法用户内容
    title = out[0].sessions[0].title
    assert title in {"(无法解析)", "(空会话)"}


def test_no_user_message_title_is_empty_session(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "noUser.jsonl",
        [
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        ],
    )
    out = list_projects(claude_home=tmp_path)
    assert len(out) == 1
    assert out[0].sessions[0].title == "(空会话)"


def test_title_truncated_to_40_chars_no_newlines(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    long_text = "ab\ncd\n" + "x" * 100
    _write_jsonl(
        proj / "f.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": long_text}}],
    )
    out = list_projects(claude_home=tmp_path)
    title = out[0].sessions[0].title
    assert len(title) <= 40
    assert "\n" not in title


def test_message_count_is_total_lines(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "f.jsonl",
        [
            {"type": "user", "sessionId": "s", "message": {"role": "user", "content": "a"}},
            {"type": "attachment"},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            },
            {"type": "queue-operation"},
        ],
    )
    out = list_projects(claude_home=tmp_path)
    # 不过滤 type，4 行计 4
    assert out[0].sessions[0].message_count == 4


def test_sessions_sorted_by_mtime_desc(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    old = proj / "old.jsonl"
    new = proj / "new.jsonl"
    _write_jsonl(
        old,
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "old"}}],
    )
    _write_jsonl(
        new,
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "new"}}],
    )
    # 强制 old 比 new 旧
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    out = list_projects(claude_home=tmp_path)
    assert [s.sdk_session_id for s in out[0].sessions] == ["new", "old"]


def test_projects_sorted_by_max_session_mtime_desc(tmp_path: Path) -> None:
    p_old = tmp_path / "projects" / "-old"
    p_new = tmp_path / "projects" / "-new"
    p_old.mkdir(parents=True)
    p_new.mkdir(parents=True)
    f_old = p_old / "x.jsonl"
    f_new = p_new / "x.jsonl"
    _write_jsonl(
        f_old, [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}]
    )
    _write_jsonl(
        f_new, [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}]
    )
    now = time.time()
    os.utime(f_old, (now - 100, now - 100))
    os.utime(f_new, (now, now))
    out = list_projects(claude_home=tmp_path)
    # -new 项目更晚 → 排前
    names = [p.name for p in out]
    assert names.index("-new") < names.index("-old")


def test_cwd_decoded_and_display_name(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "-foo-bar-baz"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "x.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}],
    )
    out = list_projects(claude_home=tmp_path)
    assert len(out) == 1
    p = out[0]
    assert p.name == "-foo-bar-baz"
    # entry 没 cwd 字段 → fallback 到目录名解码
    assert p.cwd == "/foo/bar/baz"
    assert p.display_name == "baz"


def test_cwd_prefers_jsonl_entry_real_cwd(tmp_path: Path) -> None:
    """关键修复：原 cwd 含 ``-`` / ``_`` 字符时，目录名编码不可逆；
    必须从 jsonl entry 的 ``cwd`` 字段读 SDK 写的真值。"""
    # 模拟真实场景：cwd=/Volumes/machub_app/proj/kongming-agent
    # SDK 把 / _ - . 都换成 - → 编码目录名 -Volumes-machub-app-proj-kongming-agent
    encoded = "-Volumes-machub-app-proj-kongming-agent"
    real_cwd = "/Volumes/machub_app/proj/kongming-agent"
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "session-1.jsonl",
        [
            {
                "type": "user",
                "sessionId": "session-1",
                "cwd": real_cwd,
                "message": {"role": "user", "content": "你是什么模型"},
            },
        ],
    )
    out = list_projects(claude_home=tmp_path)
    assert len(out) == 1
    p = out[0]
    assert p.name == encoded
    # 真实 cwd 来自 entry，不是错误的目录名解码 /Volumes/machub/app/proj/kongming/agent
    assert p.cwd == real_cwd
    assert p.display_name == "kongming-agent"


def test_cwd_extracted_from_first_entry_with_cwd(tmp_path: Path) -> None:
    """jsonl 里前几行没 cwd 字段（如 system 状态帧），跳过继续找。"""
    proj = tmp_path / "projects" / "-foo"
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "x.jsonl",
        [
            {"type": "system", "subtype": "init", "sessionId": "s"},  # 无 cwd
            {
                "type": "user",
                "sessionId": "s",
                "cwd": "/real/path-with-dash",
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    out = list_projects(claude_home=tmp_path)
    assert out[0].cwd == "/real/path-with-dash"
    assert out[0].display_name == "path-with-dash"


def test_progress_callback_receives_project_scan_progress(tmp_path: Path) -> None:
    p1 = tmp_path / "projects" / "-foo"
    p2 = tmp_path / "projects" / "-bar"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    _write_jsonl(
        p1 / "x.jsonl",
        [{"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "x"}}],
    )
    _write_jsonl(
        p2 / "y.jsonl",
        [{"type": "user", "sessionId": "s2", "message": {"role": "user", "content": "y"}}],
    )

    progress: list[tuple[int, int, str]] = []
    out = list_projects(
        claude_home=tmp_path,
        progress_callback=lambda current, total, current_project: progress.append(
            (current, total, current_project)
        ),
    )

    assert len(out) == 2
    assert len(progress) == 2
    assert progress[-1][0] == 2
    assert progress[-1][1] == 2
    assert {item[2] for item in progress} == {"-foo", "-bar"}
