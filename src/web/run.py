"""Entry point for ``python -m web.run``."""

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
    """CLI entry point."""
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
        app = create_app(
            cfg,
            tm,
            home_dir=home,
            scheduler_runtime_factory=getattr(runtime_factory, "_scheduler_runtime_factory", None),
        )  # type: ignore[arg-type]
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
    """Build a runtime factory for web thread cells and cron runs."""
    from config_loader.models import Config, LLMPresetConfig
    from config_loader.paths import get_kongming_home
    from context import SessionBootstrap, build_session
    from context.instruction_loader import assemble_instructions
    from context.skill_loader import format_skill_listing, load_skill_specs
    from executors.agent_runtime.native_runtime import NativeRuntime
    from host.session_bridge import SessionBridge
    from observability import JsonlTraceSink
    from tools import (
        ToolRegistry,
        build_default_approval,
        build_default_registry,
        register_evolution_write_tool_if_enabled,
        register_schedule_tool_if_enabled,
    )

    assert isinstance(cfg, Config)
    real_cfg: Config = cfg
    preset_map: dict[str, LLMPresetConfig] = {p.id: p for p in real_cfg.web.llm_presets}
    home = get_kongming_home()

    _registry_cache: list[ToolRegistry | None] = [None]
    _enabled_tools_cache: list[list[str] | None] = [None]
    _instructions_cache: list[str | None] = [None]
    _origins_cache: list[list[str] | None] = [None]
    _scheduler_runtime_factory_cache: list[object | None] = [None]
    _cache_lock = asyncio.Lock()

    async def _ensure_shared_assets(sinks: object) -> None:
        if _instructions_cache[0] is not None:
            return

        async with _cache_lock:
            if _instructions_cache[0] is not None:
                return

            sitian_raw = os.environ.get("SITIAN_PROMPT_ROOT", "").strip()
            sitian_root = Path(sitian_raw).expanduser().resolve() if sitian_raw else None

            rendered, origins = await assemble_instructions(
                kongming_home=home,
                sitian_root=sitian_root,
            )
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

            skill_specs = {spec.name: spec for spec in skill_specs_list}
            registry = build_default_registry(
                file_enabled=real_cfg.tool.file.enabled,
                shell_enabled=real_cfg.tool.shell.enabled,
                shell_timeout_seconds=real_cfg.tool.shell.timeout_seconds,
                shell_max_stream_bytes=real_cfg.tool.shell.max_stream_bytes,
                shell_terminate_grace_seconds=real_cfg.tool.shell.terminate_grace_seconds,
                file_read_max_bytes=real_cfg.tool.file.read_max_bytes,
                skill_specs=skill_specs or None,
                skill_event_sinks=sink_list,
            )
            enabled_tool_names = [
                name for name in registry.names() if name != "evolution_write"
            ]

            cron_dispatcher = None
            if real_cfg.scheduler.enabled:
                from scheduler.delivery import DeliveryDispatcher
                from web.cron_delivery import WebDeliverySink
                from web.cron_ws import get_broker

                cron_dispatcher = DeliveryDispatcher(
                    web_sink=WebDeliverySink(get_broker()),
                )

            def _scheduler_runtime_factory(store):  # type: ignore[no-untyped-def]
                from scheduler.runtime_factory import build_cron_execution_bridge

                return build_cron_execution_bridge(
                    real_cfg,
                    store,
                    event_sinks=sink_list,
                    tools=registry,
                    enabled_tool_names=enabled_tool_names,
                    dispatcher=cron_dispatcher,
                    preset_map=preset_map,
                )

            register_schedule_tool_if_enabled(
                registry,
                real_cfg,
                runtime_factory_fn=_scheduler_runtime_factory,
            )
            register_evolution_write_tool_if_enabled(
                registry,
                real_cfg,
                event_sinks=sink_list,
            )

            _registry_cache[0] = registry
            _enabled_tools_cache[0] = enabled_tool_names
            _instructions_cache[0] = rendered
            _origins_cache[0] = origins
            _scheduler_runtime_factory_cache[0] = _scheduler_runtime_factory

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: object,
        sinks: object,
    ) -> tuple[Any, Any]:
        preset = preset_map.get(preset_id)
        if preset is None:
            raise ValueError(f"unknown preset_id: {preset_id!r}")

        if isinstance(sinks, list):
            trace_path = Path(real_cfg.trace.output_path)
            stem = trace_path.stem
            suffix = trace_path.suffix
            per_thread_path = trace_path.with_name(f"{stem}.{thread_id}{suffix}")
            sinks.append(
                JsonlTraceSink(
                    per_thread_path,
                    auto_flush=real_cfg.trace.auto_flush,
                )
            )

        api_key = os.environ.get(preset.api_key_env, "") if preset.api_key_env else ""
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

        await _ensure_shared_assets(sinks)
        setattr(factory, "_scheduler_runtime_factory", _scheduler_runtime_factory_cache[0])
        instructions = _instructions_cache[0]
        assert instructions is not None
        origins = _origins_cache[0] or []
        registry = _registry_cache[0]
        assert registry is not None
        enabled_tool_names = _enabled_tools_cache[0] or []

        prompt_fn = adapter.prompt_approval if real_cfg.approval.mode == "interactive" else None  # type: ignore[attr-defined]
        approval = build_default_approval(real_cfg.approval.mode, prompt_fn=prompt_fn)

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

        runtime = NativeRuntime.build(
            preset_cfg,
            event_sinks=list(sinks) if sinks else [],  # type: ignore[call-overload]
            approval=approval,
            tools=registry,
            enabled_tool_names=enabled_tool_names,
            session_factory=session_factory,
            instructions=instructions,
        )
        bridge = SessionBridge(
            runtime=runtime,
            adapter=adapter,  # type: ignore[arg-type]
            session_id=thread_id,
            echo_final_content=False,
        )
        return runtime, bridge

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_ensure_shared_assets([]))
    setattr(factory, "_scheduler_runtime_factory", _scheduler_runtime_factory_cache[0])
    return factory


if __name__ == "__main__":
    raise SystemExit(main())
