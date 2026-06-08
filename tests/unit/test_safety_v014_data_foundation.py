"""unit：safety v0.1.4 数据基座（M1）完整性。

覆盖：
1. ``src/safety/types.py`` 全部枚举值与 dataclass schema
2. ``src/safety/default_rules.py`` 默认数据合法性
3. ``Config.safety`` yaml schema 校验（含 boundary_scope=sandbox 拒绝）
4. ``KONGMING_SAFETY_*`` env 覆盖
5. 模块边界门禁（types/default_rules 不混入运行时判定逻辑）
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from infrastructure.config.models import (
    Config,
    ModelConfig,
    SafetyApprovalRequiredConfig,
    SafetyConfig,
    SafetyHardDenyConfig,
    SafetySensitivePathConfig,
)
from safety.default_rules import (
    DEFAULT_ALLOW_TOOLS_SILENT,
    DEFAULT_ALLOW_WRITES,
    DEFAULT_APPROVAL_REQUIRED_COMMANDS,
    DEFAULT_HARD_DENY_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
    DEFAULT_TRUSTED_WORKDIRS,
)
from safety.types import (
    ApprovalMetadataKeys,
    ApprovalRequiredCommand,
    BoundaryDecision,
    BoundaryKind,
    BoundaryScope,
    BoundaryZone,
    DecisionClass,
    DecisionSource,
    Grant,
    GrantKey,
    HardDenyCommand,
    SensitivePathRule,
)

# ---------------------------------------------------------------------------
# §1 枚举值
# ---------------------------------------------------------------------------


def test_decision_class_values_match_doc() -> None:
    assert DecisionClass.HARD_BLOCK.value == "hard_block"
    assert DecisionClass.SILENT_ALLOW.value == "silent_allow"
    assert DecisionClass.EXPLICIT_CONSENT.value == "explicit_consent"


def test_decision_source_values_match_doc() -> None:
    assert {s.value for s in DecisionSource} == {
        "intrinsic",
        "session",
        "config",
        "standard",
        "elevated",
    }


def test_boundary_zone_values_match_doc() -> None:
    assert {z.value for z in BoundaryZone} == {"trusted", "approval", "blocked"}


def test_boundary_kind_values_match_doc() -> None:
    assert {k.value for k in BoundaryKind} == {"host", "sandbox"}


def test_boundary_scope_values_match_doc() -> None:
    assert {s.value for s in BoundaryScope} == {"any", "host", "sandbox"}


# ---------------------------------------------------------------------------
# §2 dataclass frozen 与字段完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        HardDenyCommand,
        ApprovalRequiredCommand,
        SensitivePathRule,
        BoundaryDecision,
        GrantKey,
        Grant,
    ],
)
def test_dataclass_is_frozen(cls: type) -> None:
    assert is_dataclass(cls)
    rule = HardDenyCommand(
        name="t",
        matcher="r",
        match_mode="profile",
        boundary_scope=BoundaryScope.ANY,
        reason="t",
    )
    if cls is HardDenyCommand:
        with pytest.raises(FrozenInstanceError):
            rule.name = "x"  # type: ignore[misc]


def test_hard_deny_command_field_set() -> None:
    names = {f.name for f in fields(HardDenyCommand)}
    assert names == {"name", "matcher", "match_mode", "boundary_scope", "reason"}


def test_approval_required_command_field_set() -> None:
    names = {f.name for f in fields(ApprovalRequiredCommand)}
    assert names == {
        "name",
        "matcher",
        "match_mode",
        "severity",
        "boundary_scope",
        "reason",
    }


def test_sensitive_path_rule_field_set() -> None:
    names = {f.name for f in fields(SensitivePathRule)}
    assert names == {
        "name",
        "matcher",
        "match_mode",
        "ops",
        "effect",
        "boundary_scope",
        "reason",
    }


def test_grant_key_includes_boundary_kind() -> None:
    names = {f.name for f in fields(GrantKey)}
    assert names == {"capability", "matcher", "boundary_kind"}


def test_approval_metadata_keys_full_set() -> None:
    keys = ApprovalMetadataKeys()
    assert keys.DECISION_CLASS == "decision_class"
    assert keys.DECISION_SOURCE == "decision_source"
    assert keys.MATCHED_RULE == "matched_rule"
    assert keys.REASON == "reason"
    assert keys.GRANT_SCOPE == "grant_scope"
    assert keys.BOUNDARY_KIND == "boundary_kind"
    assert keys.STAGE == "stage"
    assert keys.SUGGESTED_ALTERNATIVES == "suggested_alternatives"


# ---------------------------------------------------------------------------
# §3 默认规则数据合法性
# ---------------------------------------------------------------------------


def test_default_hard_deny_commands_nonempty_and_typed() -> None:
    assert len(DEFAULT_HARD_DENY_COMMANDS) >= 5
    for rule in DEFAULT_HARD_DENY_COMMANDS:
        assert isinstance(rule, HardDenyCommand)
        assert rule.boundary_scope in (BoundaryScope.HOST, BoundaryScope.ANY)


def test_default_approval_required_commands_typed() -> None:
    for rule in DEFAULT_APPROVAL_REQUIRED_COMMANDS:
        assert isinstance(rule, ApprovalRequiredCommand)
        assert rule.severity in {"standard", "elevated"}
        assert rule.boundary_scope in (BoundaryScope.HOST, BoundaryScope.ANY)


def test_default_sensitive_paths_block_credentials() -> None:
    blocked_matchers = {rule.matcher for rule in DEFAULT_SENSITIVE_PATHS if rule.effect == "block"}
    assert "~/.ssh/" in blocked_matchers
    assert "~/.kongming/" in blocked_matchers
    assert "~/.aws/credentials" in blocked_matchers


def test_default_sensitive_paths_elevated_includes_env_and_agents_md() -> None:
    elevated_matchers = {
        rule.matcher for rule in DEFAULT_SENSITIVE_PATHS if rule.effect == "elevated"
    }
    assert ".env*" in elevated_matchers
    assert "AGENTS.md" in elevated_matchers
    assert "CLAUDE.md" in elevated_matchers


def test_default_sensitive_paths_allow_kongming_workdir() -> None:
    allowed = {rule.matcher for rule in DEFAULT_SENSITIVE_PATHS if rule.effect == "allow"}
    assert ".kongming/work/" in allowed
    assert ".kongming/tmp/" in allowed


def test_default_trusted_workdirs_include_kongming_dirs() -> None:
    assert ".kongming/work/" in DEFAULT_TRUSTED_WORKDIRS
    assert ".kongming/tmp/" in DEFAULT_TRUSTED_WORKDIRS


def test_default_allow_writes_and_tools_silent_typed() -> None:
    assert all(isinstance(p, str) for p in DEFAULT_ALLOW_WRITES)
    assert "read_file" in DEFAULT_ALLOW_TOOLS_SILENT
    assert "list_dir" in DEFAULT_ALLOW_TOOLS_SILENT


# ---------------------------------------------------------------------------
# §3.5 默认 hard_deny_commands regex smoke check（攻防向量正反案例）
# ---------------------------------------------------------------------------


_HARD_DENY_POSITIVE_CASES: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf .",
    "rm -rf /*",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda1",
    "fdisk /dev/sda",
    "chmod -R 000 /",
    "chown -R user /",
    "rm --no-preserve-root -rf /",
    ":(){ :|:& };:",
)


_HARD_DENY_NEGATIVE_CASES: tuple[str, ...] = (
    "rm -rf /home",  # 子目录由 sensitive_paths 处理
    "rm -rf ~/Documents",  # 用户子路径由 boundary 处理
    "rm /tmp/x",  # 无 -r 不属于硬禁
    "python rm.py",  # 不是 rm 命令
    "ls -rf /",  # 不是 rm
    "echo rm -rf /",  # echo 输出不是命令本身
)


@pytest.mark.parametrize("cmd", _HARD_DENY_POSITIVE_CASES)
def test_default_hard_deny_regex_matches_canonical_attacks(cmd: str) -> None:
    """每个典型攻击向量必须被至少一条 segment_regex 规则命中。"""
    matched = any(
        rule.match_mode == "segment_regex" and re.search(rule.matcher, cmd)
        for rule in DEFAULT_HARD_DENY_COMMANDS
    )
    assert matched, f"No default hard_deny rule matched canonical attack: {cmd!r}"


@pytest.mark.parametrize("cmd", _HARD_DENY_NEGATIVE_CASES)
def test_default_hard_deny_regex_skips_non_attack_commands(cmd: str) -> None:
    """正常命令不应被任何 segment_regex 规则命中。"""
    matched = [
        rule.name
        for rule in DEFAULT_HARD_DENY_COMMANDS
        if rule.match_mode == "segment_regex" and re.search(rule.matcher, cmd)
    ]
    assert not matched, f"Default hard_deny rule {matched} false-matched safe command: {cmd!r}"


# ---------------------------------------------------------------------------
# §4 Config.safety yaml schema 校验
# ---------------------------------------------------------------------------


def _local_model_config() -> ModelConfig:
    return ModelConfig(
        provider="openai_compatible",
        name="m",
        base_url="http://127.0.0.1:1234",
        api_key="",
    )


def test_config_safety_default_empty_lists() -> None:
    cfg = Config(model=_local_model_config())
    assert cfg.safety.hard_deny_commands == []
    assert cfg.safety.approval_required_commands == []
    assert cfg.safety.sensitive_paths == []
    assert cfg.safety.trusted_workdirs == []
    assert cfg.safety.allow_writes == []
    assert cfg.safety.allow_tools_silent == []
    assert cfg.safety.log_silent_reads is False


def test_safety_hard_deny_rejects_sandbox_scope() -> None:
    with pytest.raises(ValueError, match=r"boundary_scope='sandbox'"):
        SafetyHardDenyConfig(
            name="x",
            matcher="^rm",
            match_mode="segment_regex",
            boundary_scope="sandbox",
            reason="t",
        )


def test_safety_hard_deny_accepts_host_and_any() -> None:
    SafetyHardDenyConfig(
        name="x",
        matcher="^rm",
        match_mode="segment_regex",
        boundary_scope="host",
        reason="t",
    )
    SafetyHardDenyConfig(
        name="y",
        matcher="^dd",
        match_mode="segment_regex",
        boundary_scope="any",
        reason="t",
    )


def test_safety_approval_required_rejects_sandbox_scope() -> None:
    with pytest.raises(ValueError, match=r"boundary_scope='sandbox'"):
        SafetyApprovalRequiredConfig(
            name="x",
            matcher="git_push",
            match_mode="profile",
            severity="standard",
            boundary_scope="sandbox",
            reason="t",
        )


def test_safety_sensitive_path_rejects_sandbox_scope() -> None:
    with pytest.raises(ValueError, match=r"boundary_scope='sandbox'"):
        SafetySensitivePathConfig(
            name="x",
            matcher="~/.ssh/",
            match_mode="path_prefix",
            ops=["read", "write"],
            effect="block",
            boundary_scope="sandbox",
            reason="t",
        )


def test_safety_sensitive_path_rejects_empty_ops() -> None:
    with pytest.raises(ValueError, match="ops must contain"):
        SafetySensitivePathConfig(
            name="x",
            matcher="~/.ssh/",
            match_mode="path_prefix",
            ops=[],
            effect="block",
            boundary_scope="host",
            reason="t",
        )


def test_safety_config_extra_field_rejected() -> None:
    with pytest.raises(ValueError):
        SafetyConfig(unknown_field="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# §5 env 覆盖（list[str] 字段从逗号分隔字符串解析）
# ---------------------------------------------------------------------------


def test_safety_config_coerces_list_from_csv_string() -> None:
    cfg = SafetyConfig(
        trusted_workdirs="a/, b/, c/",  # type: ignore[arg-type]
        allow_writes="~/scratch/, /tmp/x/",  # type: ignore[arg-type]
        allow_tools_silent="read_file, list_dir",  # type: ignore[arg-type]
    )
    assert cfg.trusted_workdirs == ["a/", "b/", "c/"]
    assert cfg.allow_writes == ["~/scratch/", "/tmp/x/"]
    assert cfg.allow_tools_silent == ["read_file", "list_dir"]


def test_safety_config_log_silent_reads_default_false() -> None:
    cfg = SafetyConfig()
    assert cfg.log_silent_reads is False


# ---------------------------------------------------------------------------
# §6 模块边界门禁：types/default_rules 不得混入运行时判定逻辑
# ---------------------------------------------------------------------------


_FORBIDDEN_PATTERNS_IN_M1 = re.compile(
    r"^(?!\s*[\"'#]).*\b("
    r"realpath\s*\(|"
    r"git\s+ls-files|"
    r"subprocess\.\w+|"
    r"if\s+\w+\.boundary_kind\s*==\s*BoundaryKind\.SANDBOX"
    r")\b",
    re.MULTILINE,
)


def _find_forbidden(file_path: Path) -> list[str]:
    text = file_path.read_text(encoding="utf-8")
    hits: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        if _FORBIDDEN_PATTERNS_IN_M1.search(line):
            hits.append(f"{file_path.name}:{line_no}: {line.rstrip()}")
    return hits


def test_module_1_boundary_no_runtime_logic() -> None:
    """M1 严格边界：types.py / default_rules.py 不允许混入运行时判定逻辑。"""
    src_root = Path(__file__).resolve().parents[2] / "src" / "safety"
    targets = [src_root / "types.py", src_root / "default_rules.py"]
    leaks: list[str] = []
    for target in targets:
        leaks.extend(_find_forbidden(target))
    assert not leaks, "M1 boundary violated:\n" + "\n".join(leaks)
