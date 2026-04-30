"""``python -m web.run`` 启动入口（v0.1.5）。

对应 ``make web``。流程：

1. 读 :class:`Config`（``KONGMING_CONFIG`` env 或默认 ``config/setting.yaml``）
2. 校验 ``cfg.web.enabled``
3. 装配 :class:`ThreadManager`（含 runtime_factory）
4. 调 :func:`web.app.create_app`
5. uvicorn 启动

runtime_factory 装配（Wave 4 接通）：

factory closure 按 preset_id 从 ``cfg.web.llm_presets`` 查找对应配置，
覆盖 ``cfg.model`` 的 name/base_url/api_key/provider/reasoning_effort 字段，
然后调 :meth:`NativeRuntime.build` + :class:`SessionBridge` 构造完整的
(NativeRuntime, SessionBridge) 元组返回给 ThreadManager。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def main() -> int:
    """CLI 入口；返回 exit code。"""
    # 尽量延迟 import，避免在 cli 模式下意外加载 web 依赖
    try:
        import uvicorn
    except ImportError as exc:
        sys.stderr.write(f"web dependencies not installed; run `uv sync --all-extras`: {exc}\n")
        return 1

    from config_loader import load_config
    from config_loader.paths import get_kongming_home
    from web.app import create_app
    from web.startup_progress import StartupProgress
    from web.thread_manager import ThreadManager

    home = get_kongming_home()
    progress = StartupProgress(home)
    progress.report("imports")

    config_path = os.environ.get("KONGMING_CONFIG", "config/setting.yaml")
    cfg = load_config(Path(config_path))
    progress.report("config")

    if not cfg.web.enabled:
        progress.fail("web.enabled is false")
        sys.stderr.write(
            "cfg.web.enabled=false; set web.enabled=true in config/setting.yaml "
            "or KONGMING_WEB_ENABLED=1 env to start web server\n"
        )
        return 1

    runtime_factory = _make_runtime_factory(cfg)
    progress.report("factory")

    tm = ThreadManager(cfg, kongming_home=home, runtime_factory=runtime_factory)  # type: ignore[arg-type]

    try:
        app = create_app(cfg, tm, home_dir=home)  # type: ignore[arg-type]
    except Exception as exc:
        progress.fail(f"create_app failed: {exc}")
        sys.stderr.write(f"create_app failed: {exc}\n")
        return 1
    progress.report("app")

    log_level = cfg.logging.level.lower()
    progress.report("uvicorn")
    try:
        uvicorn.run(
            app,
            host=cfg.web.host,
            port=cfg.web.port,
            log_level=log_level,
        )
    except Exception as exc:
        progress.fail(f"uvicorn.run failed: {exc}")
        raise
    return 0


def _make_runtime_factory(cfg: object) -> object:
    """Build runtime_factory closure that creates per-preset NativeRuntime + SessionBridge.

    Closure pre-computation (sync, once at startup):
    - preset_map: {preset_id: LLMPresetConfig}
    - registry: ToolRegistry with default tools
    - enabled_tool_names: list of tool names
    - instructions: lazy-loaded on first factory call, then cached

    The inner factory is async and called per-cell by ThreadManager._build_cell.
    """
    from config_loader.models import Config, LLMPresetConfig
    from config_loader.paths import get_kongming_home
    from context import SessionBootstrap, build_session
    from context.instruction_loader import assemble_instructions
    from context.skill_loader import format_skill_listing, load_skill_specs
    from executors.agent_runtime.native_runtime import NativeRuntime
    from host.session_bridge import SessionBridge
    from observability import JsonlTraceSink
    from tools import ToolRegistry, build_default_approval, build_default_registry

    assert isinstance(cfg, Config)
    real_cfg: Config = cfg

    # --- Closure pre-computation (sync, once at startup) ---
    preset_map: dict[str, LLMPresetConfig] = {p.id: p for p in real_cfg.web.llm_presets}
    home = get_kongming_home()

    # v0.1.6: registry / instructions 一并 lazy-load 在首次 factory 调用——
    # build_default_registry 需要 skill_specs，而 load_skill_specs 是 async
    # （启动期同步阶段没有 event loop）。同一把锁保证多并发首次调用串行化。
    _registry_cache: list[ToolRegistry | None] = [None]
    _enabled_tools_cache: list[list[str] | None] = [None]
    _instructions_cache: list[str | None] = [None]
    _origins_cache: list[list[str] | None] = [None]
    _cache_lock = asyncio.Lock()

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: object,
        sinks: object,
    ) -> tuple[Any, Any]:
        # 1. Resolve preset
        preset = preset_map.get(preset_id)
        if preset is None:
            raise ValueError(f"unknown preset_id: {preset_id!r}")

        # 1.5. 装配 JsonlTraceSink（每 thread 独立文件，按 thread_id 拆）
        # 路径策略：在 cfg.trace.output_path 文件名上插入 thread_id。
        # 例：cfg.trace.output_path=".kongming/trace.jsonl"
        #   → 实际文件 ".kongming/trace.{thread_id}.jsonl"
        # 不改 cfg schema；多 thread 互相隔离；CLI 单 thread 仍写原文件互不冲突。
        # 复用 thread_manager 传进来的 sinks list（list 是引用类型，append 即生效）。
        if isinstance(sinks, list):
            trace_path = Path(real_cfg.trace.output_path)
            stem = trace_path.stem  # e.g. "trace"
            suffix = trace_path.suffix  # e.g. ".jsonl"
            per_thread_path = trace_path.with_name(f"{stem}.{thread_id}{suffix}")
            jsonl_sink = JsonlTraceSink(
                per_thread_path,
                auto_flush=real_cfg.trace.auto_flush,
            )
            sinks.append(jsonl_sink)

        # 2. Resolve API key from env
        api_key = ""
        if preset.api_key_env:
            api_key = os.environ.get(preset.api_key_env, "")

        # 3. Build per-preset ModelConfig (keep cfg.model defaults, override preset fields)
        model_overrides: dict[str, Any] = {
            "name": preset.model,
            "base_url": preset.base_url,
            "api_key": api_key,
        }
        if preset.provider is not None:
            model_overrides["provider"] = preset.provider
        if preset.reasoning_effort is not None:
            model_overrides["reasoning_effort"] = preset.reasoning_effort
        preset_model = real_cfg.model.model_copy(update=model_overrides)
        preset_cfg = real_cfg.model_copy(update={"model": preset_model})

        # 4. Instructions + registry: lazy-load on first call, then cache.
        # v0.1.6 起 skill_specs 必须经 async load_skill_specs 取得，所以连带
        # registry 一起 lazy 化；同一把锁保证 instructions / registry 同步装配。
        if _instructions_cache[0] is None:
            async with _cache_lock:
                if _instructions_cache[0] is None:
                    rendered, origins = await assemble_instructions(kongming_home=home)
                    sink_list = list(sinks) if sinks else []  # type: ignore[call-overload]
                    skill_specs_list = await load_skill_specs(
                        home,
                        workspace=Path.cwd(),
                        event_sinks=sink_list,
                    )
                    listing = format_skill_listing(skill_specs_list)
                    if listing:
                        rendered = rendered + f"\n\n# skills\n{listing}"
                        origins = [*origins, "skills"]
                    skill_specs = {s.name: s for s in skill_specs_list}
                    new_registry = build_default_registry(
                        file_enabled=real_cfg.tool.file.enabled,
                        shell_enabled=real_cfg.tool.shell.enabled,
                        shell_timeout_seconds=real_cfg.tool.shell.timeout_seconds,
                        shell_max_stream_bytes=real_cfg.tool.shell.max_stream_bytes,
                        shell_terminate_grace_seconds=real_cfg.tool.shell.terminate_grace_seconds,
                        file_read_max_bytes=real_cfg.tool.file.read_max_bytes,
                        skill_specs=skill_specs or None,
                        skill_event_sinks=sink_list,
                    )
                    _registry_cache[0] = new_registry
                    _enabled_tools_cache[0] = new_registry.names()
                    _instructions_cache[0] = rendered
                    _origins_cache[0] = origins
        instructions = _instructions_cache[0]
        assert instructions is not None
        origins = _origins_cache[0] or []
        registry = _registry_cache[0]
        assert registry is not None
        enabled_tool_names = _enabled_tools_cache[0] or []

        # 5. Build approval: adapter.prompt_approval is a PromptFn, needs wrapping
        prompt_fn = adapter.prompt_approval if real_cfg.approval.mode == "interactive" else None  # type: ignore[attr-defined]
        approval = build_default_approval(real_cfg.approval.mode, prompt_fn=prompt_fn)

        # 6. Session factory with bootstrap
        bootstrap = SessionBootstrap(
            agent_name="kongming-agent",
            model_name=preset_cfg.model.name,
            instruction_sources=origins,
            instruction_text_hash=f"sha256:{hashlib.sha256(instructions.encode()).hexdigest()}",
            created_at=time.time(),
            cwd=str(Path.cwd()),
        )

        def session_factory(sid: str) -> Any:
            return build_session(preset_cfg, sid, bootstrap=bootstrap)

        # 7. Build NativeRuntime
        runtime = NativeRuntime.build(
            preset_cfg,
            event_sinks=list(sinks) if sinks else [],  # type: ignore[call-overload]
            approval=approval,
            tools=registry,
            enabled_tool_names=enabled_tool_names,
            session_factory=session_factory,
            instructions=instructions,
        )

        # 8. Build SessionBridge (echo_final_content=False for web streaming)
        bridge = SessionBridge(
            runtime=runtime,
            adapter=adapter,  # type: ignore[arg-type]
            session_id=thread_id,
            echo_final_content=False,
        )

        return runtime, bridge

    return factory


if __name__ == "__main__":
    raise SystemExit(main())
