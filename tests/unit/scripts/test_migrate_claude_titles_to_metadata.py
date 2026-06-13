"""``scripts/migrate_claude_titles_to_metadata.py`` 单测。

覆盖 dev-checklist #7 的三分支 + 边界：

1. 默认名 → custom-title 覆盖
2. 人工已改名 → 跳过
3. 未绑定 thread → 统计 skipped_unbound
4. ``archived:true`` 事件 → 覆盖 is_archived=True
5. ``archived:false`` 事件 → 不修改（逻辑是"无脑覆盖 True"，false 不触发）
6. custom-title 后追加 >12KB 内容 → 反向扫仍能找到（核心价值：绕开 4KB bug）
7. dry-run 不写盘
8. ``agent-*.jsonl`` 不被扫描

用 ``tmp_path`` 构造 fake claude_home + kongming_home，完全隔离。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hosts.web.threads.metadata import (
    ThreadMetadata,
    read_thread_metadata,
    write_thread_metadata,
)


def _load_migrate_module() -> Any:
    """通过文件路径加载脚本模块（``scripts/`` 不是 importable package）。"""
    project_root = Path(__file__).resolve().parents[3]
    script_path = project_root / "scripts" / "migrate_claude_titles_to_metadata.py"
    spec = importlib.util.spec_from_file_location("_migrate_claude_titles", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_migrate_claude_titles"] = module
    spec.loader.exec_module(module)
    return module


migrate_module = _load_migrate_module()


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _make_meta(
    thread_id: str,
    *,
    name: str,
    cwd: str,
    claude_thread_id: str,
    is_archived: bool = False,
) -> ThreadMetadata:
    return ThreadMetadata.model_validate(
        {
            "id": thread_id,
            "name": name,
            "preset_id": "claude-sonnet-4",
            "backend_kind": "claude_code",
            "claude_thread_id": claude_thread_id,
            "cwd": cwd,
            "created_at": 1700000000.0,
            "updated_at": 1700000100.0,
            "message_count": 3,
            "is_archived": is_archived,
        }
    )


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _jsonl_path(claude_home: Path, project_name: str, claude_thread_id: str) -> Path:
    return claude_home / "projects" / project_name / f"{claude_thread_id}.jsonl"


def _user_msg(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def _custom_title_event(session_id: str, title: str) -> dict[str, Any]:
    return {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }


def _archived_event(session_id: str, archived: bool) -> dict[str, Any]:
    return {
        "type": "archived",
        "archived": archived,
        "sessionId": session_id,
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_default_name_overwritten_by_custom_title(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/some/proj/kongming-agent"
    sid = "11111111-1111-1111-1111-111111111111"

    # cwd basename == "kongming-agent" → 视作默认名
    meta = _make_meta(
        "thread-aaaaaaaaaaaa",
        name="kongming-agent",
        cwd=cwd,
        claude_thread_id=sid,
    )
    write_thread_metadata(kongming_home, meta)

    _write_jsonl(
        _jsonl_path(claude_home, "-some-proj-kongming-agent", sid),
        [
            _user_msg("hello"),
            _custom_title_event(sid, "好名字"),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.migrated_title == 1
    assert stats.skipped_already_named == 0
    refreshed = read_thread_metadata(kongming_home, "thread-aaaaaaaaaaaa")
    assert refreshed is not None
    assert refreshed.name == "好名字"
    assert refreshed.is_archived is False


def test_manual_name_not_overwritten(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/some/proj/kongming-agent"
    sid = "22222222-2222-2222-2222-222222222222"

    # name != basename(cwd) → 视作人工已改名
    meta = _make_meta(
        "thread-bbbbbbbbbbbb",
        name="我手动改的名字",
        cwd=cwd,
        claude_thread_id=sid,
    )
    write_thread_metadata(kongming_home, meta)

    _write_jsonl(
        _jsonl_path(claude_home, "-some-proj-kongming-agent", sid),
        [
            _user_msg("hello"),
            _custom_title_event(sid, "应该被忽略的"),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.migrated_title == 0
    assert stats.skipped_already_named == 1
    refreshed = read_thread_metadata(kongming_home, "thread-bbbbbbbbbbbb")
    assert refreshed is not None
    assert refreshed.name == "我手动改的名字"


def test_unbound_session_skipped(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    # 故意不写任何 metadata
    kongming_home.mkdir(parents=True, exist_ok=True)

    sid = "33333333-3333-3333-3333-333333333333"
    _write_jsonl(
        _jsonl_path(claude_home, "-orphan-project", sid),
        [
            _user_msg("hello"),
            _custom_title_event(sid, "孤儿"),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.scanned_jsonl == 1
    assert stats.skipped_unbound == 1
    assert stats.migrated_title == 0


def test_archived_event_applies(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/proj/foo"
    sid = "44444444-4444-4444-4444-444444444444"

    meta = _make_meta(
        "thread-cccccccccccc",
        name="foo",  # basename == "foo"，但本测试不关心 title
        cwd=cwd,
        claude_thread_id=sid,
        is_archived=False,
    )
    write_thread_metadata(kongming_home, meta)

    _write_jsonl(
        _jsonl_path(claude_home, "-proj-foo", sid),
        [
            _user_msg("hello"),
            _archived_event(sid, True),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.migrated_archived == 1
    refreshed = read_thread_metadata(kongming_home, "thread-cccccccccccc")
    assert refreshed is not None
    assert refreshed.is_archived is True


def test_archived_false_event_no_effect(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/proj/bar"
    sid = "55555555-5555-5555-5555-555555555555"

    meta = _make_meta(
        "thread-dddddddddddd",
        name="bar",
        cwd=cwd,
        claude_thread_id=sid,
        is_archived=False,
    )
    write_thread_metadata(kongming_home, meta)

    _write_jsonl(
        _jsonl_path(claude_home, "-proj-bar", sid),
        [
            _user_msg("hello"),
            _archived_event(sid, False),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.migrated_archived == 0
    refreshed = read_thread_metadata(kongming_home, "thread-dddddddddddd")
    assert refreshed is not None
    assert refreshed.is_archived is False


def test_custom_title_pushed_past_4kb_still_migrated(tmp_path: Path) -> None:
    """custom-title 后追加 >12KB 内容，反向扫整文件仍能找到——这是脚本的
    核心价值：绕开旧 4KB 尾部窗口 bug。"""
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/proj/big"
    sid = "66666666-6666-6666-6666-666666666666"

    meta = _make_meta(
        "thread-eeeeeeeeeeee",
        name="big",
        cwd=cwd,
        claude_thread_id=sid,
    )
    write_thread_metadata(kongming_home, meta)

    big_blob = "X" * (12 * 1024 + 500)  # >12KB
    _write_jsonl(
        _jsonl_path(claude_home, "-proj-big", sid),
        [
            _user_msg("hello"),
            _custom_title_event(sid, "深埋的标题"),
            # 在 custom-title 后追加一条 >12KB 的 user message
            _user_msg(big_blob),
            _user_msg("trailing short"),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.migrated_title == 1
    refreshed = read_thread_metadata(kongming_home, "thread-eeeeeeeeeeee")
    assert refreshed is not None
    assert refreshed.name == "深埋的标题"


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    cwd = "/proj/dry"
    sid = "77777777-7777-7777-7777-777777777777"

    meta = _make_meta(
        "thread-ffffffffffff",
        name="dry",
        cwd=cwd,
        claude_thread_id=sid,
    )
    write_thread_metadata(kongming_home, meta)

    _write_jsonl(
        _jsonl_path(claude_home, "-proj-dry", sid),
        [
            _user_msg("hello"),
            _custom_title_event(sid, "新名"),
            _archived_event(sid, True),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=True,
    )

    # 统计正确
    assert stats.migrated_title == 1
    assert stats.migrated_archived == 1
    # 磁盘没动
    refreshed = read_thread_metadata(kongming_home, "thread-ffffffffffff")
    assert refreshed is not None
    assert refreshed.name == "dry"
    assert refreshed.is_archived is False


def test_agent_subagent_jsonl_skipped(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    kongming_home.mkdir(parents=True, exist_ok=True)

    # agent-foo.jsonl 不应该被扫
    agent_jsonl = claude_home / "projects" / "-proj-x" / "agent-foo.jsonl"
    _write_jsonl(
        agent_jsonl,
        [
            _user_msg("subagent hi"),
            _custom_title_event("agent-foo", "不该被扫到"),
        ],
    )

    stats = migrate_module.migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=False,
    )

    assert stats.scanned_jsonl == 0
    assert stats.skipped_unbound == 0
    assert stats.has_custom_title == 0


# ---------------------------------------------------------------------------
# 报告 + CLI 入口辅助测试（保证 render_report / main 不挂）
# ---------------------------------------------------------------------------


def test_render_report_includes_dry_run_note(tmp_path: Path) -> None:
    stats = migrate_module.MigrationStats(
        scanned_jsonl=5,
        skipped_unbound=1,
        has_custom_title=2,
        migrated_title=1,
        skipped_already_named=1,
        migrated_archived=1,
        no_event=1,
    )
    report = migrate_module.render_report(stats, dry_run=True)
    assert "扫描 jsonl 总数：5" in report
    assert "DRY RUN" in report

    report2 = migrate_module.render_report(stats, dry_run=False)
    assert "DRY RUN" not in report2


def test_main_smoke_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude_home = tmp_path / "claude"
    kongming_home = tmp_path / "kongming"
    # 空 home：不会爆，只产出 0 报告
    kongming_home.mkdir(parents=True, exist_ok=True)
    (claude_home / "projects").mkdir(parents=True, exist_ok=True)

    rc = migrate_module.main(
        [
            "--dry-run",
            "--claude-home",
            str(claude_home),
            "--kongming-home",
            str(kongming_home),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "扫描 jsonl 总数：0" in captured.out
    assert "DRY RUN" in captured.out


# ---------------------------------------------------------------------------
# 反向扫描器单测
# ---------------------------------------------------------------------------


def test_iter_lines_reversed_basic(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    lines = list(migrate_module._iter_lines_reversed(path))
    assert lines == ["c", "b", "a"]


def test_iter_lines_reversed_handles_large_lines(tmp_path: Path) -> None:
    """单行 >块大小 时也能正确切分。"""
    path = tmp_path / "y.jsonl"
    big = "Y" * (200 * 1024)  # 200KB 单行
    path.write_text(f"first\n{big}\nlast\n", encoding="utf-8")
    lines = list(migrate_module._iter_lines_reversed(path))
    assert lines[0] == "last"
    assert lines[1] == big
    assert lines[2] == "first"


def test_iter_lines_reversed_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert list(migrate_module._iter_lines_reversed(path)) == []


def test_find_custom_title_returns_last_when_multiple(tmp_path: Path) -> None:
    """jsonl 里多条 custom-title，应该取最后一条（用户多次 rename）。"""
    path = tmp_path / "multi.jsonl"
    _write_jsonl(
        path,
        [
            _custom_title_event("s", "第一次"),
            _user_msg("middle"),
            _custom_title_event("s", "最终"),
        ],
    )
    title, archived = migrate_module.find_custom_title_and_archived(path)
    assert title == "最终"
    assert archived is False


def test_find_custom_title_skips_garbage_lines(tmp_path: Path) -> None:
    """jsonl 里夹带非 JSON 行不应中断扫描。"""
    path = tmp_path / "noisy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not-json-line\n")
        fh.write(json.dumps(_custom_title_event("s", "valid")) + "\n")
        fh.write("\n")
        fh.write("{bad-json}\n")
    title, _ = migrate_module.find_custom_title_and_archived(path)
    assert title == "valid"


def test_iter_jsonl_files_skips_agent_prefix(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    project = claude_home / "projects" / "-p"
    project.mkdir(parents=True)
    (project / "ok.jsonl").write_text("{}\n", encoding="utf-8")
    (project / "agent-sub.jsonl").write_text("{}\n", encoding="utf-8")
    paths = sorted(p.name for p in migrate_module.iter_jsonl_files(claude_home))
    assert paths == ["ok.jsonl"]


def test_iter_jsonl_files_missing_root(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    # 不创建 projects/，应返回空
    assert list(migrate_module.iter_jsonl_files(claude_home)) == []
