"""验证 v0.7 DangerGuard 的写死危险集和自提权检测。

测试覆盖原 HardBlock / destructive 规则、新增仓库与指令保护，以及 per-cwd 模式和
thread permissions allow 扩权。每个用例直接调用 ``match``，确保 guard 自身
不依赖审批模式、permissions 本子解析或 cron 链路。
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.contracts import ApprovalRequest, ToolExecutionScope
from safety.guards.danger import DangerAction, DangerGuard, DangerRule, DangerTargetKind


def _request(
    *,
    tool_name: str,
    arguments: dict[str, object],
    cwd: Path,
    approval_mode: str = "user",
) -> ApprovalRequest:
    """构造带 cwd 与模式审计信息的审批请求。"""
    canonical_cwd = cwd.resolve().as_posix()
    prepared_arguments = (
        {**arguments, "cwd": canonical_cwd} if tool_name == "run_shell" else arguments
    )
    return ApprovalRequest(
        run_id="run-danger",
        session_id="session-danger",
        turn=1,
        call_id="call-danger",
        tool_name=tool_name,
        arguments=prepared_arguments,
        execution_scope=ToolExecutionScope(cwd=canonical_cwd if tool_name == "run_shell" else None),
        metadata={"cwd": canonical_cwd, "approval_mode": approval_mode},
    )


def _guard(tmp_path: Path) -> DangerGuard:
    """创建绑定隔离 kongming_home 的 DangerGuard。"""
    return DangerGuard(kongming_home=tmp_path / ".kongming")


def _explicit_shell_write_commands(target: str) -> tuple[str, ...]:
    """构造 Path.write_text、Out-File 和 dd 三种显式文件写入命令。"""
    return (
        (f"python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""),
        f"'x' | Out-File -FilePath '{target}'",
        f"dd if=source.txt of='{target}'",
    )


def _semantic_python_write_commands(target: str) -> tuple[str, ...]:
    """构造 keyword mode 与 Path 变量分离两种 Python 语义写入。"""
    return (
        f"python -c \"open('{target}', mode='w').write('x')\"",
        (f"python -c \"from pathlib import Path; p = Path('{target}'); p.write_text('x')\""),
    )


def _legacy_config_text(filename: str, expression: str) -> str:
    """按旧配置扩展名生成包含一条 allow 规则的完整文本。"""
    if filename == "config.toml":
        return (
            "[safety]\n"
            f'approval_rules = [{{id = "rule", behavior = "allow", expression = "{expression}"}}]\n'
        )
    if filename == "config.json":
        return json.dumps(
            {
                "safety": {
                    "approval_rules": [
                        {"id": "rule", "behavior": "allow", "expression": expression}
                    ]
                }
            }
        )
    return (
        "safety:\n  approval_rules:\n"
        f"    - id: rule\n      behavior: allow\n      expression: {expression}\n"
    )


def _assert_rule(rule: DangerRule | None, expected_name: str) -> DangerRule:
    """断言命中规则名并返回非空规则。"""
    assert rule is not None
    assert rule.name == expected_name
    assert rule.reason
    assert rule.matcher
    assert rule.target_value
    return rule


@pytest.mark.unit
@pytest.mark.parametrize("approval_mode", ["user", "llm", "full_trust"])
def test_rm_rf_root_matches_in_every_approval_mode(
    tmp_path: Path,
    approval_mode: str,
) -> None:
    """根目录递归删除在三种模式下都命中写死危险层。"""
    rule = _guard(tmp_path).match(
        _request(
            tool_name="run_shell",
            arguments={"command": "rm -rf /"},
            cwd=tmp_path,
            approval_mode=approval_mode,
        )
    )
    matched = _assert_rule(rule, "host-root-delete")
    assert matched.target_kind is DangerTargetKind.COMMAND


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected_name"),
    [
        ("rm -rf ./build", "rm-recursive"),
        ("shred secret.key", "shred"),
        (":(){ :|:& };:", "fork-bomb"),
    ],
)
def test_existing_hard_and_destructive_rules_are_merged(
    tmp_path: Path,
    command: str,
    expected_name: str,
) -> None:
    """原 HardBlock 与 destructive 默认规则从同一入口返回。"""
    rule = _guard(tmp_path).match(
        _request(tool_name="run_shell", arguments={"command": command}, cwd=tmp_path)
    )
    _assert_rule(rule, expected_name)


@pytest.mark.unit
def test_sensitive_secret_read_reuses_hard_block_path_rule(tmp_path: Path) -> None:
    """读取 SSH 凭据继续命中原敏感路径基线。"""
    target = Path.home() / ".ssh" / "id_ed25519"
    rule = _guard(tmp_path).match(
        _request(tool_name="read_file", arguments={"path": str(target)}, cwd=tmp_path)
    )
    matched = _assert_rule(rule, "ssh-material")
    assert matched.target_kind is DangerTargetKind.PATH


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_git_internal_file_write_matches(tmp_path: Path, tool_name: str) -> None:
    """直接写入或编辑 ``.git`` 内部文件命中新规则。"""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    rule = _guard(tmp_path).match(
        _request(
            tool_name=tool_name,
            arguments={"path": "repo/.git/config", "content": "x"},
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "git-internal")


@pytest.mark.unit
def test_project_relative_git_rule_resolves_nested_cwd(tmp_path: Path) -> None:
    """嵌套 cwd 访问 ``../.git`` 仍由项目相对规则硬阻断。"""
    repo = tmp_path / "repo"
    nested = repo / "src" / "nested"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    rule = _guard(tmp_path).match(
        _request(
            tool_name="edit_file",
            arguments={"path": "../../.git/HEAD", "old_string": "a", "new_string": "b"},
            cwd=nested,
        )
    )
    _assert_rule(rule, "git-internal")


@pytest.mark.unit
def test_shell_git_internal_write_matches_only_mutating_segment(tmp_path: Path) -> None:
    """shell 只在实际修改 ``.git`` 的命令段命中。"""
    guard = _guard(tmp_path)
    write_rule = guard.match(
        _request(
            tool_name="run_shell",
            arguments={"command": "cd repo && rm -rf .git/refs"},
            cwd=tmp_path,
        )
    )
    _assert_rule(write_rule, "git-dir-destroy")

    read_rule = guard.match(
        _request(
            tool_name="run_shell",
            arguments={"command": "cat .git/config && echo done"},
            cwd=tmp_path,
        )
    )
    assert read_rule is None


@pytest.mark.unit
@pytest.mark.parametrize("command", ["rm -rf .git", "mv .git .git.backup"])
def test_shell_git_directory_destroy_is_hard_block(tmp_path: Path, command: str) -> None:
    """rm/mv 整个 .git 目录命中不可绕过的项目完整性硬防线。"""
    rule = _guard(tmp_path).match(
        _request(tool_name="run_shell", arguments={"command": command}, cwd=tmp_path)
    )
    matched = _assert_rule(rule, "git-dir-destroy")
    assert matched.action is DangerAction.BLOCK


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('.git/config','w').write('x')\"",
        'powershell -Command "Set-Content -Path .git/config -Value x"',
    ],
)
def test_interpreter_shell_git_internal_write_matches(
    tmp_path: Path,
    command: str,
) -> None:
    """Python 和 PowerShell 间接写入 .git 仍命中仓库保护。"""
    rule = _guard(tmp_path).match(
        _request(tool_name="run_shell", arguments={"command": command}, cwd=tmp_path)
    )
    _assert_rule(rule, "git-internal")


@pytest.mark.unit
def test_claude_instruction_write_matches(tmp_path: Path) -> None:
    """任意目录中的 CLAUDE.md 写入命中 agent 指令保护。"""
    rule = _guard(tmp_path).match(
        _request(
            tool_name="write_file",
            arguments={"file_path": "nested/CLAUDE.md", "content": "instructions"},
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "agent-instruction-write")
    assert matched.action is DangerAction.ELEVATED


@pytest.mark.unit
@pytest.mark.parametrize("command", ["rm CLAUDE.md", "mv CLAUDE.md CLAUDE.md.bak", "> CLAUDE.md"])
def test_shell_instruction_destroy_is_hard_block(tmp_path: Path, command: str) -> None:
    """删除或清空 CLAUDE.md 进入 HardBlock，正常编辑保持 elevated。"""
    rule = _guard(tmp_path).match(
        _request(tool_name="run_shell", arguments={"command": command}, cwd=tmp_path)
    )
    matched = _assert_rule(rule, "agent-instruction-destroy")
    assert matched.action is DangerAction.BLOCK


@pytest.mark.unit
def test_shell_instruction_edit_is_elevated(tmp_path: Path) -> None:
    """sed 原地编辑 CLAUDE.md 保留用户确认。"""
    rule = _guard(tmp_path).match(
        _request(
            tool_name="run_shell",
            arguments={"command": "sed -i '' 's/old/new/' CLAUDE.md"},
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "agent-instruction-write")
    assert matched.action is DangerAction.ELEVATED


@pytest.mark.unit
def test_kongming_prompts_write_matches(tmp_path: Path) -> None:
    """写入运行时 prompts 目录命中 agent 指令保护。"""
    home = tmp_path / ".kongming"
    guard = DangerGuard(kongming_home=home)
    rule = guard.match(
        _request(
            tool_name="edit_file",
            arguments={"path": str(home / "prompts" / "AGENT.md"), "new_string": "new"},
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "agent-instruction-write")


@pytest.mark.unit
def test_setting_yaml_llm_configuration_requires_elevated_review(tmp_path: Path) -> None:
    """写 setting.yaml 的 LLM 复核器配置保留 elevated 人审。"""
    home = tmp_path / ".kongming"
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="write_file",
            arguments={
                "path": str(home / "setting.yaml"),
                "content": "safety:\n  approval:\n    llm:\n      model: review-model\n",
            },
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "self-configuration-write")
    assert matched.target_kind is DangerTargetKind.PATH
    assert matched.action is DangerAction.ELEVATED


@pytest.mark.unit
def test_setting_yaml_local_edit_requires_elevated_review(tmp_path: Path) -> None:
    """局部编辑 LLM 复核器配置仍保留 elevated 人审。"""
    home = tmp_path / ".kongming"
    target = home / "setting.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        'safety:\n  approval:\n    llm:\n      model: "old-model" # keep comment\n',
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": '"old-model" # keep comment',
                "new_string": '"new-model" # keep comment',
            },
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "self-configuration-write")
    assert matched.action is DangerAction.ELEVATED


@pytest.mark.unit
def test_setting_yaml_comment_edit_remains_elevated(tmp_path: Path) -> None:
    """.kongming 下的配置编辑保持 elevated 人审。"""
    home = tmp_path / ".kongming"
    target = home / "setting.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        'safety:\n  approval:\n    llm:\n      model: "review-model" # old comment\n',
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": "# old comment",
                "new_string": "# new comment",
            },
            cwd=tmp_path,
        )
    )
    assert rule is not None
    assert rule.action is DangerAction.ELEVATED


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('setting.yaml','w').write('safety: approval: {}')\"",
        ("powershell -Command \"Set-Content -Path setting.yaml -Value 'safety: approval: {}'\""),
    ],
)
def test_interpreter_shell_approval_configuration_write_matches_self_escalation(
    tmp_path: Path,
    command: str,
) -> None:
    """Python 和 PowerShell 间接写审批复核配置仍命中自提权。"""
    rule = _guard(tmp_path).match(
        _request(tool_name="run_shell", arguments={"command": command}, cwd=tmp_path)
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
def test_explicit_shell_write_forms_cover_git_setting_and_thread_permissions(
    tmp_path: Path,
) -> None:
    """Path.write_text、Out-File、dd 写三类保护目标均命中 danger。"""
    home = tmp_path / ".kongming"
    guard = DangerGuard(kongming_home=home)
    targets = (
        (".git/config", "git-internal"),
        ("setting.yaml", "safety-policy-self-escalation"),
        (
            (home / "safety" / "thread_permissions" / "abc.json").as_posix(),
            "safety-policy-self-escalation",
        ),
    )
    for target, expected_name in targets:
        for command in _explicit_shell_write_commands(target):
            rule = guard.match(
                _request(
                    tool_name="run_shell",
                    arguments={"command": command},
                    cwd=tmp_path,
                )
            )
            _assert_rule(rule, expected_name)


@pytest.mark.unit
def test_python_semantic_write_forms_cover_git_setting_and_thread_permissions(
    tmp_path: Path,
) -> None:
    """keyword mode 与 Path 变量写入三类保护目标均命中 danger。"""
    home = tmp_path / ".kongming"
    guard = DangerGuard(kongming_home=home)
    targets = (
        (".git/config", "git-internal"),
        ("setting.yaml", "safety-policy-self-escalation"),
        (
            (home / "safety" / "thread_permissions" / "abc.json").as_posix(),
            "safety-policy-self-escalation",
        ),
    )
    for target, expected_name in targets:
        for command in _semantic_python_write_commands(target):
            rule = guard.match(
                _request(
                    tool_name="run_shell",
                    arguments={"command": command},
                    cwd=tmp_path,
                )
            )
            _assert_rule(rule, expected_name)


@pytest.mark.unit
def test_thread_permissions_allow_expansion_matches_self_escalation(tmp_path: Path) -> None:
    """新增 thread allow 表达式命中安全策略自提权。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"allow": ["read_file"], "deny": []}),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="write_file",
            arguments={
                "path": str(target),
                "content": json.dumps({"allow": ["read_file", "run_shell(git:*)"], "deny": []}),
            },
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "safety-policy-self-escalation")
    assert matched.matcher == "thread_permissions:allow-expansion"


