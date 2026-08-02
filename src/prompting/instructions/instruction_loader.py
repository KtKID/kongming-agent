"""系统指令 / 规则 / 动态能力说明加载器。

本脚本负责统一装配父 LLM 的 system prompt。关键执行流程：物化基础
prompts → 注入 runtime → 注入 workflow catalog → 读取额外文件与环境变量 →
注入可选 sitian → 注入 skills 与 memory。

关键函数：``load_instruction_sources`` 收集结构化来源，``assemble_instructions``
返回最终文本与 origin 列表，``InstructionLoader.render`` 负责稳定渲染。
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

    属性：
        origin: 来源标识，例如 ``"agent_spec"`` / ``"file:rules.md"`` /
            ``"env:KONGMING_EXTRA_INSTRUCTIONS"``。渲染和排查时都靠它。
        content: 原始文本，不做语义解析。
    """

    origin: str
    content: str


_ENV_VAR_NAME = "KONGMING_EXTRA_INSTRUCTIONS"
_SKILLS_ORIGIN = "skills"
_MEMORY_ORIGIN = "memory"


class InstructionLoader:
    """加载并渲染多来源系统指令。

    设计要点：

    - 构造时只存额外文件路径和开关，实际 I/O 推迟到 :meth:`load` 的 async 调用
    - 文件读取走 ``asyncio.to_thread``，同当前项目的其它 I/O 约定一致
    - :meth:`render` 是纯函数（同步），方便上层在已经拿到来源列表后直接拼字符串
    """

    def __init__(
        self,
        *,
        extra_files: Sequence[str | Path] = (),
        include_env: bool = True,
        env_var_name: str = _ENV_VAR_NAME,
    ) -> None:
        """初始化。

        参数：
            extra_files: 额外的 Markdown / 文本文件列表，按顺序追加到来源列表。
                不存在的文件会被静默跳过（第一版不做强制存在校验）。
            include_env: 是否读取环境变量作为一个来源；测试或需要隔离环境时可关闭。
            env_var_name: 环境变量名，默认 ``KONGMING_EXTRA_INSTRUCTIONS``。
        """
        self._extra_files: list[Path] = [Path(p) for p in extra_files]
        self._include_env: bool = include_env
        self._env_var_name: str = env_var_name

    async def load(self, agent_instructions: str | None) -> list[InstructionSource]:
        """收集所有来源的指令。

        参数：
            agent_instructions: 来自 :class:`core.agent_spec.AgentSpec` 的 instructions
                字段；为空或空白时跳过该来源。

        返回：
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
        """把多来源指令合并成一段系统文本，带 origin 标注。

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


def _source_from_text(origin: str, content: str | None) -> InstructionSource | None:
    """把非空文本转成指令来源，输入为 origin 与文本，输出可选 source。"""
    if content and content.strip():
        return InstructionSource(origin=origin, content=content)
    return None


def _memory_source(
    *,
    memory_store: object | None,
    inject_memory: bool,
) -> InstructionSource | None:
    """从已加载 memory store 生成来源，输入为 store 与注入开关，输出可选 source。"""
    if not inject_memory or memory_store is None:
        return None
    snapshot = getattr(memory_store, "snapshot", None)
    if snapshot is None:
        return None
    render_prompt = getattr(snapshot, "render_prompt", None)
    if not callable(render_prompt):
        return None
    prompt = render_prompt()
    return _source_from_text(_MEMORY_ORIGIN, prompt)


