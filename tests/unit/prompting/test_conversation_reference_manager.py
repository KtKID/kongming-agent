"""unit：ConversationReferenceManager skill resolver。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.message import Message
from prompting.context_sources.conversation_reference_manager import (
    ConversationReferenceContext,
    ConversationReferenceManager,
)


@pytest.mark.asyncio
async def test_resolve_skill_reference_injects_skill_path_marker(tmp_path: Path) -> None:
    """skill reference 通过服务端 loader 注入 SKILL.md 路径 marker。"""
    _write_skill(tmp_path, "skill-creator", "Create a new skill.")
    manager = ConversationReferenceManager(
        ConversationReferenceContext(home=tmp_path / "home", workspace=tmp_path / "workspace")
    )
    message = Message.user(
        "如何设计这个 skill",
        metadata={
            "conversation_references": [
                {
                    "id": "ref-1",
                    "kind": "skill",
                    "ref": "skill:skill-creator",
                    "label": "Skill Creator",
                    "activation": "inject_context",
                    "source_ref": "skill:home:skill-creator",
                }
            ]
        },
    )

    resolved = await manager.resolve_for_prompt(message)
    context = manager.render_prompt_context(resolved)

    assert len(resolved) == 1
    assert resolved[0].label == "Skill Creator"
    assert context.startswith("[$skill-creator](")
    assert "SKILL.md" in context
    assert "Create a new skill." not in context


@pytest.mark.asyncio
async def test_missing_skill_reference_returns_diagnostic(tmp_path: Path) -> None:
    """缺失 skill 输出诊断片段，便于 prompt debug 定位。"""
    manager = ConversationReferenceManager(
        ConversationReferenceContext(home=tmp_path / "home", workspace=tmp_path / "workspace")
    )
    message = Message.user(
        "hello",
        metadata={
            "conversation_references": [
                {
                    "id": "ref-missing",
                    "kind": "skill",
                    "ref": "skill:missing",
                    "label": "Missing Skill",
                    "activation": "inject_context",
                }
            ]
        },
    )

    resolved = await manager.resolve_for_prompt(message)
    context = manager.render_prompt_context(resolved)

    assert len(resolved) == 1
    assert "selected_reference_error" in context
    assert "skill not found: missing" in context


def _write_skill(tmp_path: Path, name: str, body: str) -> None:
    """写入 home skill，输入为名称和正文，输出为 SKILL.md 文件。"""
    skill_dir = tmp_path / "home" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
