"""kongming-agent CLI 入口。

这里只做四件事：

1. 用 :mod:`click` 解析命令行参数；
2. 调 :func:`infrastructure.config.load_config` 拿统一配置；
3. 按 Config 装配 session 工厂 / trace sink / instructions 三条**可观测的**
   输入通道；
4. 通过 :meth:`runtime_assembly.native_runtime.NativeRuntime.build`
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
import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import click

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.roundtable_review.presets import (
    code_review_role_presets,
)
from application.subagents.manager import SubAgentManager, SubAgentTask
from application.subagents.permissions import SubAgentPermissionSpec
from core.contracts import ApprovalRequest, EventSink, SupportsLLMStream, ToolContext
from hosts.cli.adapter import CLIAdapter, CLIEventSink
from hosts.shared.mcp_runtime_registration import McpRuntimeRegistrationManager
from hosts.shared.session_bridge import SessionBridge
from infrastructure.config import (
    Config,
    get_kongming_home,
    load_config,
    resolve_kongming_path,
)
from infrastructure.config.errors import ConfigLoadError, ConfigValidationError
from infrastructure.tracing import JsonlTraceSink, PromptDebugDumpSink
from memory import MemoryStore
from prompting import assemble_instructions
from prompting.skills.skill_loader import SkillSpec, format_skill_listing, load_skill_specs
from runtime_assembly.native_runtime import NativeRuntime
from sessions import (
    SessionSummary,
    build_session,
    discover_file_sessions,
    discover_sqlite_sessions,
    find_session_by_id,
    most_recent_session,
)
from tools import (
    AgentWorkflowHandle,
    build_default_approval,
    build_default_registry,
    register_agent_role_tool,
    register_agent_workflow_tool,
    register_evolution_write_tool_if_enabled,
    register_schedule_tool_if_enabled,
    register_task_progress_tool,
)
from tools.runtime.approval import PromptActionFn

logger = logging.getLogger(__name__)

_CLI_SESSION_ID_HEX_LEN = 12


def _generate_cli_session_id() -> str:
    """生成默认 CLI session id。"""
    return f"cli-{uuid.uuid4().hex[:_CLI_SESSION_ID_HEX_LEN]}"


def _resolve_cli_session_id(
    session_id: str | None,
    *,
    smoke: bool,
    subagent_smoke: bool,
    workflow_smoke: bool = False,
) -> str:
    """在审批和运行时装配前解析 CLI 会话 ID。"""
    if session_id:
        return session_id
    if smoke:
        return "smoke"
    if subagent_smoke:
        return "subagent-smoke"
    if workflow_smoke:
        return "workflow-smoke"
    return _generate_cli_session_id()


def _build_cli_manager_prompt_fn(session_id: str) -> PromptActionFn:
    """构造由审批管理器承接的 CLI 终端审批函数。"""
    from hosts.cli.approval import build_cli_action_prompt
    from hosts.cli.approval_manager_sink import CLIApprovalEventSink
    from safety.approval.manager import get_approval_manager, make_manager_prompt_fn
    from safety.approval.rules import ApprovalRules

    action_prompt = build_cli_action_prompt()
    manager = get_approval_manager(
        rules=ApprovalRules(policy=_build_cli_auto_approval_policy()),
    )
    if not manager.has_event_sink_type(CLIApprovalEventSink):
        manager.register_event_sink(CLIApprovalEventSink(manager, action_prompt))
    return make_manager_prompt_fn(
        manager,
        session_id,
        channel="cli",
        default_cwd=str(Path.cwd()),
    )


def _build_cli_auto_approval_policy() -> Any:
    """为 CLI 构造共享自动审批策略；装配失败时按失败关闭处理。"""
    try:
        from safety.auto_approval import (
            AutoApprovalPolicy,
            ConfigStore,
            load_default_rules,
            materialize_user_rules_yaml,
        )

        home = get_kongming_home()
        auto_approval_root = home / "web" / "auto_approval"
        auto_approval_root.mkdir(parents=True, exist_ok=True)
        rules_yaml = materialize_user_rules_yaml(home)
        return AutoApprovalPolicy(
            load_default_rules(rules_yaml),
            ConfigStore(auto_approval_root),
        )
    except Exception:
        logger.exception("CLI auto-approval policy setup failed; falling back to ask")
        return None


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(dir_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="配置文件路径（缺省走 KONGMING_CONFIG 环境变量或 config/setting.yaml）。"
    "click 在 parse 阶段就 resolve 成绝对路径，避免与 --workdir chdir 联用时找不到。",
)
@click.option(
    "--session-id",
    default=None,
    help="复用会话 ID（缺省随机生成一个 cli-<hex12>）",
)
@click.option(
    "--list-sessions",
    is_flag=True,
    default=False,
    help="列出当前持久化 backend 的已有 session，并退出。",
)
@click.option(
    "--resume-last",
    is_flag=True,
    default=False,
    help="恢复最近活跃的已有 session。",
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
    "--subagent-smoke",
    is_flag=True,
    default=False,
    help="运行一个小型 subagent workflow smoke，不进入交互。",
)
@click.option(
    "--workflow-smoke",
    is_flag=True,
    default=False,
    help="运行 workflow 工具入口 smoke，覆盖 run_agent_workflow + approval + map_reduce planner。",
)
@click.option(
    "--model-preset",
    "model_preset_id",
    default=None,
    help="按 config.web.llm_presets 里的 preset id 覆盖 CLI 模型配置，例如 minimax-m3。",
)
@click.option(
    "--instructions-file",
    "instructions_files",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path),
    multiple=True,
    help="额外的系统指令文件（markdown / 文本）。可重复指定。"
    "click 在 parse 阶段 resolve 成绝对路径，避免与 --workdir chdir 联用时失效。",
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
    help="保存每轮 system prompt 和完整 history 到 kongming_home/debug/。",
)
@click.option(
    "--stream/--no-stream",
    "stream_flag",
    default=None,
    help="启用/关闭 LLM 流式响应（覆盖 config.stream.enabled）。",
)
@click.option(
    "--workdir",
    "-C",
    "workdir",
    type=click.Path(
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        path_type=Path,
    ),
    default=None,
    help="启动时切到该目录工作（os.chdir）。LLM 看到的 cwd 即此路径。等同于先 cd 进去再启动 CLI。",
)
def main(
    config_path: Path | None,
    session_id: str | None,
    list_sessions: bool,
    resume_last: bool,
    verbose: bool,
    smoke: bool,
    subagent_smoke: bool,
    workflow_smoke: bool,
    model_preset_id: str | None,
    instructions_files: tuple[Path, ...],
    no_trace: bool,
    reasoning_effort: str | None,
    show_reasoning: bool | None,
    prompt_debug: bool,
    stream_flag: bool | None,
    workdir: Path | None,
) -> None:
    """kongming-agent CLI"""
    try:
        asyncio.run(
            _run(
                config_path=config_path,
                session_id=session_id,
                list_sessions=list_sessions,
                resume_last=resume_last,
                verbose=verbose,
                smoke=smoke,
                subagent_smoke=subagent_smoke,
                workflow_smoke=workflow_smoke,
                model_preset_id=model_preset_id,
                instructions_files=list(instructions_files),
                trace_enabled=not no_trace,
                reasoning_effort=reasoning_effort,
                show_reasoning=show_reasoning,
                prompt_debug=prompt_debug,
                stream_flag=stream_flag,
                workdir=workdir,
            )
        )
    except KeyboardInterrupt:
        # 顶层 Ctrl-C：干净退出，不打 traceback。
        raise SystemExit(130) from None


async def _run(
    *,
    config_path: Path | None,
    session_id: str | None,
    list_sessions: bool,
    resume_last: bool,
    verbose: bool,
    smoke: bool,
    subagent_smoke: bool = False,
    workflow_smoke: bool = False,
    model_preset_id: str | None = None,
    instructions_files: list[Path] | None = None,
    trace_enabled: bool = True,
    reasoning_effort: str | None = None,
    show_reasoning: bool | None = None,
    prompt_debug: bool = False,
    stream_flag: bool | None = None,
    workdir: Path | None = None,
) -> None:
    # --workdir / -C：在 load_config 之前 chdir，让 KONGMING_HOME 默认值
    # 工具相对路径和 thread.cwd 语义基于新 cwd；kongming_home 由 get_kongming_home() 决定。
    #
    # 但必须先把 ``--config`` / ``--instructions-file`` 的相对路径基于**原
    # cwd** resolve（``cli.sh`` 写死了 ``--config config/setting.yaml`` 是相对
    # 仓库根的）。click option 已加 ``resolve_path=True``；此处再做防御性
    # resolve 覆盖直接 ``await _run(...)`` 绕过 click 的调用路径。
    config_path, instructions_files = _resolve_input_paths_before_chdir(
        config_path, instructions_files or []
    )

    if workdir is not None:
        _chdir_or_exit(workdir)

    cfg = _load_config_or_exit(config_path)
    if model_preset_id:
        cfg = _apply_model_preset_or_exit(cfg, model_preset_id)
    _validate_session_selection_or_exit(
        session_id=session_id,
        list_sessions=list_sessions,
        resume_last=resume_last,
        smoke=smoke or subagent_smoke or workflow_smoke,
    )

    discovered_sessions, discovered_path = _discover_persistent_sessions(cfg)
    if list_sessions:
        _print_sessions_and_exit(
            backend=cfg.session.backend,
            summaries=discovered_sessions,
            source_path=discovered_path,
        )
        return

    if resume_last:
        if cfg.session.backend == "memory":
            click.echo("[sessions] --resume-last 需要 file 或 sqlite backend。", err=True)
            raise SystemExit(2)
        latest = most_recent_session(discovered_sessions)
        if latest is None:
            source = str(discovered_path) if discovered_path is not None else "<unavailable>"
            click.echo(f"[sessions] 未找到可恢复 session：{source}", err=True)
            raise SystemExit(2)
        session_id = latest.session_id

    if (
        session_id
        and not (smoke or subagent_smoke)
        and cfg.session.backend in ("file", "sqlite")
        and find_session_by_id(discovered_sessions, session_id) is None
    ):
        source = str(discovered_path) if discovered_path is not None else "<unavailable>"
        click.echo(
            f"[sessions] session 不存在：{session_id}；运行 --list-sessions 查看可用会话。"
            f" source={source}",
            err=True,
        )
        raise SystemExit(2)

    cfg = _bind_discovered_session_path(cfg, discovered_path)
    resolved_session_id = _resolve_cli_session_id(
        session_id,
        smoke=smoke,
        subagent_smoke=subagent_smoke,
        workflow_smoke=workflow_smoke,
    )

    # CLI 参数 --reasoning-effort 覆盖 config 文件里的设置。
    if reasoning_effort is not None:
        effort_typed: Literal["low", "medium", "high"] = reasoning_effort  # type: ignore[assignment]
        cfg = cfg.model_copy(
            update={"model": cfg.model.model_copy(update={"reasoning_effort": effort_typed})}
        )

    # CLI 参数 --stream/--no-stream 覆盖 config.stream.enabled。
    if stream_flag is not None:
        cfg = cfg.model_copy(
            update={"stream": cfg.stream.model_copy(update={"enabled": stream_flag})}
        )

    # --show-reasoning flag 覆盖 config.cli.show_reasoning；未传 flag 时沿用 config。
    effective_show_reasoning = (
        show_reasoning if show_reasoning is not None else cfg.cli.show_reasoning
    )

    # v0.3 cron-delivery M5：cron 关时不构造 sink，避免不必要的拉模块。
    # 当 scheduler.enabled 时构造 CliDeliverySink + 注入 pre_prompt_hook
    # 让 REPL 在每次 prompt 之前 flush 投递 buffer。
    cli_cron_sink: Any = None
    cron_dispatcher: Any = None
    cli_pre_prompt_hook: Any = None
    if cfg.scheduler.enabled:
        from hosts.cli.cron_delivery import CliDeliverySink
        from scheduler.delivery import DeliveryDispatcher

        cli_cron_sink = CliDeliverySink()
        cron_dispatcher = DeliveryDispatcher(cli_sink=cli_cron_sink)

        async def _flush_cron_buffer() -> None:
            pending = await cli_cron_sink.drain_pending()
            if pending:
                # 用 click.echo 与 CLIAdapter.write_output 风格一致
                click.echo(pending, nl=False)

        cli_pre_prompt_hook = _flush_cron_buffer

    adapter = CLIAdapter(verbose=verbose, pre_prompt_hook=cli_pre_prompt_hook)

    # 事件 sink 装配：默认挂 JsonlTraceSink（--no-trace 可关）；verbose 或
    # show_reasoning 时挂 CLIEventSink。runner 会 fan-out 到 list 里所有 sink。
    event_sinks: list[EventSink] = []
    if trace_enabled:
        event_sinks.append(
            JsonlTraceSink(
                resolve_kongming_path(cfg.trace.output_path),
                auto_flush=cfg.trace.auto_flush,
                delta_sampling=cfg.stream.delta_sampling,
                periodic_batch_size=cfg.stream.periodic_batch_size,
            )
        )
    # 协调 reasoning 显示归属：流式开启时由 CLIStreamSink 实时打印 reasoning.delta，
    # CLIEventSink 不再在 llm.response 后重复打印 reasoning_content（避免双重显示）。
    cli_event_show_reasoning = effective_show_reasoning and not cfg.stream.enabled
    if verbose or cli_event_show_reasoning:
        event_sinks.append(CLIEventSink(verbose=verbose, show_reasoning=cli_event_show_reasoning))
    # 流式渲染 sink：cfg.stream.enabled 时挂上 CLIStreamSink 负责 reasoning + content 实时渲染。
    if cfg.stream.enabled:
        from hosts.cli import CLIStreamSink

        event_sinks.append(
            CLIStreamSink(
                show_reasoning=effective_show_reasoning,
            )
        )

    # v0.1.6 skill 装载：扫描 <home>/skills + <cwd>/.kongming/skills，得到 SkillSpec
    # 列表与 listing 文本。listing 在 _assemble_instructions 内部拼到 system prompt；
    # specs 字典传给 build_default_registry 注册 SkillTool。
    skill_specs_list = await load_skill_specs(
        get_kongming_home(),
        workspace=Path.cwd(),
        event_sinks=event_sinks,
    )
    skill_specs: dict[str, SkillSpec] = {s.name: s for s in skill_specs_list}
    skill_listing = format_skill_listing(skill_specs_list)

    registry = build_default_registry(
        file_enabled=cfg.tool.file.enabled,
        shell_enabled=cfg.tool.shell.enabled,
        shell_timeout_seconds=cfg.tool.shell.timeout_seconds,
        shell_max_stream_bytes=cfg.tool.shell.max_stream_bytes,
        shell_terminate_grace_seconds=cfg.tool.shell.terminate_grace_seconds,
        file_read_max_bytes=cfg.tool.file.read_max_bytes,
        skill_specs=skill_specs or None,
        skill_event_sinks=event_sinks,
    )

    # v0.2 cron：跟 memory_tool 一致的"外部 register"模式。
    # ticker_factory 闭包：schedule_tool.run_now 路径会调它装配 fresh runtime；
    # 必须 lazy 导入 build_cron_execution_bridge 以遵循"cron 关时不拉模块"。
    # v0.3 M4/M5：cron_dispatcher 已在上方按 cfg.scheduler.enabled 构造；
    # 这里透传给 bridge 让 schedule_tool.run_now 触发的 cron run 也走投递。
    def _scheduler_runtime_factory(_store):  # type: ignore[no-untyped-def]
        from scheduler.runtime_factory import build_cron_execution_bridge

        return build_cron_execution_bridge(
            cfg,
            _store,
            event_sinks=event_sinks,
            dispatcher=cron_dispatcher,
        )

    scheduler_store = register_schedule_tool_if_enabled(
        registry,
        cfg,
        runtime_factory_fn=_scheduler_runtime_factory,
    )
    register_evolution_write_tool_if_enabled(
        registry,
        cfg,
        event_sinks=event_sinks,
    )
    agent_workflow_handle = AgentWorkflowHandle()
    agent_role_manager = AgentRoleManager(
        role_dir=get_kongming_home() / "agent_roles",
        builtin_roles=code_review_role_presets(),
    )
    register_agent_role_tool(registry, agent_role_manager)
    register_agent_workflow_tool(registry, agent_workflow_handle)
    register_task_progress_tool(registry, cfg)

    # approval 按配置模式选：interactive 走 ApprovalManager + CLI sink；
    # 其它模式（auto_allow / auto_deny）不需要 prompt_fn。
    prompt_fn = (
        _build_cli_manager_prompt_fn(resolved_session_id)
        if cfg.approval.mode == "interactive"
        else None
    )
    approval = build_default_approval(cfg.approval.mode, prompt_fn=prompt_fn)

    # instructions 装配：用 InstructionLoader 把 agent_spec 基础文本 + 外部文件
    # + KONGMING_EXTRA_INSTRUCTIONS 合成一段带来源标注的 system prompt。
    # memory 通道受 cfg.evolution.memory.enabled/inject_prompt 控制；
    # enabled=False 时 memory_store 为 None。
    # prompts 装配（<kongming_home>/prompts/*.md 物化 + 读取）失败时给用户友好错误消息，
    # 对齐 _load_config_or_exit 的 UX；避免裸 traceback。
    try:
        instructions, instruction_origins, memory_store = await _assemble_instructions(
            cfg,
            instructions_files,
            skill_listing=skill_listing,
        )
    except (PermissionError, OSError, UnicodeDecodeError, FileNotFoundError) as exc:
        click.echo(
            f"[prompts] failed to load kongming_home/prompts templates: {type(exc).__name__}: {exc}",
            err=True,
        )
        raise SystemExit(2) from exc

    # 仅当 memory 启用时才注册 memory tool + 装 MemoryRefreshSink。
    if memory_store is not None:
        from tools.builtin.memory_tool import build_memory_tool

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
            from hosts.shared.memory_refresh_sink import MemoryRefreshSink
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

    mcp_runtime_registration = McpRuntimeRegistrationManager(cfg, event_sinks=event_sinks)
    await mcp_runtime_registration.register(
        registry,
        excluded_tool_names=("evolution_write",),
    )

    enabled_tool_names = [
        name for name in registry.names() if name != "evolution_write"
    ]  # child reviewer 专用工具不暴露给主 agent

    # session bootstrap：收集 CLI 阶段可得的稳定元数据，file backend 需要它。
    import hashlib
    import time

    from sessions import SessionBootstrap

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
    subagent_manager = SubAgentManager(runtime)
    agent_workflow_manager = AgentWorkflowManager(
        subagents=subagent_manager,
        config=cfg,
        workspace_root=Path.cwd(),
        role_manager=agent_role_manager,
    )
    agent_workflow_handle.bind(agent_workflow_manager)

    # v0.2 cron：scheduler.enabled 时拉起后台 ticker 循环；ticker 自身装配
    # 一份独立的 cron-用 NativeRuntime（fresh agent run 走它），不复用主聊天
    # runtime（后者带交互上下文）。退出路径必须 set stop_event + await，
    # 否则 KeyboardInterrupt 之后 ticker 还在跑、httpx client 未释放。
    ticker_task: asyncio.Task[None] | None = None
    ticker_stop: asyncio.Event | None = None
    ticker_runtime = None
    if (
        cfg.scheduler.enabled
        and not (smoke or subagent_smoke or workflow_smoke)
        and scheduler_store is not None
    ):
        from scheduler.runtime_factory import build_cron_execution_bridge
        from scheduler.ticker import run_ticker_loop

        # v0.3 M4/M5：ticker 主循环 build cron bridge 时也传 dispatcher，
        # 与 _scheduler_runtime_factory 闭包共用同一 cron_dispatcher 实例 ——
        # 两条路径触发的 cron run 投递最终都走同一个 cli_cron_sink。
        ticker_runtime, ticker_bridge = build_cron_execution_bridge(
            cfg,
            scheduler_store,
            event_sinks=event_sinks,
            dispatcher=cron_dispatcher,
        )
        ticker_stop = asyncio.Event()
        ticker_task = asyncio.create_task(
            run_ticker_loop(
                scheduler_store,
                ticker_bridge,
                ticker_stop,
                interval=cfg.scheduler.interval,
                max_inflight=cfg.scheduler.max_inflight,
            )
        )

    # try/finally 确保 smoke / 交互循环 / KeyboardInterrupt / 其它异常
    # 三种退出路径都会 aclose runtime（释放 provider httpx client）。
    # 不上 async context manager 是为了控制改动面：runtime 目前只在这里起止。
    try:
        if smoke:
            await _run_smoke(runtime, resolved_session_id)
            return
        if subagent_smoke:
            await _run_subagent_smoke(agent_workflow_manager, resolved_session_id)
            return
        if workflow_smoke:
            await _run_workflow_smoke(runtime, resolved_session_id)
            return

        # 流式路径下 CLIStreamSink 已实时打印 content；SessionBridge 不再重复
        # write_output(final.content)。流式实际生效要求：cfg 启用 + provider
        # 实现 SupportsLLMStream（AnthropicMessagesProvider 暂未实现，会自动 fallback）。
        # getattr 兜底：测试用的 dummy runtime 可能没暴露 llm 属性。
        runtime_llm = getattr(runtime, "llm", None)
        stream_active = (
            cfg.stream.enabled
            and runtime_llm is not None
            and isinstance(runtime_llm, SupportsLLMStream)
        )
        bridge = SessionBridge(
            runtime=runtime,
            adapter=adapter,
            session_id=resolved_session_id,
            echo_final_content=not stream_active,
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
        # cron ticker 收尾：set stop_event → 等 ticker_task 自然退出（带超时
        # 兜底，超时则 cancel）；ticker_runtime 必须随后 aclose 以释放 httpx
        # 连接池。顺序：先停 ticker → 再 aclose runtime。
        if ticker_task is not None and ticker_stop is not None:
            ticker_stop.set()
            try:
                await asyncio.wait_for(ticker_task, timeout=30.0)
            except (TimeoutError, asyncio.CancelledError):
                ticker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ticker_task
            if ticker_runtime is not None:
                aclose_fn = getattr(ticker_runtime, "aclose", None)
                if aclose_fn is not None:
                    await aclose_fn()
        try:
            await runtime.aclose()
        finally:
            await mcp_runtime_registration.aclose()


def _resolve_sitian_prompt_root(cfg: Config) -> Path | None:
    """解析 sitian prompt 注入所需的根目录。

    优先读 ``SITIAN_PROMPT_ROOT`` 环境变量；未设置时返回 None（不注入）。
    返回的是频道父目录（如 ``/Users/kid/.SiTian``），
    ``build_sitian_context_text`` 会遍历子目录按频道聚合。
    """
    import os

    raw = os.environ.get("SITIAN_PROMPT_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _resolve_memory_dir(raw: str) -> Path:
    """把 `cfg.evolution.memory.root_path` 解析成绝对 memory 目录。

    - 绝对路径直接用（支持 ``~`` 展开）。
    - ``.kongming/*`` 相对路径派生到 ``kongming_home``。
    - 其他相对路径按当前进程路径规则解析。

    这里不再追加 ``.kongming/memory``——由用户在 config 中直接写完整 memory 目录，
    让 MemoryStore 按 memory_dir 使用。
    """
    return resolve_kongming_path(raw)


async def _assemble_instructions(
    cfg: Config,
    instructions_files: list[Path],
    *,
    skill_listing: str = "",
) -> tuple[str, list[str], MemoryStore | None]:
    """用 InstructionLoader 合成最终 system prompt 文本。

    Returns:
        (rendered_text, instruction_origins, memory_store) — 渲染后的完整文本、来源 origin 列表、
        以及 MemoryStore 实例（memory 关闭时为 ``None``）。

    - 基础指令装配（prompts 物化 + env + runtime context）委托给
      :func:`prompting.assemble_instructions`，本函数只追加 skill / memory 通道。
    - v0.1.6 skill 通道：``skill_listing`` 非空时追加为 ``# skills`` 段并标
      origin。空 listing（无 skill 或 loader 跳过）保持 v0.1.5 行为。
    - 本地长期记忆的加载由 ``cfg.evolution.memory`` 控制：
        - ``enabled=False`` 时完全跳过（返回 memory_store=None）
        - ``inject_prompt=False`` 时仍加载活态 entries 供 memory tool 使用，
          但不 append ``InstructionSource(origin="memory")``
    """
    kongming_home = get_kongming_home()
    sitian_root = _resolve_sitian_prompt_root(cfg)

    # 公共指令装配：prompts 物化 + InstructionLoader + runtime context
    rendered, origins = await assemble_instructions(
        kongming_home=kongming_home,
        extra_files=instructions_files,
        sitian_root=sitian_root,
    )

    # v0.1.6 skill listing 通道（在 memory 之前；listing 描述能力，memory 描述事实）
    if skill_listing:
        rendered = rendered + f"\n\n# skills\n{skill_listing}"
        origins = [*origins, "skills"]

    # Memory 通道（CLI 特有，不在公共函数里）
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
                rendered = rendered + f"\n\n# memory\n{snapshot_prompt}"
                origins = [*origins, "memory"]

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


async def _run_subagent_smoke(
    manager: AgentWorkflowManager,
    session_id: str | None,
) -> None:
    """Run a tiny real-model subagent workflow from the CLI."""
    parent_session_id = session_id or "subagent-smoke"
    result = await manager.run_parallel(
        parent_session_id=parent_session_id,
        tasks=[
            SubAgentTask(
                task_id="calc",
                task_name="simple calculation",
                prompt="计算 7 + 5。只输出数字 12 和一句很短的说明。",
            ),
            SubAgentTask(
                task_id="write-note",
                task_name="write scoped note",
                prompt=(
                    "必须调用 write_file 工具，在你的工作目录内创建 result.txt。"
                    "文件内容必须是：kongming subagent smoke。"
                    "完成后报告写入的绝对路径。"
                ),
                tool_names=("write_file",),
                permission=SubAgentPermissionSpec(mode="scoped_workdir"),
            ),
        ],
    )
    calc_run = next(run for run in result.runs if run.task.task_id == "calc")
    write_run = next(run for run in result.runs if run.task.task_id == "write-note")
    write_dir_raw = write_run.task.metadata.get("working_dir")
    write_dir = Path(str(write_dir_raw))
    expected_file = write_dir / "result.txt"
    if not result.completed:
        click.echo(
            f"[subagent-smoke] failed workflow_id={result.workflow_id} dir={result.workflow_dir}",
            err=True,
        )
        raise SystemExit(1)
    if "12" not in calc_run.content:
        click.echo(
            f"[subagent-smoke] calc result missing 12: {calc_run.content!r}",
            err=True,
        )
        raise SystemExit(1)
    if not expected_file.is_file():
        click.echo(
            f"[subagent-smoke] expected file missing: {expected_file}",
            err=True,
        )
        raise SystemExit(1)
    text = expected_file.read_text(encoding="utf-8")
    if text != "kongming subagent smoke":
        click.echo(
            f"[subagent-smoke] unexpected file content: {text!r}",
            err=True,
        )
        raise SystemExit(1)
    click.echo(
        "[subagent-smoke] ok "
        f"workflow_id={result.workflow_id} "
        f"workflow_dir={result.workflow_dir} "
        f"file={expected_file}"
    )


def _workflow_smoke_args() -> dict[str, object]:
    """构造 workflow smoke 参数，输入为空，输出为会进入 map_reduce planner 的最小参数。"""
    return {
        "mode": "map_reduce",
        "payload": {
            "mode": "map_reduce",
            "objective": "CLI workflow smoke 验证 run_agent_workflow 入口。",
            "input_source": {
                "kind": "path_glob",
                "root_dir": ".",
                "include": ["__kongming_workflow_smoke_no_such_file__"],
                "exclude": [],
                "files": [],
                "index_provider": "rg",
                "input_digest": None,
            },
            "shard_strategy": {
                "kind": "by_file_count",
                "max_files_per_shard": 1,
                "max_estimated_tokens_per_shard": 1000,
                "min_shards": 1,
                "max_shards": 1,
                "preserve_directory_boundary": True,
                "prefer_dependency_cohesion": False,
            },
            "mapper": {
                "name_prefix": "workflow-smoke",
                "prompt_template": "code_findings_v0_1",
                "tool_names": ["read_file", "list_dir"],
                "skill_names": [],
                "permission_mode": "scoped_workdir",
                "max_turns": 1,
                "max_output_chars": 4000,
            },
            "reducer": {
                "kind": "deterministic",
                "dedupe_strategy": "exact_dedupe_key",
                "ranking_strategy": "severity_first",
                "max_findings": 1,
                "include_failed_shards": True,
                "reducer_prompt_template": None,
            },
            "limits": {
                "max_concurrency": 1,
                "workflow_timeout_seconds": 30,
                "mapper_timeout_seconds": 10,
                "reducer_timeout_seconds": 10,
                "mapper_retries": 0,
                "validation_repair_retries": 0,
            },
            "output_contract": "code_findings",
            "audit_tags": ["smoke", "cli-workflow-smoke"],
        },
    }


async def _run_workflow_smoke(runtime: NativeRuntime, session_id: str | None) -> None:
    """运行 workflow 工具入口 smoke，输入为 runtime 和 session，输出为 CLI 状态行。"""
    smoke_sid = session_id or "workflow-smoke"
    run_id = f"{smoke_sid}-1"
    call_id = "workflow-smoke-call-1"
    args = _workflow_smoke_args()
    request = ApprovalRequest(
        run_id=run_id,
        session_id=smoke_sid,
        turn=1,
        call_id=call_id,
        tool_name="run_agent_workflow",
        arguments=dict(args),
    )
    decision = await runtime.approval.decide(request)
    if not decision.approved:
        click.echo(
            f"[workflow-smoke] failed approval outcome={decision.outcome} reason={decision.reason}",
            err=True,
        )
        raise SystemExit(1)

    tool = runtime.tools["run_agent_workflow"]
    result = await tool.execute(
        dict(args),
        ToolContext(run_id=run_id, session_id=smoke_sid, turn=1, call_id=call_id),
    )
    error_text = result.error_message or result.content
    if result.ok or "map_reduce planner found no input files" not in error_text:
        click.echo(
            "[workflow-smoke] failed unexpected tool result "
            f"ok={result.ok} error={result.error_message!r}",
            err=True,
        )
        raise SystemExit(1)

    click.echo(
        "[workflow-smoke] ok "
        f"approval={decision.outcome} tool=run_agent_workflow planner=no_input_files"
    )


def _resolve_input_paths_before_chdir(
    config_path: Path | None,
    instructions_files: list[Path],
) -> tuple[Path | None, list[Path]]:
    """把 CLI 传入的相对路径基于**当前** cwd 解析成绝对路径。

    必须在 ``--workdir`` chdir **之前**调用。否则相对路径会落到新 cwd 下，
    导致 ``cli.sh --config config/setting.yaml`` 这类相对默认值找不到文件。

    Args:
        config_path: ``--config`` 选项；``None`` 时不动。
        instructions_files: ``--instructions-file`` 选项列表。

    Returns:
        ``(resolved_config_path, resolved_instructions_files)``。绝对路径
        原样返回；相对路径调 :meth:`Path.resolve`。
    """
    if config_path is not None and not config_path.is_absolute():
        config_path = config_path.resolve()
    instructions_files = [f if f.is_absolute() else f.resolve() for f in instructions_files]
    return config_path, instructions_files


def _chdir_or_exit(workdir: Path) -> None:
    """切换进程 cwd 到 ``workdir``，失败时友好报错并退出。

    click 的 ``Path(exists=True, ...)`` 已经做存在 / 是目录 / 可读校验；
    此函数只兜底 chdir 时刻的 OSError（race / 权限 / 设备问题）。
    """
    try:
        os.chdir(workdir)
    except OSError as exc:
        click.echo(f"[cli] 无法切到工作目录 {workdir}: {exc}", err=True)
        raise SystemExit(2) from exc


def _load_config_or_exit(config_path: Path | None) -> Config:
    try:
        return load_config(config_path)
    except (ConfigLoadError, ConfigValidationError) as exc:
        click.echo(f"[config] {type(exc).__name__}: {exc.message}", err=True)
        raise SystemExit(2) from exc


def _apply_model_preset_or_exit(cfg: Config, preset_id: str) -> Config:
    preset = next((item for item in cfg.web.llm_presets if item.id == preset_id), None)
    if preset is None:
        known = ", ".join(item.id for item in cfg.web.llm_presets) or "<empty>"
        click.echo(f"[config] unknown --model-preset {preset_id!r}; available: {known}", err=True)
        raise SystemExit(2)

    api_key = os.environ.get(preset.api_key_env, "") if preset.api_key_env else ""
    if preset.api_key_env and not api_key:
        click.echo(f"[config] env {preset.api_key_env} is required by preset {preset_id}", err=True)
        raise SystemExit(2)

    model_overrides: dict[str, Any] = {
        "name": preset.model,
        "base_url": preset.base_url,
        "api_key": api_key,
    }
    if preset.provider is not None:
        model_overrides["provider"] = preset.provider
    if preset.reasoning_effort is not None:
        model_overrides["reasoning_effort"] = preset.reasoning_effort
    return cfg.model_copy(update={"model": cfg.model.model_copy(update=model_overrides)})


def _validate_session_selection_or_exit(
    *,
    session_id: str | None,
    list_sessions: bool,
    resume_last: bool,
    smoke: bool,
) -> None:
    if session_id and resume_last:
        click.echo("[sessions] --session-id 和 --resume-last 只能二选一。", err=True)
        raise SystemExit(2)
    if list_sessions and smoke:
        click.echo("[sessions] --list-sessions 和 --smoke 不能同时使用。", err=True)
        raise SystemExit(2)
    if list_sessions and resume_last:
        click.echo("[sessions] --list-sessions 和 --resume-last 只能二选一。", err=True)
        raise SystemExit(2)


def _discover_persistent_sessions(
    cfg: Config,
) -> tuple[list[SessionSummary], Path | None]:
    if cfg.session.backend == "file":
        return _discover_file_backend_sessions(cfg.session.file_store_path)
    if cfg.session.backend == "sqlite":
        return _discover_sqlite_backend_sessions(cfg.session.store_path)
    return [], None


def _discover_file_backend_sessions(raw_path: str) -> tuple[list[SessionSummary], Path | None]:
    candidates = _resolve_session_candidates(raw_path)
    fallback_path = candidates[0] if candidates else None
    first_existing: Path | None = None
    for candidate in candidates:
        if candidate.is_dir():
            if first_existing is None:
                first_existing = candidate
            summaries = discover_file_sessions(candidate)
            if summaries:
                return summaries, candidate
    return [], first_existing or fallback_path


def _discover_sqlite_backend_sessions(raw_path: str) -> tuple[list[SessionSummary], Path | None]:
    candidates = _resolve_session_candidates(raw_path)
    fallback_path = candidates[0] if candidates else None
    first_existing: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            if first_existing is None:
                first_existing = candidate
            summaries = discover_sqlite_sessions(candidate)
            if summaries:
                return summaries, candidate
    return [], first_existing or fallback_path


def _resolve_session_candidates(raw_path: str) -> list[Path]:
    return [resolve_kongming_path(raw_path)]


def _print_sessions_and_exit(
    *,
    backend: str,
    summaries: list[SessionSummary],
    source_path: Path | None,
) -> None:
    if backend == "memory":
        click.echo("[sessions] memory backend 没有可恢复 session 列表。")
        return

    source = str(source_path) if source_path is not None else "<unavailable>"
    if not summaries:
        click.echo(f"[sessions] 未找到 session。source={source}")
        return

    click.echo(f"[sessions] backend={backend} source={source} count={len(summaries)}")
    for summary in summaries:
        click.echo(_format_session_summary(summary))


def _bind_discovered_session_path(cfg: Config, discovered_path: Path | None) -> Config:
    if discovered_path is None:
        return cfg
    if cfg.session.backend == "file":
        return cfg.model_copy(
            update={
                "session": cfg.session.model_copy(update={"file_store_path": str(discovered_path)})
            }
        )
    if cfg.session.backend == "sqlite":
        return cfg.model_copy(
            update={"session": cfg.session.model_copy(update={"store_path": str(discovered_path)})}
        )
    return cfg


def _format_session_summary(summary: SessionSummary) -> str:
    session_id = summary.session_id
    updated_at = summary.updated_at
    preview = summary.preview
    preview_text = preview if preview else "(empty)"
    timestamp = _format_timestamp(updated_at)
    return f"- {session_id} | {timestamp} | {preview_text}"


def _format_timestamp(value: float) -> str:
    if value <= 0:
        return "unknown"
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


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
