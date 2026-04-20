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

import click

from config_loader import Config, load_config
from config_loader.errors import ConfigLoadError, ConfigValidationError
from context import InstructionLoader, build_session
from core.contracts import EventSink
from executors.agent_runtime.native_runtime import NativeRuntime
from host.cli_adapter import CLIAdapter, CLIEventSink
from host.session_bridge import SessionBridge
from observability import JsonlTraceSink
from tools import build_default_approval, build_default_registry


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="配置文件路径（缺省走 KONGMING_CONFIG 环境变量或 config/default.yaml）",
)
@click.option(
    "--session-id",
    default=None,
    help="复用会话 ID（缺省随机生成一个 cli-<hex8>）",
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
def main(
    config_path: Path | None,
    session_id: str | None,
    verbose: bool,
    smoke: bool,
    instructions_files: tuple[Path, ...],
    no_trace: bool,
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
) -> None:
    cfg = _load_config_or_exit(config_path)

    adapter = CLIAdapter(verbose=verbose)

    # 事件 sink 装配：默认挂 JsonlTraceSink（--no-trace 可关）；verbose 时再补
    # 一个 CLIEventSink 把事件打到终端（stderr）。runner 会 fan-out 到 list 里
    # 的所有 sink，顺序不敏感。
    event_sinks: list[EventSink] = []
    if trace_enabled:
        event_sinks.append(JsonlTraceSink(cfg.trace.output_path))
    if verbose:
        event_sinks.append(CLIEventSink(verbose=True))

    registry = build_default_registry(
        file_enabled=cfg.tool.file.enabled,
        shell_enabled=cfg.tool.shell.enabled,
    )
    enabled_tool_names = registry.names()

    # approval 按配置模式选：interactive 时让 adapter 提供 prompt_fn，
    # 其它模式（auto_allow / auto_deny）不需要 prompt_fn。
    prompt_fn = adapter.prompt_approval if cfg.approval.mode == "interactive" else None
    approval = build_default_approval(cfg.approval.mode, prompt_fn=prompt_fn)

    # instructions 装配：用 InstructionLoader 把 agent_spec 基础文本 + 外部文件
    # + KONGMING_EXTRA_INSTRUCTIONS 合成一段带来源标注的 system prompt。
    instructions = await _assemble_instructions(instructions_files)

    # session 工厂：按 cfg.session.backend 调 build_session()。memory 走
    # InMemorySession（进程内），sqlite 走 SQLiteSession（持久化、可跨进程恢复）。
    def _session_factory(sid: str):  # type: ignore[no-untyped-def]
        return build_session(cfg, sid)

    runtime = NativeRuntime.build(
        cfg,
        event_sinks=event_sinks,
        approval=approval,
        tools=registry,
        enabled_tool_names=enabled_tool_names,
        session_factory=_session_factory,
        instructions=instructions,
    )

    if smoke:
        await _run_smoke(runtime, session_id)
        return

    resolved_session_id = session_id or f"cli-{uuid.uuid4().hex[:8]}"
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


async def _assemble_instructions(instructions_files: list[Path]) -> str:
    """用 InstructionLoader 合成最终 system prompt 文本。

    - 基础来源是 "You are kongming agent."
    - extra_files 来自 CLI ``--instructions-file`` 参数
    - 环境变量 ``KONGMING_EXTRA_INSTRUCTIONS`` 自动读取（include_env=True）
    """
    loader = InstructionLoader(extra_files=instructions_files, include_env=True)
    sources = await loader.load(agent_instructions="You are kongming agent.")
    return loader.render(sources)


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
    click.echo(
        f"kongming-agent · model={cfg.model.name} ({locality}) · "
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
