"""unit：config/setting.yaml 用户自定义 safety 规则解析正确。

本测试**只断言 yaml 解析后字段进了 SafetyConfig**，不验证运行时拦截行为
（运行时拦截由 hard_block / consent guard 自己的单测覆盖）。这样配置层的
yaml typo / 字段名错误能在最早期发现。
"""

from __future__ import annotations

from pathlib import Path

from config_loader import load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTING_YAML = _REPO_ROOT / "config" / "setting.yaml"


def test_setting_yaml_loads_without_validation_error() -> None:
    """yaml 整体能被 pydantic 加载（schema 不出错）。

    本测试是冒烟保护：任何用户加错字段名 / 拼错 effect / boundary_scope
    用 sandbox（v0.1.4 起拒绝）等会立刻挂在这里。
    """
    cfg = load_config(_SETTING_YAML, load_env_file=False)
    assert cfg.safety is not None


def test_setting_yaml_sensitive_paths_protect_personal_dirs() -> None:
    """sensitive_paths 含本次补的 8 条个人数据目录规则。"""
    cfg = load_config(_SETTING_YAML, load_env_file=False)
    rules_by_name = {r.name: r for r in cfg.safety.sensitive_paths}

    # 个人数据目录（read+write block）
    for name, matcher in (
        ("user-documents", "~/Documents/"),
        ("user-desktop", "~/Desktop/"),
        ("user-downloads", "~/Downloads/"),
        ("macos-keychains", "~/Library/Keychains/"),
        ("macos-mail", "~/Library/Mail/"),
    ):
        assert name in rules_by_name, f"missing sensitive_paths rule: {name}"
        rule = rules_by_name[name]
        assert rule.matcher == matcher
        assert rule.match_mode == "path_prefix"
        assert rule.effect == "block"
        assert "read" in rule.ops and "write" in rule.ops

    # 媒体目录：write 拦，read 放（让 agent 能看图/音/视频）
    for name, matcher in (
        ("user-pictures-write", "~/Pictures/"),
        ("user-movies-write", "~/Movies/"),
        ("user-music-write", "~/Music/"),
    ):
        assert name in rules_by_name, f"missing sensitive_paths rule: {name}"
        rule = rules_by_name[name]
        assert rule.matcher == matcher
        assert rule.effect == "block"
        # 关键：只拦 write，不拦 read
        assert "write" in rule.ops
        assert "read" not in rule.ops


def test_setting_yaml_rm_recursive_requires_elevated_approval() -> None:
    """approval_required_commands 含 rm-recursive-elevated 规则，severity=elevated。"""
    cfg = load_config(_SETTING_YAML, load_env_file=False)
    rules_by_name = {r.name: r for r in cfg.safety.approval_required_commands}

    assert "rm-recursive-elevated" in rules_by_name, (
        "missing rm-recursive-elevated rule in approval_required_commands"
    )
    rule = rules_by_name["rm-recursive-elevated"]
    assert rule.match_mode == "segment_regex"
    assert rule.severity == "elevated"
    # SafetyApprovalRequiredConfig.boundary_scope 是 Literal[str]
    assert rule.boundary_scope == "any"
    # regex 必须能命中典型 rm 形态
    import re

    pattern = re.compile(rule.matcher)
    assert pattern.search("rm -rf warp")
    assert pattern.search("rm -r build/")
    assert pattern.search("rm --recursive node_modules")
    assert pattern.search("rm -fR /tmp/foo")
    # 不应误命中
    assert not pattern.search("rm file.txt")  # 没 -r
    assert not pattern.search("python rm.py")  # 不是 rm 起头
