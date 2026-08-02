"""AutoApprovalPolicy 的 default ask 模式配置合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hosts.web.approvals.auto.config_store import ConfigStore, ProjectConfig
from hosts.web.approvals.auto.policy import AutoApprovalPolicy
from hosts.web.approvals.auto.rules import load_default_rules
from safety.auto_approval.disposition import ApprovalDispositionMode


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "auto_approval")


@pytest.fixture
def policy(store: ConfigStore) -> AutoApprovalPolicy:
    return AutoApprovalPolicy(load_default_rules(), store)


def test_new_cwd_defaults_to_user_mode(policy: AutoApprovalPolicy) -> None:
    config = policy.get_config("/workspace")
    assert config.mode is ApprovalDispositionMode.USER
    assert policy.mode_for("/workspace") is ApprovalDispositionMode.USER


def test_set_mode_persists(store: ConfigStore, policy: AutoApprovalPolicy) -> None:
    updated = policy.set_mode("/workspace", ApprovalDispositionMode.LLM)
    assert updated.mode is ApprovalDispositionMode.LLM
    assert updated.cwd == "/workspace"

    reloaded = AutoApprovalPolicy(load_default_rules(), store)
    assert reloaded.mode_for("/workspace") is ApprovalDispositionMode.LLM


def test_mode_can_return_to_user(policy: AutoApprovalPolicy) -> None:
    policy.set_mode("/workspace", ApprovalDispositionMode.FULL_TRUST)
    policy.set_mode("/workspace", ApprovalDispositionMode.USER)
    assert policy.mode_for("/workspace") is ApprovalDispositionMode.USER


def test_get_config_reads_latest_store_value(
    store: ConfigStore,
    policy: AutoApprovalPolicy,
) -> None:
    store.set(
        ProjectConfig(
            cwd="/workspace",
            mode=ApprovalDispositionMode.LLM,
            timeout_ms=4_000,
        )
    )
    assert policy.get_config("/workspace").timeout_ms == 4_000

    store.set(
        ProjectConfig(
            cwd="/workspace",
            mode=ApprovalDispositionMode.LLM,
            timeout_ms=9_000,
        )
    )
    assert policy.get_config("/workspace").timeout_ms == 9_000


def test_policy_exposes_mode_defaults(policy: AutoApprovalPolicy) -> None:
    assert policy.rule_set.default_timeout_ms == 10_000


def test_policy_has_no_rule_decision_api() -> None:
    assert not hasattr(AutoApprovalPolicy, "classify")
