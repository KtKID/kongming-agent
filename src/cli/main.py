"""kongming-agent CLI 入口。

这里只做四件事：

1. 用 :mod:`click` 解析命令行参数；
2. 调 :func:`config_loader.load_config` 拿统一配置；
3. 按 Config 装配 session 工厂 / trace sink / instructions 三条**可观测的**
   输入通道；
4. 通过 :meth:`executors.agent_runtime.native_runtime.NativeRuntime.build`
   装配 runtime，交给 :class:`host.session_bridge.SessionBridge` 跑交互循环。

**不做**：

- 不复制 provider / registry / approval / runner / safety 装配——全部走
  :meth:`NativeRuntime.build`。
- 不写死 model / api_key / base_url / max_turns / timeout——全部从 Config 拿。
- 不自持第二套 run loop。

同步入口 :func:`main`：click 不支持 native async，因此 ``main`` 是同步的，
内部通过 ``asyncio.run`` 驱动 async 主链路。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Literal

import click

from config_loader import Config, get_kongming_home, load_config
from config_loader.errors import ConfigLoadError, ConfigValidationError
from context import (
    InstructionLoader,
    InstructionSource,
    build_session,
    materialize_and_load_prompts,
)
from core.contracts import EventSink
from executors.agent_runtime.native_runtime import NativeRuntime
from host.cli_adapter import CLIAdapter, CLIEventSink
from host.session_bridge import SessionBridge
from memory import MemoryStore
from observability import JsonlTraceSink, PromptDebugDumpSink
from tools import build_default_approval, build_default_registry

_CLI_SESSION_ID_HEX_LEN = 12


def _generate_cli_session_id() -> str:
    """生成默认 CLI session id。"""
    return f"cli-{uuid.uuid4().hex[:_CLI_SESSION_ID_HEX_LEN]}"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="配置文件路径（缺省走 KONGMING_CONFIG 环境变量或 config/setting.yaml）",
)
@click.option(
    "--session-id",
    default=None,
    help="复用会话 ID（缺省随机生成一个 cli-<hex12>）",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="显示 runtime 事件进度（turn / tool / approval）",
)
@click.option(
    "--smoke",
    is_flag=True,
    default=False,
    help="最小 smoke：装配一轮 hello 验证 provider 可达，不进入交互",
)
@click.option(
    "--instructions-file",
    "instructions_files",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="额外的系统指令文件（markdown / 文本）。可重复指定。",
)
@click.option(
    "--no-trace",
    is_flag=True,
    default=False,
    help="关闭 JSONL trace 落盘（默认打开，写到 config.trace.output_path）",
)
@click.option(
    "--reasoning-effort",
    "reasoning_effort",
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    default=None,
    help="覆盖模型思考深度（low/medium/high）。仅对支持的 provider 生效（OpenAI o 系列、GLM-Z 等）。",
)
@click.option(
    "--show-reasoning",
    "show_reasoning",
    is_flag=True,
    default=None,
    help="每轮响应后在终端打印模型思考内容（覆盖 config.cli.show_reasoning）。",
)
@click.option(
    "--debug",
    "prompt_debug",
    is_flag=True,
    default=False,
    help="保存每轮 system prompt 和完整 history 到 .kongming/debug/。",
)
def main(
    config_path: Path | None,
    session_id: str | None,
    verbose: bool,
    smoke: bool,
    instructions_files: tuple[Path, ...],
    no_trace: bool,
    reasoning_effort: str | None,
    show_reasoning: bool | None,
    prompt_debug: bool,
) -> None:
    """kongming-agent CLI"""
    try:
        asyncio.run(
            _run(
                config_path=config_path,
                session_id=session_id,
                verbose=verbose,
                smoke=smoke,
                instructions_files=list(instructions_files),
                trace_enabled=not no_trace,
                reasoning_effort=reasoning_effort,
                show_reasoning=show_reasoning,
                prompt_debug=prompt_debug,
            )
        )
    except KeyboardInterrupt:
        # 顶层 Ctrl-C：干净退出，不打 traceback。
        raise SystemExit(130) from None


async def _run(
    *,
    config_path: Path | None,
    session_id: str | None,
    verbose: bool,
    smoke: bool,
    instructions_files: list[Path],
    trace_enabled: bool,
    reasoning_effort: str | None = None,
    show_reasoning: bool | None = None,
    prompt_debug: bool = False,
) -> None:
    cfg = _load_config_or_exit(config_path)

    # CLI 参数 --reasoning-effort 覆盖 config 文件里的设置。
    if reasoning_effort is not None:
        effort_typed: Literal["low", "medium", "high"] = reasoning_effort  # type: ignore[assignment]
        cfg = cfg.model_copy(
            update={"model": cfg.model.model_copy(update={"reasoning_effort": effort_typed})}
        )

    # --show-reasoning flag 覆盖 config.cli.show_reasoning；未传 flag 时沿用 config。
    effective_show_reasoning = (
        show_reasoning if show_reasoning is not None else cfg.cli.show_reasoning
    )

    adapter = CLIAdapter(verbose=verbose)

    # 事件 sink 装配：默认挂 JsonlTraceSink（--no-trace 可关）；verbose 或
    # show_reasoning 时挂 CLIEventSink。runner 会 fan-out 到 list 里所有 sink。
    event_sinks: list[EventSink] = []
    if trace_enabled:
        event_sinks.append(JsonlTraceSink(cfg.trace.output_path, auto_flush=cfg.trace.auto_flush))
    if verbose or effective_show_reasoning:
        event_sinks.append(CLIEventSink(verbose=verbose, show_reasoning=effective_show_reasoning))

    registry = build_default_registry(
        file_enabled=cfg.tool.file.enabled,
        shell_enabled=cfg.tool.shell.enabled,
        shell_timeout_seconds=cfg.tool.shell.timeout_seconds,
        shell_max_stream_bytes=cfg.tool.shell.max_stream_bytes,
        shell_terminate_grace_seconds=cfg.tool.shell.terminate_grace_seconds,
        file_read_max_bytes=cfg.tool.file.read_max_bytes,
    )

    # approval 按配置模式选：interactive 时让 adapter 提供 prompt_fn，
    # 其它模式（auto_allow / auto_deny）不需要 prompt_fn。
    prompt_fn = adapter.prompt_approval if cfg.approval.mode == "interactive" else None
    approval = build_default_approval(cfg.approval.mode, prompt_fn=prompt_fn)

    # instructions 装配：用 InstructionLoader 把 agent_spec 基础文本 + 外部文件
    # + KONGMING_EXTRA_INSTRUCTIONS 合成一段带来源标注的 system prompt。
    # memory 通道受 cfg.evolution.memory.enabled/inject_prompt 控制；
    # enabled=False 时 memory_store 为 None。
    # prompts 装配（.kongming/prompts/*.md 物化 + 读取）失败时给用户友好错误消息，
    # 对齐 _load_config_or_exit 的 UX；避免裸 traceback。
    try:
        instructions, instruction_origins, memory_store = await _assemble_instructions(
            cfg,
            instructions_files,
        )
    except (PermissionError, OSError, UnicodeDecodeError, FileNotFoundError) as exc:
        click.echo(
            f"[prompts] failed to load .kongming/prompts/ templates: {type(exc).__name__}: {exc}",
            err=True,
        )
        raise SystemExit(2) from exc

    # 仅当 memory 启用时才注册 memory tool + 装 MemoryRefreshSink。
    if memory_store is not None:
        from tools.memory_tool import build_memory_tool

        registry.register(
            build_memory_tool(
                memory_store,
                view_max_chars=cfg.evolution.memory.view_max_chars,
                event_sinks=event_sinks,
            )
        )

        # memory snapshot trace event（启动一次性快照捕获）
        _snap = getattr(memory_store, "snapshot", None)
        if _snap is not None and not _snap.is_empty:
            from core.contracts import Event

            for sink in event_sinks:
                await sink.emit(
                    Event(
                        kind="memory.snapshot.captured",
                        run_id="cli-init",
                        payload={
                            "checksum": _snap.checksum,
                            "memory_chars": len(_snap.memory_text),
                            "user_chars": len(_snap.user_text),
                            "source_paths": list(_snap.source_paths),
                        },
                    )
                )

        # 装配 MemoryRefreshSink：监听 history.compact → reload memory snapshot。
        # 类体由 host/memory_refresh_sink.py 提供（Agent 2 负责）。这里按能力探测
        # 方式 import，避免 Agent 2 未就绪时 CLI 启动失败。
        try:
            from host.memory_refresh_sink import MemoryRefreshSink
        except ImportError:
            # Agent 2 尚未落地时保持 CLI 可用；写入的 memory 不会自动刷新快照
            # 到下一轮 prompt（需要重启 CLI）。
            pass
        else:
            memory_refresh_sink = MemoryRefreshSink(
                memory_store=memory_store,
                downstream_sinks=list(event_sinks),
            )
            event_sinks = [*event_sinks, memory_refresh_sink]

    enabled_tool_names = registry.names()  # 含 memory 的工具列表（若启用）

    # session bootstrap：收集 CLI 阶段可得的稳定元数据，file backend 需要它。
    import hashlib
    import time

    from context import SessionBootstrap

    bootstrap = SessionBootstrap(
        agent_name="kongming-agent",
        model_name=cfg.model.name,
        instruction_sources=instruction_origins,
        instruction_text_hash=f"sha256:{hashlib.sha256(instructions.encode()).hexdigest()}",
        created_at=time.time(),
        cwd=str(Path.cwd()),
        app_version=None,
    )

    # session 工厂：按 cfg.session.backend 调 build_session()。memory 走
    # InMemorySession（进程内），sqlite 走 SQLiteSession（持久化、可跨进程恢复）。
    def _session_factory(sid: str):  # type: ignore[no-untyped-def]
        return build_session(cfg, sid, bootstrap=bootstrap)

    runtime = NativeRuntime.build(
        cfg,
        event_sinks=event_sinks,
        approval=approval,
        tools=registry,
        enabled_tool_names=enabled_tool_names,
        session_factory=_session_factory,
        instructions=instructions,
        prompt_debug_sink=PromptDebugDumpSink() if prompt_debug else None,
        instruction_origins=instruction_origins,
    )

    # try/finally 确保 smoke / 交互循环 / KeyboardInterrupt / 其它异常
    # 三种退出路径都会 aclose runtime（释放 provider httpx client）。
    # 不上 async context manager 是为了控制改动面：runtime 目前只在这里起止。
    try:
        if smoke:
            await _run_smoke(runtime, session_id)
            return

        resolved_session_id = session_id or _generate_cli_session_id()
        bridge = SessionBridge(
            runtime=runtime,
            adapter=adapter,
            session_id=resolved_session_id,
        )

        _print_banner(
            cfg,
            bridge.session_id,
            verbose=verbose,
            trace_enabled=trace_enabled,
            instructions_sources=len(instructions_files),
        )
        await bridge.run_loop()
    finally:
        await runtime.aclose()


def _resolve_memory_dir(raw: str) -> Path:
    """把 `cfg.evolution.memory.root_path` 解析成绝对 memory 目录。

    - 绝对路径直接用（支持 ``~`` 展开）。
    - 相对路径视为相对于 **当前 cwd** 的子目录。

    这里不再追加 ``.kongming/memory``——由用户在 config 中直接写完整 memory 目录，
    让 MemoryStore 按 memory_dir 使用。
    """
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded
    return (Path.cwd() / expanded).resolve()


async def _assemble_instructions(
    cfg: Config,
    instructions_files: list[Path],
) -> tuple[str, list[str], MemoryStore | None]:
    """用 InstructionLoader 合成最终 system prompt 文本。

    Returns:
        (rendered_text, instruction_origins, memory_store) — 渲染后的完整文本、来源 origin 列表、
        以及 MemoryStore 实例（memory 关闭时为 ``None``）。

    - 基础 agent 规约来自 ``.kongming/prompts/{AGENT,TOOLS,USER}.md`` 三份用户
      可编辑的模板（v0.1.3 prompt-modules）。启动时若这些文件缺失，
      ``context.prompts_loader.materialize_and_load_prompts`` 会从
      ``src/prompts/templates/`` 物化默认内容。
    - extra_files 来自 CLI ``--instructions-file`` 参数
    - 环境变量 ``KONGMING_EXTRA_INSTRUCTIONS`` 自动读取（include_env=True）
    - 本地长期记忆的加载由 ``cfg.evolution.memory`` 控制：
        - ``enabled=False`` 时完全跳过（返回 memory_store=None）
        - ``inject_prompt=False`` 时仍加载活态 entries 供 memory tool 使用，
          但不 append ``InstructionSource(origin="memory")``
    """
    base_agent_instructions = await materialize_and_load_prompts(get_kongming_home())

    loader = InstructionLoader(extra_files=instructions_files, include_env=True)
    sources = list(await loader.load(agent_instructions=base_agent_instructions))

    memory_cfg = cfg.evolution.memory
    memory_store: MemoryStore | None = None

    if memory_cfg.enabled:
        memory_dir = _resolve_memory_dir(memory_cfg.root_path)
        memory_store = MemoryStore(
            memory_dir=memory_dir,
            read_max_chars=memory_cfg.read_max_chars,
        )
        await memory_store.load_from_disk()

        if memory_cfg.inject_prompt:
            snapshot_prompt = (
                memory_store.snapshot.render_prompt() if memory_store.snapshot else None
            )
            if snapshot_prompt is not None:
                sources.append(InstructionSource(origin="memory", content=snapshot_prompt))

    rendered = loader.render(sources)
    origins = [s.origin for s in sources]
    return rendered, origins, memory_store


async def _run_smoke(runtime: NativeRuntime, session_id: str | None) -> None:
    """最小 smoke：真的调一次模型，确认配置 + provider 可达。

    这条路径会**真的**发起一次模型请求，期望本地模型服务已经启动。
    失败时退出码 1，方便 CI / shell 脚本判定。
    """
    smoke_sid = session_id or "smoke"
    result = await runtime.run(
        "hello, respond with 'ok' in one short line.",
        session_id=smoke_sid,
    )
    if result.status == "completed":
        text = (result.final_message.content if result.final_message else "") or ""
        click.echo(f"[smoke] ok status={result.status} reply={text.strip()[:80]!r}")
        return
    click.echo(f"[smoke] failed status={result.status} error={result.error}", err=True)
    raise SystemExit(1)


def _load_config_or_exit(config_path: Path | None) -> Config:
    try:
        return load_config(config_path)
    except (ConfigLoadError, ConfigValidationError) as exc:
        click.echo(f"[config] {type(exc).__name__}: {exc.message}", err=True)
        raise SystemExit(2) from exc


def _print_banner(
    cfg: Config,
    session_id: str,
    *,
    verbose: bool,
    trace_enabled: bool,
    instructions_sources: int,
) -> None:
    locality = "local" if cfg.model.is_local else "remote"
    thinking_suffix = (
        f" · thinking={cfg.model.reasoning_effort}" if cfg.model.reasoning_effort else ""
    )
    click.echo(
        f"kongming-agent · model={cfg.model.name} ({locality}){thinking_suffix} · "
        f"session={session_id} ({cfg.session.backend}) · approval={cfg.approval.mode}"
    )
    extras: list[str] = ["Ctrl+D 退出"]
    if trace_enabled:
        extras.append(f"trace={cfg.trace.output_path}")
    if instructions_sources:
        extras.append(f"instructions_files={instructions_sources}")
    if verbose:
        extras.append("verbose 已开启（事件输出到 stderr）")
    click.echo(" · ".join(extras) + "。")


if __name__ == "__main__":  # pragma: no cover - 进程入口
    main()


__all__ = ["main"]
