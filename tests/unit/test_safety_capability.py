"""unit：safety.capability_policy 基本判定行为。"""

from __future__ import annotations

import pytest

from config_loader.models import (
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    ShellToolConfig,
    ToolConfig,
)
from safety.capability_policy import CapabilityPolicy, CapabilitySet


def _local_config(
    *,
    file_enabled: bool = True,
    shell_enabled: bool = True,
    memory_enabled: bool = True,
) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        tool=ToolConfig(
            file=FileToolConfig(enabled=file_enabled),
            shell=ShellToolConfig(enabled=shell_enabled),
        ),
        evolution=EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=memory_enabled),
        ),
    )


@pytest.mark.unit
async def test_empty_allow_means_allow_all() -> None:
    """allow 集合为空 → 全开（配合黑名单使用）。"""
    policy = CapabilityPolicy(CapabilitySet(allow=frozenset()))
    result = await policy.check("read_file", {})
    assert result.outcome == "allow"


@pytest.mark.unit
async def test_deny_takes_precedence_over_allow() -> None:
    policy = CapabilityPolicy(
        CapabilitySet(
            allow=frozenset({"file_read"}),
            deny=frozenset({"file_read"}),
        )
    )
    result = await policy.check("read_file", {})
    assert result.outcome == "deny"


@pytest.mark.unit
async def test_capability_not_in_allow_is_denied() -> None:
    policy = CapabilityPolicy(CapabilitySet(allow=frozenset({"file_read"})))
    # run_shell 对应 capability=shell，不在 allow 中
    result = await policy.check("run_shell", {})
    assert result.outcome == "deny"


@pytest.mark.unit
async def test_capability_in_allow_is_allowed() -> None:
    policy = CapabilityPolicy(CapabilitySet(allow=frozenset({"file_read"})))
    result = await policy.check("read_file", {})
    assert result.outcome == "allow"


@pytest.mark.unit
async def test_unknown_tool_name_falls_back_to_tool_name_as_capability() -> None:
    """未登记的 tool_name，capability 就是 tool_name 本身。"""
    policy = CapabilityPolicy(CapabilitySet(allow=frozenset({"custom_cap"})))
    result = await policy.check("custom_cap", {})
    assert result.outcome == "allow"


@pytest.mark.unit
async def test_from_config_includes_file_and_shell_when_enabled() -> None:
    policy = CapabilityPolicy.from_config(_local_config())
    r1 = await policy.check("read_file", {})
    r2 = await policy.check("run_shell", {})
    assert r1.outcome == "allow"
    assert r2.outcome == "allow"


@pytest.mark.unit
async def test_from_config_excludes_shell_when_disabled() -> None:
    policy = CapabilityPolicy.from_config(_local_config(shell_enabled=False))
    result = await policy.check("run_shell", {})
    assert result.outcome == "deny"


@pytest.mark.unit
async def test_from_config_includes_memory_when_enabled() -> None:
    """evolution.memory.enabled=True 时 capability 层必须允许 memory 工具通过；
    否则 memory tool 永远被 capability 拦截、无法触达 permission → approval 层。"""
    policy = CapabilityPolicy.from_config(_local_config(memory_enabled=True))
    result = await policy.check("memory", {"action": "add", "target": "memory"})
    assert result.outcome == "allow"
    assert result.capability == "memory"


@pytest.mark.unit
async def test_from_config_excludes_memory_when_disabled() -> None:
    """evolution.memory.enabled=False 时 memory capability 不进 allow 集合。
    此时即使 cli 注册了 memory tool（异常路径），也会被 capability 拦。"""
    policy = CapabilityPolicy.from_config(_local_config(memory_enabled=False))
    result = await policy.check("memory", {"action": "view", "target": "memory"})
    assert result.outcome == "deny"


@pytest.mark.unit
async def test_capability_check_includes_reason() -> None:
    policy = CapabilityPolicy(CapabilitySet(allow=frozenset({"file_read"})))
    result = await policy.check("read_file", {})
    assert result.reason != ""
    assert result.capability == "file_read"
