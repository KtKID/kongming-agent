"""验证 permissions v0.6 单 thread 定向迁移脚本。

覆盖必填 thread、显式 dry-run/apply、历史 allow/deny 与 approval_rules 转换、
ask 丢弃、trusted_workdirs 候选、diff 报告、幂等去重、revision CAS 和零 thread
扇出。测试使用真实 PermissionsManager 与隔离 kongming_home，不接触 scheduler。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import scripts.migrate_permissions_v06 as migration
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord, ThreadPermissionsSnapshot


def _rule(expression: str) -> PermissionRuleRecord:
    """构造无 cwd 的非 Shell 或 deny 规则。"""
    return PermissionRuleRecord(expression=expression, scope_cwd=None)


def _write_legacy_setting(path: Path) -> None:
    """写入覆盖全部受支持历史结构的旧 setting。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "safety:\n"
        "  permissions:\n"
        "    allow: [read_file, read_file]\n"
        "    deny: [run_shell(curl:*)]\n"
        "  allow: [list_dir]\n"
        "  deny: [run_shell(wget:*)]\n"
        "  approval_rules:\n"
        "    - id: allow-git\n"
        "      behavior: allow\n"
        "      expression: run_shell(git:*)\n"
        "    - id: deny-curl-duplicate\n"
        "      behavior: deny\n"
        "      expression: run_shell(curl:*)\n"
        "    - id: old-ask\n"
        "      behavior: ask\n"
        "      expression: write_file\n"
        "    - id: disabled-allow\n"
        "      behavior: allow\n"
        "      expression: edit_file\n"
        "      enabled: false\n"
        "  allow_tools_silent: [read_file]\n"
        "  allow_writes: [generated/]\n"
        "  trusted_workdirs: [scratch/]\n",
        encoding="utf-8",
    )


def _run_migration(
    *,
    config_path: Path,
    kongming_home: Path,
    project_root: Path,
    thread_id: str,
    apply: bool,
    manager: PermissionsManager | None = None,
) -> migration.MigrationResult:
    """同步执行异步迁移入口，供单测复用。"""
    return asyncio.run(
        migration.migrate_permissions(
            config_path=config_path,
            kongming_home=kongming_home,
            project_root=project_root,
            thread_id=thread_id,
            apply=apply,
            manager=manager,
        )
    )


def _snapshot(manager: PermissionsManager, thread_id: str) -> ThreadPermissionsSnapshot:
    """同步读取指定 thread 快照。"""
    return asyncio.run(manager.snapshot(thread_id))


def test_cli_requires_thread_id_and_explicit_mode() -> None:
    """CLI 强制提供 thread id，并要求 dry-run/apply 二选一。"""
    with pytest.raises(SystemExit) as missing_thread:
        migration._parse_args(["--dry-run"])
    assert missing_thread.value.code == 2

    with pytest.raises(SystemExit) as missing_mode:
        migration._parse_args(["--thread-id", "thread-a"])
    assert missing_mode.value.code == 2

    with pytest.raises(SystemExit) as conflicting_mode:
        migration._parse_args(["--thread-id", "thread-a", "--dry-run", "--apply"])
    assert conflicting_mode.value.code == 2


def test_cli_accepts_dry_run_and_apply_modes() -> None:
    """两个显式执行模式分别映射到稳定布尔参数。"""
    dry_args = migration._parse_args(["--thread-id", "thread-a", "--dry-run"])
    apply_args = migration._parse_args(["--thread-id", "thread-a", "--apply"])
    assert dry_args.dry_run is True and dry_args.apply is False
    assert apply_args.apply is True and apply_args.dry_run is False


def test_dry_run_builds_diff_without_writing_target_thread(tmp_path: Path) -> None:
    """dry-run 展示定向 diff、丢弃 ask，并保持目标本子未物化。"""
    config_path = tmp_path / "project" / "config" / "setting.yaml"
    project_root = tmp_path / "project"
    kongming_home = tmp_path / "home"
    _write_legacy_setting(config_path)

    result = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=project_root,
        thread_id="thread-a",
        apply=False,
    )

    assert result.applied is False
    assert result.before.revision == 0
    assert result.after.revision == 0
    assert result.discarded_ask == 1
    assert result.diff.allow_added[:2] == (
        "read_file",
        "list_dir",
    )
    assert result.invalidated_shell_allow_count == 1
    assert result.diff.deny_added == (
        "run_shell(curl:*)",
        "run_shell(wget:*)",
    )
    assert not list((kongming_home / "safety" / "thread_permissions").glob("*.json"))


