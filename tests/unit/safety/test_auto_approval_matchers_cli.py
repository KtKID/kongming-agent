"""共享自动审批匹配器对 CLI/原生工具名的覆盖测试。"""

from __future__ import annotations

from safety.auto_approval.matchers import matches


# 验证 CLI 的 ``run_shell`` 工具名可以复用 Bash 命令规则。
def test_run_shell_matches_bash_rules() -> None:
    assert matches(
        {"kind": "bash_cmd_first", "command": "rm"},
        "run_shell",
        {"command": "rm -rf tmp"},
    )


# 验证 CLI 的 ``write_file`` 工具名可以复用编辑路径规则。
def test_write_file_matches_edit_path_rules() -> None:
    assert matches(
        {"kind": "edit_path_glob", "globs": ["*/.ssh/*"]},
        "write_file",
        {"path": "/Users/me/.ssh/config"},
    )


# 验证 CLI 的 ``edit_file`` 工具名可以复用编辑路径规则。
def test_edit_file_matches_edit_path_rules() -> None:
    assert matches(
        {"kind": "edit_path_glob", "globs": ["*/.env"]},
        "edit_file",
        {"path": "/work/app/.env"},
    )


# 验证 Web 既有 ``Edit`` / ``Write`` 工具名仍可匹配编辑路径规则。
def test_web_edit_names_still_match_edit_path_rules() -> None:
    rule = {"kind": "edit_path_glob", "globs": ["*/.ssh/*"]}

    assert matches(rule, "Edit", {"path": "/Users/me/.ssh/config"})
    assert matches(rule, "Write", {"path": "/Users/me/.ssh/config"})
