"""unit：core.agent_spec.AgentSpec __post_init__ 校验分支。

B7 / CR 报告 cr-report-20260424-202744.md。
"""

from __future__ import annotations

import pytest

from core.agent_spec import AgentSpec


def test_valid_agent_spec():
    spec = AgentSpec(
        name="agent",
        instructions="hi",
        default_model="gpt-4",
    )
    assert spec.name == "agent"
    assert spec.max_turns == 10  # 默认值
    assert spec.tool_names == ()
    assert spec.reasoning_effort is None


def test_max_turns_zero_rejected():
    with pytest.raises(ValueError, match="max_turns must be positive"):
        AgentSpec(name="a", instructions="i", default_model="m", max_turns=0)


def test_max_turns_negative_rejected():
    with pytest.raises(ValueError, match="max_turns must be positive"):
        AgentSpec(name="a", instructions="i", default_model="m", max_turns=-1)


def test_empty_name_rejected():
    with pytest.raises(ValueError, match="name must not be empty"):
        AgentSpec(name="", instructions="i", default_model="m")


def test_empty_default_model_rejected():
    with pytest.raises(ValueError, match="default_model must not be empty"):
        AgentSpec(name="a", instructions="i", default_model="")


def test_with_reasoning_effort():
    spec = AgentSpec(
        name="a",
        instructions="i",
        default_model="m",
        reasoning_effort="high",
    )
    assert spec.reasoning_effort == "high"


def test_with_tool_names_and_metadata():
    spec = AgentSpec(
        name="a",
        instructions="i",
        default_model="m",
        tool_names=("read_file", "run_shell"),
        metadata={"version": "v1"},
    )
    assert spec.tool_names == ("read_file", "run_shell")
    assert spec.metadata == {"version": "v1"}


def test_frozen_dataclass_immutable():
    """AgentSpec 是 frozen dataclass，无法就地修改字段。"""
    from dataclasses import FrozenInstanceError

    spec = AgentSpec(name="a", instructions="i", default_model="m")
    with pytest.raises(FrozenInstanceError):
        spec.name = "b"  # type: ignore[misc]