@pytest.mark.unit
def test_thread_permissions_edit_allow_expansion_matches_self_escalation(tmp_path: Path) -> None:
    """局部替换现有 allow 数组时按替换后的完整本子识别扩权。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"revision": 1, "allow": ["read_file"], "deny": []},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": '"allow":["read_file"]',
                "new_string": '"allow":["read_file","write_file"]',
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
def test_thread_permissions_tail_comma_edit_expansion_matches(tmp_path: Path) -> None:
    """局部替换带逗号的 allow 条目时仍按完整 JSON 识别新增权限。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"revision": 1, "allow": ["read_file", "list_dir"], "deny": []},
            indent=2,
        ),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": '"read_file",',
                "new_string": '"read_file",\n    "write_file",',
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
def test_thread_permissions_allow_shrink_remains_elevated(tmp_path: Path) -> None:
    """thread permissions 的收紧也属于自我配置编辑。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"revision": 1, "allow": ["read_file", "write_file"], "deny": []},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": '"allow":["read_file","write_file"]',
                "new_string": '"allow":["read_file"]',
            },
            cwd=tmp_path,
        )
    )
    assert rule is not None
    assert rule.action is DangerAction.ELEVATED


@pytest.mark.unit
def test_thread_permissions_replace_all_reaches_later_allow_occurrence(tmp_path: Path) -> None:
    """replace_all 先改无关 deny 后继续修改 allow，完整模拟识别后处扩权。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"revision": 1, "deny": ["read_file"], "allow": ["read_file"]},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": "read_file",
                "new_string": "write_file",
                "replace_all": True,
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_string", "new_string"),
    [
        ('"deny":[]', '"deny":["run_shell(curl:*)"]'),
        ('"revision":1', '"revision":2'),
    ],
)
def test_thread_permissions_restrictive_or_neutral_edit_remains_elevated(
    tmp_path: Path,
    old_string: str,
    new_string: str,
) -> None:
    """增加 deny 或更新 revision 也保留自我配置的人审门槛。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"revision": 1, "allow": ["read_file"], "deny": []},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": old_string,
                "new_string": new_string,
            },
            cwd=tmp_path,
        )
    )
    assert rule is not None
    assert rule.action is DangerAction.ELEVATED


@pytest.mark.unit
def test_thread_permissions_restrictive_write_remains_elevated(tmp_path: Path) -> None:
    """完整本子写入保持 elevated，防止完全信任绕过自我配置确认。"""
    home = tmp_path / ".kongming"
    target = home / "safety" / "thread_permissions" / "abc.json"
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="write_file",
            arguments={
                "path": str(target),
                "content": json.dumps({"allow": [], "deny": ["run_shell(curl:*)"]}),
            },
            cwd=tmp_path,
        )
    )
    assert rule is not None
    assert rule.action is DangerAction.ELEVATED


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (
            "config.yaml",
            "safety:\n  approval_rules:\n"
            "    - id: root-write\n      behavior: allow\n      expression: write_file\n",
        ),
        (
            "config.yml",
            "safety:\n  approval_rules:\n"
            "    - id: root-write\n      behavior: allow\n      expression: write_file\n",
        ),
        (
            "config.json",
            json.dumps(
                {
                    "safety": {
                        "approval_rules": [
                            {
                                "id": "root-write",
                                "behavior": "allow",
                                "expression": "write_file",
                            }
                        ]
                    }
                }
            ),
        ),
        (
            "config.toml",
            "[safety]\n"
            'approval_rules = [{id = "root-write", behavior = "allow", '
            'expression = "write_file"}]\n',
        ),
    ],
)
def test_legacy_kongming_config_root_allow_matches_self_escalation(
    tmp_path: Path,
    filename: str,
    content: str,
) -> None:
    """旧项目 config 四种格式的根级 allow 保持原自提权基线。"""
    rule = _guard(tmp_path).match(
        _request(
            tool_name="write_file",
            arguments={"path": str(tmp_path / ".kongming" / filename), "content": content},
            cwd=tmp_path,
        )
    )
    matched = _assert_rule(rule, "safety-policy-self-escalation")
    assert matched.matcher == "setting.yaml:approval_rules.allow"


@pytest.mark.unit
@pytest.mark.parametrize(
    "filename",
    ["config.yaml", "config.yml", "config.json", "config.toml"],
)
def test_legacy_kongming_config_edit_to_root_allow_matches_self_escalation(
    tmp_path: Path,
    filename: str,
) -> None:
    """旧配置局部把 read_file allow 改成 write_file 时按完整文件识别扩权。"""
    target = tmp_path / ".kongming" / filename
    target.parent.mkdir(parents=True)
    target.write_text(_legacy_config_text(filename, "read_file"), encoding="utf-8")
    rule = _guard(tmp_path).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": "read_file",
                "new_string": "write_file",
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
def test_setting_yaml_replace_all_remains_elevated(tmp_path: Path) -> None:
    """replace_all 编辑 LLM 复核器配置保持 elevated。"""
    home = tmp_path / ".kongming"
    target = home / "setting.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# documented model: old-model\nsafety:\n  approval:\n    llm:\n      model: old-model\n",
        encoding="utf-8",
    )
    rule = DangerGuard(kongming_home=home).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": "old-model",
                "new_string": "new-model",
                "replace_all": True,
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "self-configuration-write")


@pytest.mark.unit
def test_legacy_config_replace_all_reaches_later_allow_rule(tmp_path: Path) -> None:
    """replace_all 先改 deny 后继续把旧 allow 表达式提升为 write_file。"""
    target = tmp_path / ".kongming" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "safety:\n  approval_rules:\n"
        "    - id: deny-read\n      behavior: deny\n      expression: read_file\n"
        "    - id: allow-read\n      behavior: allow\n      expression: read_file\n",
        encoding="utf-8",
    )
    rule = _guard(tmp_path).match(
        _request(
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_string": "read_file",
                "new_string": "write_file",
                "replace_all": True,
            },
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "safety-policy-self-escalation")


@pytest.mark.unit
def test_invalid_write_path_fails_closed_as_danger(tmp_path: Path) -> None:
    """无法 canonicalize 的写入路径转为危险命中，决策链保持可用。"""
    rule = _guard(tmp_path).match(
        _request(
            tool_name="write_file",
            arguments={"path": "invalid\x00path", "content": "x"},
            cwd=tmp_path,
        )
    )
    _assert_rule(rule, "path-resolve-failed")


@pytest.mark.unit
def test_safe_write_and_shell_return_none(tmp_path: Path) -> None:
    """普通源码写入和只读 shell 命令保持未命中。"""
    guard = _guard(tmp_path)
    write_rule = guard.match(
        _request(
            tool_name="write_file",
            arguments={"path": "src/app.py", "content": "print('ok')"},
            cwd=tmp_path,
        )
    )
    shell_rule = guard.match(
        _request(tool_name="run_shell", arguments={"command": "git status"}, cwd=tmp_path)
    )
    assert write_rule is None
    assert shell_rule is None


@pytest.mark.unit
def test_danger_rule_is_frozen_value_object() -> None:
    """DangerRule 创建后保持不可变，供跨层安全消费。"""
    rule = DangerRule(
        name="example",
        reason="reason",
        matcher="matcher",
        target_kind=DangerTargetKind.PATH,
        target_value="target",
    )
    with pytest.raises(FrozenInstanceError):
        rule.name = "changed"  # type: ignore[misc]
