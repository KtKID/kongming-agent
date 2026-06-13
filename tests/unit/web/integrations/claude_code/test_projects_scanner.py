"""projects_scanner.py 单测（v0.2 / claude-session-rename-archive-metadata-source）。

覆盖：

A. **原有语义回归**（registry-driven 形态）：
   1. registry 为空 → []
   2. registry 含 cwd 但 claude_home 不存在 → 节点保留，sessions=[]
   3. agent-*.jsonl 跳过
   4. 空 jsonl 跳过
   5. 损坏 jsonl → title="(无法解析)" / "(空会话)"
   6. 无 user 消息 → title="(空会话)"
   7. user 消息 title 截断 + 去换行
   8. message_count = jsonl 总行数（不过滤）
   9. sessions 按 mtime desc
   10. projects 按 max(session.mtime) desc
   11. cwd / display_name 来自 registry 真值
   12. progress_callback 收到逐 project 进度

B. **thread metadata 真源**（本次迁移新增）：
   13. ``test_title_from_metadata``: metadata.name 命中 → title 用之
   14. ``test_archived_from_metadata_filters_session``: meta.is_archived=True
       → session 不出现
   15. ``test_metadata_not_found_fallback_to_first_user_msg``: 索引为空 →
       走原 fallback 取首条 user message
   16. ``test_metadata_overrides_old_4kb_window_bug``: 关键复现——jsonl rename
       后写超 4KB 内容，metadata 仍然给出正确 title（旧 ``read_custom_title``
       从尾部 4KB 倒读，rename 行被推出窗口必然漏读）
   17. ``test_read_custom_title_and_read_archived_deleted``: 旧函数已彻底移除
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

from hosts.web.integrations.claude_code.projects_scanner import list_projects
from hosts.web.threads.metadata import ThreadMetadata


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _make_metadata(
    *,
    thread_id: str = "thread-aaaaaaaaaaaa",
    name: str = "thread",
    claude_thread_id: str = "",
    is_archived: bool = False,
) -> ThreadMetadata:
    return ThreadMetadata(
        id=thread_id,
        name=name,
        preset_id="",
        backend_kind="claude_code",
        claude_thread_id=claude_thread_id,
        cwd="/some/cwd",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
        is_archived=is_archived,
    )


# ---------------------------------------------------------------------------
# A. 原有语义回归
# ---------------------------------------------------------------------------


def test_empty_registry_returns_empty(tmp_path: Path) -> None:
    assert list_projects([], claude_home=tmp_path) == []


def test_registry_cwd_without_claude_dir_keeps_node(tmp_path: Path) -> None:
    out = list_projects(["/foo/bar"], claude_home=tmp_path)
    assert len(out) == 1
    assert out[0].cwd == "/foo/bar"
    assert out[0].sessions == []


def test_skip_agent_files(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo/bar"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "agent-abc.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}],
    )
    _write_jsonl(
        proj / "real.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "real"}}],
    )
    out = list_projects([cwd], claude_home=tmp_path)
    assert len(out) == 1
    sids = [s.claude_thread_id for s in out[0].sessions]
    assert "real" in sids
    assert not any(s.startswith("agent-") for s in sids)


def test_empty_jsonl_skipped(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    (proj / "empty.jsonl").write_text("", encoding="utf-8")
    _write_jsonl(
        proj / "valid.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "hi"}}],
    )
    out = list_projects([cwd], claude_home=tmp_path)
    assert len(out) == 1
    assert len(out[0].sessions) == 1
    assert out[0].sessions[0].claude_thread_id == "valid"


def test_corrupt_jsonl_title_unparsable(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    (proj / "bad.jsonl").write_text("garbage {{ not json\nmore garbage\n", encoding="utf-8")
    out = list_projects([cwd], claude_home=tmp_path)
    assert len(out) == 1
    assert len(out[0].sessions) == 1
    title = out[0].sessions[0].title
    assert title in {"(无法解析)", "(空会话)"}


def test_no_user_message_title_is_empty_session(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
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
    out = list_projects([cwd], claude_home=tmp_path)
    assert len(out) == 1
    assert out[0].sessions[0].title == "(空会话)"


def test_title_truncated_to_40_chars_no_newlines(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    long_text = "ab\ncd\n" + "x" * 100
    _write_jsonl(
        proj / "f.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": long_text}}],
    )
    out = list_projects([cwd], claude_home=tmp_path)
    title = out[0].sessions[0].title
    assert len(title) <= 40
    assert "\n" not in title


def test_message_count_is_total_lines(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
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
    out = list_projects([cwd], claude_home=tmp_path)
    assert out[0].sessions[0].message_count == 4


def test_sessions_sorted_by_mtime_desc(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
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
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    out = list_projects([cwd], claude_home=tmp_path)
    assert [s.claude_thread_id for s in out[0].sessions] == ["new", "old"]


def test_projects_sorted_by_max_session_mtime_desc(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd_old = "/old"
    cwd_new = "/new"
    p_old = tmp_path / "projects" / encode_cwd(cwd_old)
    p_new = tmp_path / "projects" / encode_cwd(cwd_new)
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
    out = list_projects([cwd_old, cwd_new], claude_home=tmp_path)
    cwds = [p.cwd for p in out]
    assert cwds.index(cwd_new) < cwds.index(cwd_old)


def test_cwd_and_display_name_from_registry(tmp_path: Path) -> None:
    """display_name 直接来自 registry cwd 的 basename，不再从 jsonl entry 反推。"""
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo/bar/baz"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "x.jsonl",
        [{"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}],
    )
    out = list_projects([cwd], claude_home=tmp_path)
    assert len(out) == 1
    p = out[0]
    assert p.cwd == cwd
    assert p.display_name == "baz"
    # name 字段是编码后的目录名（用于 UI 调试 / 与 ~/.claude/projects/<name>/ 对齐）
    assert p.name == encoded


def test_progress_callback_receives_project_scan_progress(tmp_path: Path) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd1 = "/foo"
    cwd2 = "/bar"
    p1 = tmp_path / "projects" / encode_cwd(cwd1)
    p2 = tmp_path / "projects" / encode_cwd(cwd2)
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
        [cwd1, cwd2],
        claude_home=tmp_path,
        progress_callback=lambda current, total, current_project: progress.append(
            (current, total, current_project)
        ),
    )

    assert len(out) == 2
    assert len(progress) == 2
    assert progress[-1][0] == 2
    assert progress[-1][1] == 2


# ---------------------------------------------------------------------------
# B. thread metadata 真源
# ---------------------------------------------------------------------------


def test_title_from_metadata(tmp_path: Path) -> None:
    """metadata.name 命中 → SessionSummary.title 取 metadata.name，
    不再去 jsonl 扫首条 user message。"""
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "abc.jsonl",
        [{"type": "user", "sessionId": "abc", "message": {"role": "user", "content": "原话题"}}],
    )
    index = {"abc": _make_metadata(name="自定义标题", claude_thread_id="abc")}
    out = list_projects([cwd], claude_home=tmp_path, thread_metadata_index=index)
    assert len(out) == 1
    assert len(out[0].sessions) == 1
    assert out[0].sessions[0].title == "自定义标题"
    # 即使被 metadata 覆盖也仍要正确算行数
    assert out[0].sessions[0].message_count == 1


def test_archived_from_metadata_filters_session(tmp_path: Path) -> None:
    """meta.is_archived=True → _build_session_summary 返回 None → session
    不出现在列表里（project 节点仍保留，sessions=[]）。"""
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "arc.jsonl",
        [{"type": "user", "sessionId": "arc", "message": {"role": "user", "content": "x"}}],
    )
    _write_jsonl(
        proj / "live.jsonl",
        [{"type": "user", "sessionId": "live", "message": {"role": "user", "content": "y"}}],
    )
    index = {
        "arc": _make_metadata(
            thread_id="thread-aaaaaaaaaaa0",
            name="已归档",
            claude_thread_id="arc",
            is_archived=True,
        ),
        "live": _make_metadata(
            thread_id="thread-aaaaaaaaaaa1",
            name="活跃",
            claude_thread_id="live",
            is_archived=False,
        ),
    }
    out = list_projects([cwd], claude_home=tmp_path, thread_metadata_index=index)
    assert len(out) == 1
    sids = [s.claude_thread_id for s in out[0].sessions]
    assert sids == ["live"]


def test_metadata_not_found_fallback_to_first_user_msg(tmp_path: Path) -> None:
    """索引为空 / 缺该 claude_thread_id → 走原 fallback：扫首条 user message。"""
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    _write_jsonl(
        proj / "orphan.jsonl",
        [
            {
                "type": "user",
                "sessionId": "orphan",
                "message": {"role": "user", "content": "首条问题"},
            }
        ],
    )
    # 显式空 dict
    out_empty = list_projects([cwd], claude_home=tmp_path, thread_metadata_index={})
    assert out_empty[0].sessions[0].title == "首条问题"

    # 默认 None
    out_none = list_projects([cwd], claude_home=tmp_path)
    assert out_none[0].sessions[0].title == "首条问题"


def test_metadata_overrides_old_4kb_window_bug(tmp_path: Path) -> None:
    """**关键复现**：rename 后又追加 >12KB 消息——旧 ``read_custom_title``
    只读末尾 4KB，必然把 ``custom-title`` 行推出窗口、漏读真值；新实现
    走 metadata.name 完全绕过 jsonl 尾部窗口扫描。

    本测试构造：jsonl 首行是 ``custom-title``，后接 12KB 大消息。
    metadata.name=``新名``。预期 title=``新名``。
    """
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    cwd = "/foo"
    encoded = encode_cwd(cwd)
    proj = tmp_path / "projects" / encoded
    proj.mkdir(parents=True)
    big = "x" * 12000
    entries = [
        {"type": "custom-title", "customTitle": "已被旧 4KB 窗口推出"},
        {
            "type": "user",
            "sessionId": "bigsess",
            "message": {"role": "user", "content": big},
        },
    ]
    _write_jsonl(proj / "bigsess.jsonl", entries)

    # 不带 metadata 索引 → 旧 fallback 路径会读到首条 user msg（不是 custom-title，
    # 因为新实现已经不解析 custom-title 事件）；title 是大段 x。
    out_no_meta = list_projects([cwd], claude_home=tmp_path)
    assert out_no_meta[0].sessions[0].title.startswith("x")  # fallback 拿到 user msg

    # 带 metadata 索引 → 直接用 metadata.name，无视 jsonl 内容大小
    index = {"bigsess": _make_metadata(name="新名", claude_thread_id="bigsess")}
    out_with_meta = list_projects([cwd], claude_home=tmp_path, thread_metadata_index=index)
    assert out_with_meta[0].sessions[0].title == "新名"


def test_read_custom_title_and_read_archived_deleted() -> None:
    """旧函数 ``read_custom_title`` / ``read_archived`` 已彻底移除。

    用 import + AST 双重检查：
    1. 从 module import 这俩函数应抛 ImportError（symbol 不存在）。
    2. 解析源文件 AST，确认顶层不存在同名 def。
    """
    import hosts.web.integrations.claude_code.projects_scanner as scanner_mod

    assert not hasattr(scanner_mod, "read_custom_title")
    assert not hasattr(scanner_mod, "read_archived")
    # __all__ 同步剔除
    assert "read_custom_title" not in scanner_mod.__all__
    assert "read_archived" not in scanner_mod.__all__

    src_path = Path(scanner_mod.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    top_level_funcs = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "read_custom_title" not in top_level_funcs
    assert "read_archived" not in top_level_funcs
