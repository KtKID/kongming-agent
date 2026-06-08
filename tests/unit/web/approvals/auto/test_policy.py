"""policy.py 单测：classify + elevated 硬约束 + rule_overrides。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hosts.web.approvals.auto.config_store import ConfigStore, ProjectConfig
from hosts.web.approvals.auto.policy import AutoApprovalPolicy, Decision
from hosts.web.approvals.auto.rules import load_default_rules


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "auto_approval")


@pytest.fixture()
def policy(store: ConfigStore) -> AutoApprovalPolicy:
    return AutoApprovalPolicy(load_default_rules(), store)


# ---------- elevated 硬约束（核心安全规则）----------


class TestElevatedHardConstraint:
    """elevated 路径必须在 classify 第一行短路；不读 yaml / 不查 config。"""

    def test_elevated_always_blocks_auto_eligible(self, policy: AutoApprovalPolicy) -> None:
        # 即使 cwd 启用了智能审批，elevated 也不能自动通过
        policy.set_enabled("/proj", True)

        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "echo hello"},  # 无害命令
            cwd="/proj",
            is_elevated=True,
        )
        assert decision.auto_eligible is False
        assert decision.rule_evaluation.get("reason") == "elevated_hard_constraint"

    def test_elevated_works_with_disabled_policy(self, policy: AutoApprovalPolicy) -> None:
        # 即使智能审批关着，elevated 也走特殊路径
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            cwd="/no-config",
            is_elevated=True,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule is None
        assert decision.rule_evaluation.get("reason") == "elevated_hard_constraint"

    def test_elevated_does_not_read_config(
        self,
        store: ConfigStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """elevated 路径绝对不应调用 config_store.get_or_default。"""
        policy = AutoApprovalPolicy(load_default_rules(), store)

        call_count = {"n": 0}
        orig_get = store.get_or_default

        def spy(cwd: str) -> ProjectConfig:
            call_count["n"] += 1
            return orig_get(cwd)

        monkeypatch.setattr(store, "get_or_default", spy)

        policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=True,
        )
        assert call_count["n"] == 0, "elevated must not touch config_store"

    def test_elevated_is_first_line(self) -> None:
        """白盒：classify 源码里 ``if is_elevated`` 必须出现在配置读取之前。"""
        import inspect

        src = inspect.getsource(AutoApprovalPolicy.classify)
        elevated_pos = src.find("is_elevated")
        config_pos = src.find("get_or_default")
        assert elevated_pos > 0
        assert config_pos > 0
        assert elevated_pos < config_pos, (
            "elevated short-circuit must precede config_store.get_or_default"
        )


# ---------- policy 关闭：完全不自动通过 ----------


class TestPolicyDisabled:
    def test_new_cwd_default_off(self, policy: AutoApprovalPolicy) -> None:
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/never-configured",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule is None
        assert decision.rule_evaluation.get("reason") == "policy_disabled_for_cwd"

    def test_disabled_does_not_evaluate_rules(self, policy: AutoApprovalPolicy) -> None:
        # 即使是危险命令，policy off 时不评估规则（all_rules 应为空）
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            cwd="/off",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule is None  # 不评估
        assert decision.rule_evaluation.get("all_rules") == []


# ---------- policy 启用 + 命中规则 ----------


class TestPolicyEnabledAndBlocked:
    def test_dangerous_bash_blocked(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/x"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        # v1.0.1: bash_rm_any 在 yaml 中排在 bash_rm_recursive 之前，先命中先返回
        assert decision.blocked_by_rule == "bash_rm_any"

    def test_dangerous_git_force_blocked(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "git push --force origin main"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule == "bash_git_push_force"

    def test_dangerous_edit_dotenv_blocked(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Write",
            tool_input={"file_path": "/proj/.env"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule == "edit_dotenv"

    def test_unknown_mcp_blocked(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="mcp__playwright__browser_click",
            tool_input={},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is False
        assert decision.blocked_by_rule == "mcp_unknown"


# ---------- policy 启用 + 未命中 → auto_eligible ----------


class TestPolicyEnabledAuto:
    def test_safe_bash_auto_eligible(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls /tmp"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is True
        assert decision.blocked_by_rule is None

    def test_safe_edit_auto_eligible(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Edit",
            tool_input={"file_path": "/proj/src/foo.py"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.auto_eligible is True

    def test_returns_default_timeout(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.timeout_ms == 10000  # default_rules.yaml


# ---------- rule_overrides：用户可关单条规则 ----------


class TestRuleOverrides:
    def test_disabled_rule_lets_dangerous_through(
        self,
        store: ConfigStore,
        policy: AutoApprovalPolicy,
    ) -> None:
        """用户关掉 bash_sudo → sudo 命令自动通过。"""
        store.set(
            ProjectConfig(
                cwd="/proj",
                enabled=True,
                rule_overrides={"bash_sudo": False},
            ),
        )
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "sudo ls"},
            cwd="/proj",
            is_elevated=False,
        )
        # bash_sudo 被关 → 不拦
        # 但要检查没被其他规则误捕
        assert decision.blocked_by_rule != "bash_sudo"
        assert decision.auto_eligible is True

    def test_disabled_rule_excluded_from_all_rules(
        self,
        store: ConfigStore,
        policy: AutoApprovalPolicy,
    ) -> None:
        store.set(
            ProjectConfig(
                cwd="/proj",
                enabled=True,
                rule_overrides={"bash_sudo": False, "bash_dd": False},
            ),
        )
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=False,
        )
        all_rules = decision.rule_evaluation["all_rules"]
        assert "bash_sudo" not in all_rules
        assert "bash_dd" not in all_rules
        assert "bash_rm_recursive" in all_rules

    def test_per_cwd_timeout_overrides_global(
        self,
        store: ConfigStore,
        policy: AutoApprovalPolicy,
    ) -> None:
        store.set(
            ProjectConfig(
                cwd="/proj",
                enabled=True,
                timeout_ms=5000,
            ),
        )
        decision = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=False,
        )
        assert decision.timeout_ms == 5000


# ---------- 公共接口 ----------


class TestPublicApi:
    def test_set_enabled_persists(self, store: ConfigStore, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        assert policy.is_enabled_for("/proj") is True
        # 重新构造 policy 仍能读到（已落盘）
        policy2 = AutoApprovalPolicy(load_default_rules(), store)
        assert policy2.is_enabled_for("/proj") is True

    def test_toggle_off(self, policy: AutoApprovalPolicy) -> None:
        policy.set_enabled("/proj", True)
        policy.set_enabled("/proj", False)
        assert policy.is_enabled_for("/proj") is False

    def test_decision_dataclass_frozen(self) -> None:
        d = Decision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10000)
        with pytest.raises(Exception):
            d.auto_eligible = False  # type: ignore[misc]


# ---------- 实时反映 config 变化 ----------


class TestRuntimeConfigReload:
    def test_classify_reads_latest_config_each_call(
        self,
        policy: AutoApprovalPolicy,
    ) -> None:
        """policy 不缓存 cwd 配置——每次 classify 重新读盘。"""
        # 第一次：未启用
        d1 = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=False,
        )
        assert d1.auto_eligible is False

        # toggle 开
        policy.set_enabled("/proj", True)

        # 第二次：已启用
        d2 = policy.classify(
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/proj",
            is_elevated=False,
        )
        assert d2.auto_eligible is True
