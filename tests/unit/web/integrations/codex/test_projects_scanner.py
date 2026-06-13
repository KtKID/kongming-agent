"""projects_scanner 边界用例测试（T4 #14 / #15 / #22）。

覆盖：
- #14: session_index.jsonl 缺失时兜底扫盘
- #15: 索引延迟时扫盘补齐（Union 合并）
- #22: 按 cwd 正确分组 + codex_home 不存在
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from hosts.web.integrations.codex.projects_scanner import list_codex_projects

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_UUID_A = str(uuid.UUID("019c429a-b3e2-7f00-a1d2-e4f5a6b7c8d9"))
_UUID_B = str(uuid.UUID("019c429b-c4f3-7f01-b2e3-f5a6b7c8d9e0"))
_UUID_C = str(uuid.UUID("019c429c-d5a4-7f02-c3f4-a6b7c8d9e0f1"))


def _rollout_filename(session_id: str) -> str:
    """生成 rollout-<ISO>-<UUID>.jsonl 文件名。"""
    return f"rollout-2026-05-03T14-00-00-{session_id}.jsonl"


def _write_rollout(
    path: Path,
    session_id: str,
    cwd: str,
    extra_lines: int = 5,
) -> None:
    """写一个最小 rollout 文件（session_meta 首行 + N 条消息行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": "2026-05-03T14:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": cwd,
                    "originator": "codex_exec",
                    "cli_version": "0.128.0",
                    "source": "exec",
                    "model_provider": "openai",
                },
            }
        )
    ]
    for i in range(extra_lines):
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-05-03T14:00:0{i + 1}.000Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "text": f"msg {i}"},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def _write_index(path: Path, entries: list[dict[str, str]]) -> None:
    """写 session_index.jsonl。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# #14: session_index.jsonl 缺失 → 纯扫盘兜底
# ---------------------------------------------------------------------------


class TestNoIndex:
    """用例 14: session_index.jsonl 缺失时兜底扫盘。"""

    def test_no_index_file(self, tmp_path: Path) -> None:
        # 只有 rollout，没有 session_index.jsonl
        rollout = tmp_path / "sessions" / "2026" / "05" / "03" / _rollout_filename(_UUID_A)
        cwd_val = str(tmp_path / "my-project")
        _write_rollout(rollout, _UUID_A, cwd_val, extra_lines=3)

        projects = list_codex_projects([cwd_val], codex_home=tmp_path)

        assert len(projects) == 1
        proj = projects[0]
        assert proj.display_name == "my-project"
        assert len(proj.sessions) == 1

        s = proj.sessions[0]
        assert s.session_id == _UUID_A
        # 无索引 → title 应为占位符
        assert s.title == "(无标题)"
        assert s.message_count == 4  # 1 meta + 3 extra
        assert s.cli_version == "0.128.0"
        assert s.provider == "codex"


# ---------------------------------------------------------------------------
# #15: 索引延迟 → 扫盘补齐（Union 合并）
# ---------------------------------------------------------------------------


class TestIndexLag:
    """用例 15: 索引只含 A，rollout 含 A+B → 结果含两者。"""

    def test_index_missing_recent_session(self, tmp_path: Path) -> None:
        cwd_val = str(tmp_path / "repo")

        # rollout A + B
        dir_day = tmp_path / "sessions" / "2026" / "05" / "03"
        _write_rollout(dir_day / _rollout_filename(_UUID_A), _UUID_A, cwd_val)
        _write_rollout(dir_day / _rollout_filename(_UUID_B), _UUID_B, cwd_val)

        # 索引只含 A（带 thread_name）
        _write_index(
            tmp_path / "session_index.jsonl",
            [
                {
                    "id": _UUID_A,
                    "thread_name": "Fix login bug",
                    "updated_at": "2026-05-03T14:30:00Z",
                }
            ],
        )

        projects = list_codex_projects([cwd_val], codex_home=tmp_path)

        assert len(projects) == 1
        proj = projects[0]
        # 同 cwd → 一个 project 下 2 sessions
        assert len(proj.sessions) == 2

        by_id = {s.session_id: s for s in proj.sessions}
        assert _UUID_A in by_id
        assert _UUID_B in by_id

        # A 的 title 应来自 index 的 thread_name
        assert by_id[_UUID_A].title == "Fix login bug"
        # B 无索引 → 占位符
        assert by_id[_UUID_B].title == "(无标题)"


# ---------------------------------------------------------------------------
# #22 延伸: 按 cwd 正确分组
# ---------------------------------------------------------------------------


class TestCwdGrouping:
    """用例 22: 按 cwd 正确分组。"""

    def test_same_cwd_grouped(self, tmp_path: Path) -> None:
        cwd_val = str(tmp_path / "shared-repo")
        dir_day = tmp_path / "sessions" / "2026" / "05" / "03"
        _write_rollout(dir_day / _rollout_filename(_UUID_A), _UUID_A, cwd_val)
        _write_rollout(dir_day / _rollout_filename(_UUID_B), _UUID_B, cwd_val)

        projects = list_codex_projects([cwd_val], codex_home=tmp_path)

        assert len(projects) == 1
        assert len(projects[0].sessions) == 2
        assert projects[0].display_name == "shared-repo"

    def test_different_cwd_separate(self, tmp_path: Path) -> None:
        dir_day = tmp_path / "sessions" / "2026" / "05" / "03"
        _write_rollout(
            dir_day / _rollout_filename(_UUID_A),
            _UUID_A,
            str(tmp_path / "repo-alpha"),
        )
        _write_rollout(
            dir_day / _rollout_filename(_UUID_B),
            _UUID_B,
            str(tmp_path / "repo-beta"),
        )

        projects = list_codex_projects(
            [str(tmp_path / "repo-alpha"), str(tmp_path / "repo-beta")],
            codex_home=tmp_path,
        )

        assert len(projects) == 2
        names = {p.display_name for p in projects}
        assert names == {"repo-alpha", "repo-beta"}
        for p in projects:
            assert len(p.sessions) == 1


# ---------------------------------------------------------------------------
# codex_home 不存在 → 空列表
# ---------------------------------------------------------------------------


class TestEmptyHome:
    def test_codex_home_not_exist(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        # 空 registry → 即使 codex_home 不存在也不会返回任何登记节点。
        projects = list_codex_projects([], codex_home=missing)
        assert projects == []
