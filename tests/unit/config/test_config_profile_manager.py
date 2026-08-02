"""配置 profile 同步管理器测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

from infrastructure.config import load_config
from infrastructure.config.profile_manager import ConfigProfileManager

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_CONFIG = REPO_ROOT / "config" / "setting.yaml"
TARGET_CONFIG = REPO_ROOT / "config" / "xspace" / "setting.yaml"
POLICY_CONFIG = REPO_ROOT / "config" / "xspace" / "sync-policy.yaml"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "config-xspace-sync.py"
XSPACE_KEEP_PATHS = (
    "cli.show_reasoning",
    "evolution.learning.drain_on_close_seconds",
    "evolution.learning.enabled",
    "evolution.learning.every_n_runs",
    "evolution.learning.max_nutrients",
    "evolution.learning.review_timeout_seconds",
    "evolution.memory.view_max_chars",
    "web.host",
    "web_search.search_tool_name",
    "web_search.search_tool_names",
)


def _copy_profile_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    """复制真实 profile 三件套到临时目录，返回 source/target/policy 路径。"""
    source = tmp_path / "setting.yaml"
    target = tmp_path / "xspace-setting.yaml"
    policy = tmp_path / "sync-policy.yaml"
    source.write_text(SOURCE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(TARGET_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    policy.write_text(POLICY_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return source, target, policy


def _manager(source: Path, target: Path, policy: Path) -> ConfigProfileManager:
    """构造测试用 ConfigProfileManager。"""
    return ConfigProfileManager(source_path=source, target_path=target, policy_path=policy)


def _write_complete_policy(source: Path, target: Path, policy: Path) -> None:
    """写入当前 XSpace profile 需要的完整决策。"""
    manager = _manager(source, target, policy)
    for path in XSPACE_KEEP_PATHS:
        manager.write_decision(path, "xspace-keep", f"{path} 使用 XSpace profile 值")


def _remove_policy_decision(policy: Path, path: str) -> None:
    """从 policy 中结构化移除指定 path 的 decision。"""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with policy.open("r", encoding="utf-8") as f:
        doc = yaml.load(f)
    decisions = doc["decisions"]
    doc["decisions"] = [item for item in decisions if item.get("path") != path]
    with policy.open("w", encoding="utf-8") as f:
        yaml.dump(doc, f)


def test_profile_review_passes_with_real_policy_copy(tmp_path: Path) -> None:
    """补齐完整决策后，profile review 必须通过。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)

    review = _manager(source, target, policy).review()

    assert not review.issues


def test_profile_review_reports_missing_search_tools_xspace_keep_decision(
    tmp_path: Path,
) -> None:
    """XSpace 搜索工具列表缺 keep 决策时，review 必须报告待决策。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)
    _remove_policy_decision(policy, "web_search.search_tool_names")

    review = _manager(source, target, policy).review()

    assert any(
        issue.path == "web_search.search_tool_names"
        and issue.code == "xspace-keep-decision-required"
        for issue in review.issues
    )


def test_profile_review_reports_missing_xspace_keep_decision(tmp_path: Path) -> None:
    """目标值与主配置不同且缺 xspace-keep 决策时，review 必须报告待决策。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)
    _remove_policy_decision(policy, "web.host")

    review = _manager(source, target, policy).review()

    assert any(
        issue.path == "web.host" and issue.code == "xspace-keep-decision-required"
        for issue in review.issues
    )


def test_profile_review_allows_policy_omission_for_matching_explicit_value(
    tmp_path: Path,
) -> None:
    """目标显式值等于主配置时，policy 可以省略该字段。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)

    review = _manager(source, target, policy).review()

    assert not any(issue.path == "scheduler.default_max_turns" for issue in review.issues)


def test_profile_review_reports_stale_source_hash(tmp_path: Path) -> None:
    """主配置 hash 变化后，review 必须报告过期决策。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with policy.open("r", encoding="utf-8") as f:
        doc = yaml.load(f)
    doc["decisions"][0]["source_hash"] = "sha256:stale"
    with policy.open("w", encoding="utf-8") as f:
        yaml.dump(doc, f)

    review = _manager(source, target, policy).review()

    assert any(issue.code == "decision-stale" for issue in review.issues)


def test_sync_copy_updates_target_and_policy(tmp_path: Path) -> None:
    """sync_copy 必须写回目标 YAML 并把决策改成 sync-copy。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)

    _manager(source, target, policy).sync_copy("web.host", "测试复制主配置 host")

    cfg = load_config(target, load_env_file=False)
    policy_text = policy.read_text(encoding="utf-8")
    review = _manager(source, target, policy).review()

    assert cfg.web.host == "0.0.0.0"
    assert "path: web.host" in policy_text
    assert "action: sync-copy" in policy_text
    assert not review.issues


def test_write_decision_rejects_blank_reason(tmp_path: Path) -> None:
    """decision reason 为空时，Manager 必须拒绝写入自失效 policy。"""
    source, target, policy = _copy_profile_files(tmp_path)
    before = policy.read_text(encoding="utf-8")

    try:
        _manager(source, target, policy).write_decision("web.host", "xspace-keep", "  ")
    except ValueError as exc:
        assert "reason" in str(exc)
    else:
        raise AssertionError("blank reason should raise ValueError")

    assert policy.read_text(encoding="utf-8") == before


def test_sync_copy_rejects_blank_reason(tmp_path: Path) -> None:
    """sync-copy reason 为空时，Manager 必须拒绝改写目标 YAML。"""
    source, target, policy = _copy_profile_files(tmp_path)
    before_target = target.read_text(encoding="utf-8")
    before_policy = policy.read_text(encoding="utf-8")

    try:
        _manager(source, target, policy).sync_copy("web.host", "  ")
    except ValueError as exc:
        assert "reason" in str(exc)
    else:
        raise AssertionError("blank reason should raise ValueError")

    assert target.read_text(encoding="utf-8") == before_target
    assert policy.read_text(encoding="utf-8") == before_policy


def test_config_xspace_sync_script_rejects_blank_reason(tmp_path: Path) -> None:
    """脚本必须把空 reason 转为非零退出，保持 policy 文件不变。"""
    source, target, policy = _copy_profile_files(tmp_path)
    before = policy.read_text(encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--source",
            str(source),
            "--target",
            str(target),
            "--policy",
            str(policy),
            "decision",
            "--path",
            "web.host",
            "--action",
            "xspace-keep",
            "--reason",
            "",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "reason" in result.stderr
    assert policy.read_text(encoding="utf-8") == before


def test_config_xspace_sync_script_review_passes(tmp_path: Path) -> None:
    """脚本 review 子命令必须调用 Manager 并返回可诊断输出。"""
    source, target, policy = _copy_profile_files(tmp_path)
    _write_complete_policy(source, target, policy)

    result = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--source",
            str(source),
            "--target",
            str(target),
            "--policy",
            str(policy),
            "review",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "review=pass" in result.stdout
