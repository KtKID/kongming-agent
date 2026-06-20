"""系统指令 / 规则 / 静态说明加载器。

v1-mini 的职责边界：

- 接 agent_spec 的 ``instructions`` 字段
- 接可选的额外 markdown 文件（由装配层显式传入）
- 接一个预留的环境变量 ``KONGMING_EXTRA_INSTRUCTIONS``

**不做**的事：

- 不读 ``.claude/CLAUDE.md`` / ``.cursorrules`` 等 coding-agent 专有文件——v1-mini
  定位是宿主无关通用 agent，coding project 语义是 v0.2+ 的事（参考
  ``docs/kongming-agent-v1-minimal/11-v1-file-layout.md`` 里对 ``instruction_loader``
  的说明）
- 不做 skill / tool-specific instruction 分层——那是 ``skill_loader.py`` 的事，明确
  留到 v0.2+（11-v1-file-layout.md 已列出 not-in-v1 的 ``prompting/skill_loader.py``）

渲染策略：

- 多来源合并为一段文本，每段前加 ``# <origin>`` 标注，便于排查"这条规则是哪里来的"
- 空白 / 空文件忽略，不产出空段
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstructionSource:
    """单条指令来源记录。

    Attributes:
        origin: 来源标识，例如 ``"agent_spec"`` / ``"file:rules.md"`` /
            ``"env:KONGMING_EXTRA_INSTRUCTIONS"``。渲染和排查时都靠它。
        content: 原始文本，不做语义解析。
    """

    origin: str
    content: str


_ENV_VAR_NAME = "KONGMING_EXTRA_INSTRUCTIONS"


class InstructionLoader:
    """加载并渲染多来源系统指令。

    设计要点：

    - 构造时只存额外文件路径和开关，实际 I/O 推迟到 :meth:`load` 的 async 调用
    - 文件读取走 ``asyncio.to_thread``，同当前项目的其它 I/O 约定一致
    - :meth:`render` 是纯函数（同步），方便上层在已经拿到 sources 后直接拼字符串
    """

    def __init__(
        self,
        *,
        extra_files: Sequence[str | Path] = (),
        include_env: bool = True,
        env_var_name: str = _ENV_VAR_NAME,
    ) -> None:
        """初始化。

        Args:
            extra_files: 额外的 markdown / 文本文件列表，按顺序追加到 sources。
                不存在的文件会被静默跳过（第一版不做强制存在校验）。
            include_env: 是否读取环境变量作为一个来源；测试或需要隔离环境时可关闭。
            env_var_name: 环境变量名，默认 ``KONGMING_EXTRA_INSTRUCTIONS``。
        """
        self._extra_files: list[Path] = [Path(p) for p in extra_files]
        self._include_env: bool = include_env
        self._env_var_name: str = env_var_name

    async def load(self, agent_instructions: str | None) -> list[InstructionSource]:
        """收集所有来源的指令。

        Args:
            agent_instructions: 来自 :class:`core.agent_spec.AgentSpec` 的 instructions
                字段；为空或空白时跳过该来源。

        Returns:
            按 ``agent_spec → files → env`` 顺序的 :class:`InstructionSource` 列表。
        """
        sources: list[InstructionSource] = []

        if agent_instructions and agent_instructions.strip():
            sources.append(InstructionSource(origin="agent_spec", content=agent_instructions))

        for path in self._extra_files:
            content = await asyncio.to_thread(self._read_if_exists, path)
            if content is not None and content.strip():
                sources.append(InstructionSource(origin=f"file:{path.name}", content=content))

        if self._include_env:
            env_val = os.environ.get(self._env_var_name)
            if env_val and env_val.strip():
                sources.append(
                    InstructionSource(
                        origin=f"env:{self._env_var_name}",
                        content=env_val,
                    )
                )

        return sources

    @staticmethod
    def _read_if_exists(path: Path) -> str | None:
        """存在则读，不存在静默返回 ``None``。

        缺失处理策略（v0.1.2 契约）：

        - ``OSError``（权限不足、设备错误等 I/O 异常）→ 静默跳过，返回 ``None``
        - ``UnicodeDecodeError``（文件存在但不是合法 UTF-8）→ **冒泡**，调用方不应
          把非法编码文件当成空指令吞掉，需要明确失败以便排查配置问题
        """
        try:
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def render(sources: Sequence[InstructionSource]) -> str:
        """把多来源指令合并成一段 system 文本，带 origin 标注。

        空 list 返回空字符串；单来源也带标注，保持格式统一。
        """
        parts: list[str] = []
        for source in sources:
            content = source.content.strip()
            if not content:
                continue
            # origin="" 表示"透传"：内容已预格式化，不加 `# \n` 前缀
            if source.origin:
                parts.append(f"# {source.origin}\n{content}")
            else:
                parts.append(content)
        return "\n\n".join(parts)


__all__ = [
    "InstructionLoader",
    "InstructionSource",
    "assemble_instructions",
    "load_instruction_sources",
]


async def load_instruction_sources(
    *,
    kongming_home: Path,
    extra_files: Sequence[str | Path] = (),
    pre_file_sources: Sequence[InstructionSource] = (),
    cwd: Path | None = None,
    sitian_root: Path | None = None,
) -> list[InstructionSource]:
    """Load ordered instruction sources from prompts + extra files + env + runtime context text.

    Shared by CLI and web host. Extracts the core instruction assembly logic
    (prompts materialization, InstructionLoader, runtime context injection)
    without host-specific concerns like memory loading.

    Args:
        kongming_home: Path to the ``.kongming/`` directory.
        extra_files: Additional instruction file paths (e.g. from CLI ``--instructions-file``).
        pre_file_sources: Instruction sources injected after runtime context and before
            materialized prompts, extra files, env, and optional context sources.
        cwd: Working directory for runtime context text. Defaults to ``Path.cwd()``.
        sitian_root: Optional sitian project root. When set, appends sitian context
            (core-flow, suggestions, etc.) as an ``"sitian"`` origin instruction source.

    Returns:
        Ordered instruction sources ready for rendering.
    """
    from prompting.assembly.runtime_context import build_runtime_context_text
    from prompting.instructions.prompts_loader import materialize_and_load_prompts

    base = await materialize_and_load_prompts(kongming_home)

    loader = InstructionLoader(extra_files=extra_files, include_env=True)
    runtime_text = build_runtime_context_text(
        cwd=cwd or Path.cwd(),
        kongming_home=kongming_home,
    )
    sources = [InstructionSource(origin="runtime", content=runtime_text)]
    sources.extend(source for source in pre_file_sources if source.content.strip())
    sources.extend(await loader.load(agent_instructions=base))

    if sitian_root is not None:
        from prompting.context_sources.sitian_context import build_sitian_context_text

        sitian_text = build_sitian_context_text(sitian_root)
        if sitian_text:
            sources.append(InstructionSource(origin="sitian", content=sitian_text))

    return sources


async def assemble_instructions(
    *,
    kongming_home: Path,
    extra_files: Sequence[str | Path] = (),
    pre_file_sources: Sequence[InstructionSource] = (),
    cwd: Path | None = None,
    sitian_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Assemble base agent instructions from prompts + extra files + env + runtime context text.

    Shared by CLI and web host. Extracts the core instruction assembly logic
    (prompts materialization, InstructionLoader, runtime context injection)
    without host-specific concerns like memory loading.

    Args:
        kongming_home: Path to the ``.kongming/`` directory.
        extra_files: Additional instruction file paths (e.g. from CLI ``--instructions-file``).
        pre_file_sources: Instruction sources injected after runtime context and before
            materialized prompts, extra files, env, and optional context sources.
        cwd: Working directory for runtime context text. Defaults to ``Path.cwd()``.
        sitian_root: Optional sitian project root. When set, appends sitian context
            (core-flow, suggestions, etc.) as an ``"sitian"`` origin instruction source.

    Returns:
        ``(rendered_text, origins)`` — merged system prompt text and origin label list.
    """
    loader = InstructionLoader(extra_files=extra_files, include_env=True)
    sources = await load_instruction_sources(
        kongming_home=kongming_home,
        extra_files=extra_files,
        pre_file_sources=pre_file_sources,
        cwd=cwd,
        sitian_root=sitian_root,
    )
    rendered = loader.render(sources)
    origins = [s.origin for s in sources]
    return rendered, origins