async def load_instruction_sources(
    *,
    kongming_home: Path,
    extra_files: Sequence[str | Path] = (),
    pre_file_sources: Sequence[InstructionSource] = (),
    workflow_catalog: str = "",
    skill_listing: str = "",
    memory_store: object | None = None,
    inject_memory: bool = False,
    cwd: Path | str | None = None,
    sitian_root: Path | None = None,
) -> list[InstructionSource]:
    """按顺序加载提示模板、动态能力说明、记忆和运行时上下文等指令来源。

    CLI 和 Web 宿主共享该装配逻辑：物化提示模板，调用 InstructionLoader，
    注入运行时上下文，统一收口 workflow、skills、memory 三类动态来源。

    参数：
        kongming_home: ``.kongming/`` 目录路径。
        extra_files: 额外指令文件路径，例如 CLI 的 ``--instructions-file``。
        pre_file_sources: 注入到运行时上下文之后、物化提示模板 / 额外文件 / 环境变量 /
            可选上下文来源之前的指令来源。
        workflow_catalog: 已由 workflow manager/formatter 生成的短 listing。
        skill_listing: 已由 skill loader 生成的短 listing。
        memory_store: 已加载的 MemoryStore 或兼容对象。
        inject_memory: 是否把 memory 冻结快照注入 prompt。
        cwd: 构建运行时上下文文本时使用的工作目录，默认 ``Path.cwd()``；
            字符串输入会原样进入渲染后的运行时提示。
        sitian_root: 可选的司天项目根目录；传入后追加 core-flow、suggestions 等司天
            上下文，来源标识为 ``"sitian"``。

    返回：
        已按渲染顺序排列的指令来源列表。
    """
    from prompting.assembly.runtime_context import build_runtime_context_text
    from prompting.instructions.prompts_loader import materialize_and_load_prompts

    base = await materialize_and_load_prompts(kongming_home)

    loader = InstructionLoader(extra_files=extra_files, include_env=True)
    runtime_cwd = Path(cwd) if isinstance(cwd, str) else (cwd or Path.cwd())
    runtime_text = build_runtime_context_text(
        cwd=runtime_cwd,
        kongming_home=kongming_home,
    )
    sources = [InstructionSource(origin="runtime", content=runtime_text)]
    sources.extend(source for source in pre_file_sources if source.content.strip())
    workflow_source = _source_from_text("workflow_catalog", workflow_catalog)
    if workflow_source is not None:
        sources.append(workflow_source)
    sources.extend(await loader.load(agent_instructions=base))

    if sitian_root is not None:
        from prompting.context_sources.sitian_context import build_sitian_context_text

        sitian_text = build_sitian_context_text(sitian_root)
        if sitian_text:
            sources.append(InstructionSource(origin="sitian", content=sitian_text))

    skill_source = _source_from_text(_SKILLS_ORIGIN, skill_listing)
    if skill_source is not None:
        sources.append(skill_source)

    memory_source = _memory_source(
        memory_store=memory_store,
        inject_memory=inject_memory,
    )
    if memory_source is not None:
        sources.append(memory_source)

    return sources


async def assemble_instructions(
    *,
    kongming_home: Path,
    extra_files: Sequence[str | Path] = (),
    pre_file_sources: Sequence[InstructionSource] = (),
    workflow_catalog: str = "",
    skill_listing: str = "",
    memory_store: object | None = None,
    inject_memory: bool = False,
    cwd: Path | str | None = None,
    sitian_root: Path | None = None,
) -> tuple[str, list[str]]:
    """把基础 agent 指令和动态来源装配为系统提示词文本和来源标签列表。

    CLI 和 Web 宿主共享该装配逻辑：物化提示模板，调用 InstructionLoader，
    注入运行时上下文，统一收口 workflow、skills、memory 三类动态来源。

    参数：
        kongming_home: ``.kongming/`` 目录路径。
        extra_files: 额外指令文件路径，例如 CLI 的 ``--instructions-file``。
        pre_file_sources: 注入到运行时上下文之后、物化提示模板 / 额外文件 / 环境变量 /
            可选上下文来源之前的指令来源。
        workflow_catalog: 已由 workflow manager/formatter 生成的短 listing。
        skill_listing: 已由 skill loader 生成的短 listing。
        memory_store: 已加载的 MemoryStore 或兼容对象。
        inject_memory: 是否把 memory 冻结快照注入 prompt。
        cwd: 构建运行时上下文文本时使用的工作目录，默认 ``Path.cwd()``；
            字符串输入会原样进入渲染后的运行时提示。
        sitian_root: 可选的司天项目根目录；传入后追加 core-flow、suggestions 等司天
            上下文，来源标识为 ``"sitian"``。

    返回：
        ``(rendered_text, origins)``，分别是合并后的系统提示词文本和来源标签列表。
    """
    loader = InstructionLoader(extra_files=extra_files, include_env=True)
    sources = await load_instruction_sources(
        kongming_home=kongming_home,
        extra_files=extra_files,
        pre_file_sources=pre_file_sources,
        workflow_catalog=workflow_catalog,
        skill_listing=skill_listing,
        memory_store=memory_store,
        inject_memory=inject_memory,
        cwd=cwd,
        sitian_root=sitian_root,
    )
    rendered = loader.render(sources)
    origins = [s.origin for s in sources]
    return rendered, origins