def test_apply_writes_only_migratable_rules_through_real_manager(tmp_path: Path) -> None:
    """apply 经真实 Manager 写入 allow/deny，ask 和 trusted 候选保持在本子外。"""
    config_path = tmp_path / "project" / "config" / "setting.yaml"
    project_root = tmp_path / "project"
    kongming_home = tmp_path / "home"
    _write_legacy_setting(config_path)

    result = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=project_root,
        thread_id="thread-a",
        apply=True,
    )
    persisted = _snapshot(PermissionsManager(kongming_home), "thread-a")

    assert result.applied is True
    assert result.after.revision == 1
    assert persisted == result.after
    assert _rule("write_file") not in result.after.allow
    assert _rule("edit_file") not in result.after.allow
    assert all(
        _rule(candidate) not in result.after.allow for candidate in result.trusted_candidates
    )
    assert result.trusted_candidates == (
        f"read_file({(project_root / 'scratch').resolve().as_posix()}/**)",
        f"list_dir({(project_root / 'scratch').resolve().as_posix()}/**)",
        f"write_file({(project_root / 'scratch').resolve().as_posix()}/**)",
        f"edit_file({(project_root / 'scratch').resolve().as_posix()}/**)",
    )


def test_apply_is_idempotent_and_deduplicates_rules(tmp_path: Path) -> None:
    """重复 apply 不增加 revision，历史重复项只保留第一次。"""
    config_path = tmp_path / "setting.yaml"
    kongming_home = tmp_path / "home"
    _write_legacy_setting(config_path)

    first = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=tmp_path,
        thread_id="thread-a",
        apply=True,
    )
    second = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=tmp_path,
        thread_id="thread-a",
        apply=True,
    )

    assert first.after.revision == 1
    assert second.applied is False
    assert second.diff.changed is False
    assert second.after.revision == 1
    assert second.after.allow.count(_rule("read_file")) == 1
    assert second.after.deny.count(_rule("run_shell(curl:*)")) == 1


def test_deny_wins_when_same_expression_exists_in_allow(tmp_path: Path) -> None:
    """迁移来源同时 allow/deny 同一表达式时，计划从 allow 移除并保留 deny。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        "safety:\n  permissions:\n    allow: [read_file]\n    deny: [read_file]\n",
        encoding="utf-8",
    )
    result = _run_migration(
        config_path=config_path,
        kongming_home=tmp_path / "home",
        project_root=tmp_path,
        thread_id="thread-a",
        apply=False,
    )
    assert "read_file" not in result.diff.allow_added
    assert result.diff.deny_added == ("read_file",)


def test_report_contains_stable_diff_and_candidate_summary(tmp_path: Path) -> None:
    """报告明确展示目标 thread、增删数量、ask 丢弃和 trusted 候选。"""
    config_path = tmp_path / "setting.yaml"
    _write_legacy_setting(config_path)
    result = _run_migration(
        config_path=config_path,
        kongming_home=tmp_path / "home",
        project_root=tmp_path,
        thread_id="thread-report",
        apply=False,
    )
    report = migration.render_report(result, dry_run=True)
    assert "thread_id: thread-report" in report
    assert "allow: +" in report
    assert "deny: +2 -0" in report
    assert "discarded ask rules: 1" in report
    assert "trusted_workdirs candidates: 4" in report
    assert "dry-run: target thread was not written" in report


def test_cli_dry_run_prints_diff_and_keeps_disk_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实 CLI dry-run 返回成功、打印摘要并保持 permissions 目录为空。"""
    config_path = tmp_path / "setting.yaml"
    kongming_home = tmp_path / "home"
    _write_legacy_setting(config_path)
    exit_code = migration.main(
        [
            "--thread-id",
            "thread-cli",
            "--dry-run",
            "--config",
            str(config_path),
            "--kongming-home",
            str(kongming_home),
            "--project-root",
            str(tmp_path),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "permissions v0.6 migration [DRY RUN]" in output
    assert "thread_id: thread-cli" in output
    assert not list((kongming_home / "safety" / "thread_permissions").glob("*.json"))


def test_migration_never_fans_out_to_other_threads(tmp_path: Path) -> None:
    """迁移显式 thread-a 时，已存在的 thread-b 快照与 revision 保持原值。"""
    config_path = tmp_path / "setting.yaml"
    kongming_home = tmp_path / "home"
    _write_legacy_setting(config_path)
    manager = PermissionsManager(kongming_home)
    before_b = asyncio.run(
        manager.replace(
            "thread-b",
            allow=(_rule("list_dir"),),
            deny=(),
            expected_revision=0,
        )
    )

    result = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=tmp_path,
        thread_id="thread-a",
        apply=True,
        manager=manager,
    )
    after_b = _snapshot(manager, "thread-b")

    assert result.thread_id == "thread-a"
    assert result.after.revision == 1
    assert after_b == before_b


def test_existing_revision_is_used_for_cas_update(tmp_path: Path) -> None:
    """已有目标本子从当前 revision 执行 CAS，成功后只增加一次 revision。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text("safety:\n  allow: [read_file]\n", encoding="utf-8")
    kongming_home = tmp_path / "home"
    manager = PermissionsManager(kongming_home)
    initial = asyncio.run(
        manager.replace(
            "thread-a",
            allow=(_rule("list_dir"),),
            deny=(),
            expected_revision=0,
        )
    )

    result = _run_migration(
        config_path=config_path,
        kongming_home=kongming_home,
        project_root=tmp_path,
        thread_id="thread-a",
        apply=True,
        manager=manager,
    )
    assert initial.revision == 1
    assert result.before.revision == 1
    assert result.after.revision == 2
    assert result.after.allow == (_rule("list_dir"), _rule("read_file"))
