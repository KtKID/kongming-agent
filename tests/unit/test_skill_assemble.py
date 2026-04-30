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

import cli.main as cli_main
from config_loader.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from context.skill_loader import format_skill_listing, load_skill_specs


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
