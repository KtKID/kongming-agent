"""skill-assemble-v0.1.6 单测：验证 skill listing 真的进了 system prompt。

覆盖：
- listing 文本经 ``_assemble_instructions`` 拼到 rendered；origins 含 "skills"。
- 空 listing（无 skill 装载）保持 v0.1.5 行为，origins 不含 "skills"。
- 真实文件路径走通：临时 ``.kongming/skills/<name>/SKILL.md`` →
  ``load_skill_specs`` → ``format_skill_listing`` → ``_assemble_instructions``
  端到端拿到含 demo skill 的 prompt。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hosts.cli.main as cli_main
from application.agent_workflows.prompt_catalog import WorkflowPromptListingRender
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from prompting.skills.skill_loader import format_skill_listing, load_skill_specs


def _build_cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory"),
        trace=TraceConfig(output_path=".kongming/traces/test.jsonl"),
        approval=ApprovalConfig(mode="auto_allow"),
        # memory 关闭：聚焦 skill 通道，避免 memory 装配的副作用
        evolution=EvolutionConfig(memory=EvolutionMemoryConfig(enabled=False)),
    )


@pytest.mark.asyncio
async def test_assemble_instructions_appends_skill_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非空 ``skill_listing`` 时，rendered 末尾追加 ``# skills`` 段，origins 含 "skills"。"""
    monkeypatch.setattr(cli_main, "get_kongming_home", lambda: tmp_path)
    cfg = _build_cfg()
    listing = "- demo: 演示用 skill - 当用户说 demo 时触发"

    rendered, origins, memory_store = await cli_main._assemble_instructions(
        cfg,
        instructions_files=[],
        skill_listing=listing,
    )

    assert "# skills" in rendered
    assert listing in rendered
    assert "skills" in origins
    assert memory_store is None


@pytest.mark.asyncio
async def test_assemble_instructions_places_workflow_listing_before_dynamic_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """workflow listing 早于文件/env/skills，输入为三类动态来源，输出文本顺序断言。"""
    monkeypatch.setattr(cli_main, "get_kongming_home", lambda: tmp_path)
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "env instruction")
    extra_file = tmp_path / "rules.md"
    extra_file.write_text("file instruction", encoding="utf-8")
    cfg = _build_cfg()
    workflow_listing = WorkflowPromptListingRender(
        text="# workflow catalog\nworkflow instruction",
        origin="workflow_catalog",
        template_version="test-template",
        listing_hash="test-hash",
    )

    rendered, origins, memory_store = await cli_main._assemble_instructions(
        cfg,
        instructions_files=[extra_file],
        skill_listing="- demo: skill instruction",
        workflow_listing=workflow_listing,
    )

    assert "workflow_catalog" in origins
    assert "file:rules.md" in origins
    assert "env:KONGMING_EXTRA_INSTRUCTIONS" in origins
    assert "skills" in origins
    assert rendered.index("# workflow_catalog") < rendered.index("# file:rules.md")
    assert rendered.index("# workflow_catalog") < rendered.index(
        "# env:KONGMING_EXTRA_INSTRUCTIONS"
    )
    assert rendered.index("# workflow_catalog") < rendered.index("# skills")
    assert memory_store is None


@pytest.mark.asyncio
async def test_assemble_instructions_skips_empty_skill_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """空 listing 不动 v0.1.5 行为：origins 不含 "skills"，rendered 末尾无 skills 段。"""
    monkeypatch.setattr(cli_main, "get_kongming_home", lambda: tmp_path)
    cfg = _build_cfg()

    rendered, origins, _ = await cli_main._assemble_instructions(
        cfg,
        instructions_files=[],
        skill_listing="",
    )

    assert "# skills" not in rendered
    assert "skills" not in origins


@pytest.mark.asyncio
async def test_skill_loader_to_listing_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实路径：home/skills/<name>/SKILL.md 装载 → listing 拼到 prompt。"""
    home = tmp_path / "kongming_home"
    skill_dir = home / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: 演示用 skill\n"
        "when-to-use: 当用户说 demo 时触发\n"
        "---\n"
        "skill body content",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_main, "get_kongming_home", lambda: home)

    specs = await load_skill_specs(home, workspace=None, event_sinks=())
    listing = format_skill_listing(specs)
    cfg = _build_cfg()

    rendered, origins, _ = await cli_main._assemble_instructions(
        cfg,
        instructions_files=[],
        skill_listing=listing,
    )

    assert len(specs) == 1
    assert specs[0].name == "demo"
    assert "- demo: 演示用 skill - 当用户说 demo 时触发" in rendered
    assert "skills" in origins
