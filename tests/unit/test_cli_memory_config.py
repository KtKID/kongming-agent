"""cli/main.py 侧 memory 配置消费单测。

覆盖：
- `cfg.evolution.memory.enabled=False` 时 `_assemble_instructions` 返回 memory_store=None
- `inject_prompt=False` 时仍加载活态 entries，但不向 sources 追加 memory block
- `root_path` 绝对路径直接作为 memory_dir（不再拼 `.kongming/memory`）
- `root_path` 相对路径与 cwd 拼接
- `view_max_chars` 透传给 MemoryTool
- `read_max_chars` 生效于 MemoryStore 文件读取

这些测试直接驱动 ``cli.main._assemble_instructions`` 以及相关子函数，
不走完整 CLI run loop。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import main as cli_main
from config_loader.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    ModelConfig,
    RunnerConfig,
)
from memory import MemoryStore
from tools.builtin.memory_tool import MemoryTool, build_memory_tool

# 默认 AGENT 模板内容（来自源模板文件）。断言"agent 段落已被拼入 rendered/text"
# 用此内容做真值，避免与模板字面字符串硬编码耦合（模板内容可能后续被改写）。
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "prompts" / "templates"
_DEFAULT_AGENT_TEXT = (_TEMPLATES_DIR / "AGENT.md").read_text(encoding="utf-8").strip()


def _build_cfg(memory: EvolutionMemoryConfig | None = None) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        approval=ApprovalConfig(mode="auto_allow"),
        evolution=EvolutionConfig(memory=memory) if memory is not None else EvolutionConfig(),
    )


# ---------------------------------------------------------------------------
# A1 + A3: enabled / inject_prompt
# ---------------------------------------------------------------------------


async def test_enabled_false_skips_memory_tool_registration(tmp_path: Path) -> None:
    """enabled=False → _assemble_instructions 返回 memory_store=None，
    origins 不含 "memory"。"""
    cfg = _build_cfg(
        EvolutionMemoryConfig(
            enabled=False,
            root_path=str(tmp_path / "memory-not-used"),
        )
    )
    rendered, origins, memory_store = await cli_main._assemble_instructions(cfg, [])

    assert memory_store is None
    assert "memory" not in origins
    # 默认 agent instructions 段落仍被拼入（实际内容来自 AGENT.md 模板）
    assert _DEFAULT_AGENT_TEXT in rendered


async def test_inject_prompt_false_keeps_entries_but_no_prompt_block(tmp_path: Path) -> None:
    """inject_prompt=False → memory_store 仍创建并加载磁盘内容（活态 entries 可用），
    但 origins/rendered 不包含 memory block。"""
    mem_dir = tmp_path / ".kongming" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("user prefers typescript", encoding="utf-8")

    cfg = _build_cfg(
        EvolutionMemoryConfig(
            enabled=True,
            inject_prompt=False,
            root_path=str(mem_dir),
        )
    )
    rendered, origins, memory_store = await cli_main._assemble_instructions(cfg, [])

    assert memory_store is not None
    assert "memory" not in origins
    # 活态 entries 仍然加载
    assert len(memory_store.memory_entries) == 1
    assert "typescript" in memory_store.memory_entries[0].content
    # rendered 不含 memory 内容
    assert "user prefers typescript" not in rendered


# ---------------------------------------------------------------------------
# A2: root_path 语义
# ---------------------------------------------------------------------------


async def test_root_path_absolute_used_as_memory_dir(tmp_path: Path) -> None:
    """绝对 root_path 直接作为 memory_dir，不再拼 .kongming/memory。"""
    mem_dir = tmp_path / "custom-memory-root"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("absolute path works", encoding="utf-8")

    cfg = _build_cfg(
        EvolutionMemoryConfig(
            enabled=True,
            root_path=str(mem_dir),
        )
    )
    _, _, memory_store = await cli_main._assemble_instructions(cfg, [])
    assert memory_store is not None
    assert memory_store.memory_dir == mem_dir.resolve()
    assert any("absolute path works" in e.content for e in memory_store.memory_entries)


async def test_root_path_relative_joined_with_cwd(tmp_path: Path, monkeypatch) -> None:
    """相对 root_path 视为 cwd 子目录。"""
    # 用 tmp_path 当 cwd，避免污染真实项目目录
    monkeypatch.chdir(tmp_path)
    rel = "memory-rel"
    (tmp_path / rel).mkdir()
    (tmp_path / rel / "MEMORY.md").write_text("relative path works", encoding="utf-8")

    cfg = _build_cfg(
        EvolutionMemoryConfig(
            enabled=True,
            root_path=rel,
        )
    )
    _, _, memory_store = await cli_main._assemble_instructions(cfg, [])
    assert memory_store is not None
    assert memory_store.memory_dir == (tmp_path / rel).resolve()


# ---------------------------------------------------------------------------
# A4: view_max_chars 透传
# ---------------------------------------------------------------------------


async def test_view_max_chars_passed_to_tool(tmp_path: Path) -> None:
    """build_memory_tool 应用 cfg 的 view_max_chars。"""
    cfg = _build_cfg(
        EvolutionMemoryConfig(
            enabled=True,
            root_path=str(tmp_path / ".kongming" / "memory"),
            view_max_chars=111,
        )
    )
    _, _, memory_store = await cli_main._assemble_instructions(cfg, [])
    assert memory_store is not None

    tool = build_memory_tool(
        memory_store,
        view_max_chars=cfg.evolution.memory.view_max_chars,
    )
    assert isinstance(tool, MemoryTool)
    assert tool._view_max_chars == 111  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# A5: read_max_chars 截断
# ---------------------------------------------------------------------------


async def test_read_max_chars_truncates_big_memory_file(tmp_path: Path) -> None:
    """超出 read_max_chars 的内容会被截断。"""
    mem_dir = tmp_path / ".kongming" / "memory"
    mem_dir.mkdir(parents=True)
    big_content = "x" * 10_000
    (mem_dir / "MEMORY.md").write_text(big_content, encoding="utf-8")

    store = MemoryStore(memory_dir=mem_dir, read_max_chars=1024)
    snap = await store.load_from_disk()
    assert snap is not None
    assert len(snap.memory_text) == 1024

    # 无截断基线：read_max_chars 足够大时拿到完整文本
    store_full = MemoryStore(memory_dir=mem_dir, read_max_chars=20_000)
    snap_full = await store_full.load_from_disk()
    assert len(snap_full.memory_text) == 10_000


# ---------------------------------------------------------------------------
# A6 / A7 / A8: 文案落实（保险：防回退）
# ---------------------------------------------------------------------------


def test_memory_tool_description_has_dont_use_write_file_hint() -> None:
    """MemoryTool.description 必须明确引导 Agent 走 memory 而不是 write_file。"""
    from tools.builtin.memory_tool import MemoryTool

    desc = MemoryTool.description
    assert "不要用 write_file" in desc or "INSTEAD" in desc
    # 中文关键词要保住
    assert "跨会话" in desc or "长期记忆" in desc


async def test_default_agent_instructions_mentions_memory_tool(tmp_path) -> None:
    """默认 agent instructions 必须提醒使用 memory tool，而不是 write_file。

    v0.1.3 prompt-modules 改动后，默认内容来自物化的
    `.kongming/prompts/{AGENT,TOOLS,USER}.md` 模板，而不再是 Python 常量。
    这里让装配器把 tmp_path 当 home，跑一次启动物化 + 读取，断言关键中文
    关键词仍然在渲染结果里。
    """
    from prompting.instructions.prompts_loader import materialize_and_load_prompts

    text = await materialize_and_load_prompts(tmp_path)
    assert "memory" in text
    assert "write_file" in text  # 显式反面警示
    # AGENT 段落实际内容（来自模板文件）已被拼入
    assert _DEFAULT_AGENT_TEXT in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
