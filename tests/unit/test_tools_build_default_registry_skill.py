"""unit：``tools.build_default_registry`` 的 skill_specs 装配分支（v0.1.6 #4）。

覆盖三条契约：
- ``skill_specs=None`` 不注册 Skill 工具（保持 v0.1.5 行为）。
- ``skill_specs={...}`` 非空 dict 注册 ``SkillTool``，name="skill"。
- 装配出来的 SkillTool 能被 ``ToolRegistry.get/__getitem__`` 正确分发。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tools import build_default_registry
from tools.builtin.skill_tool import SkillTool


@dataclass(frozen=True)
class _FakeSkillSpec:
    """``_SkillSpecLike`` 结构兼容的最小 fixture。"""

    name: str
    source: str
    body_path: Path


@pytest.mark.unit
def test_build_default_registry_skill_specs_none_does_not_register() -> None:
    reg = build_default_registry(
        file_enabled=False,
        shell_enabled=False,
        skill_specs=None,
    )
    assert "skill" not in reg


@pytest.mark.unit
def test_build_default_registry_skill_specs_empty_does_not_register(
    tmp_path: Path,
) -> None:
    """空 dict 视为未启用 skill 系统，不注册（避免暴露空工具给模型）。"""
    reg = build_default_registry(
        file_enabled=False,
        shell_enabled=False,
        skill_specs={},
    )
    assert "skill" not in reg


@pytest.mark.unit
def test_build_default_registry_skill_specs_dict_registers(tmp_path: Path) -> None:
    spec_path = tmp_path / "demo.md"
    spec_path.write_text("hello world", encoding="utf-8")

    specs = {
        "demo": _FakeSkillSpec(name="demo", source="workspace", body_path=spec_path),
    }

    reg = build_default_registry(
        file_enabled=False,
        shell_enabled=False,
        skill_specs=specs,
    )

    assert "skill" in reg
    tool = reg["skill"]
    assert isinstance(tool, SkillTool)
    assert tool.name == "skill"


@pytest.mark.unit
def test_build_default_registry_skill_dispatch_via_registry(tmp_path: Path) -> None:
    """ToolRegistry.get('skill') 命中后能找到同一对象引用。"""
    spec_path = tmp_path / "x.md"
    spec_path.write_text("body", encoding="utf-8")
    specs = {"x": _FakeSkillSpec(name="x", source="workspace", body_path=spec_path)}

    reg = build_default_registry(
        file_enabled=False,
        shell_enabled=False,
        skill_specs=specs,
    )
    via_get = reg.get("skill")
    via_getitem = reg["skill"]
    assert via_get is via_getitem
    assert isinstance(via_get, SkillTool)
