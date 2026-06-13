"""
司天命令行入口——run-once / loop / state 三个子命令。

Role:
    click 命令组，把 CLI 参数翻译为 service 层调用。
    支持 --config / --root-dir 覆盖默认配置路径和产物目录。

Owns:
    - CLI 参数解析与校验
    - 输出格式化（JSON / 表格 / markdown）
    - 异步事件循环管理

Does not own:
    - 业务逻辑（service.py）
    - 持久化（store.py）
    - 扫描（scanners.py）

Called by:
    - ``python -m sitian.cli`` 或 ``./sitian.sh`` 封装脚本

Key outputs:
    - 终端输出（扫描结果 / 状态查询 / 循环日志）

Change risks:
    - 子命令名/参数变化会影响 sitian.sh 脚本和用户习惯
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

from infrastructure.config import load_config
from infrastructure.config.models import Config

if TYPE_CHECKING:
    from core.contracts import LLMProvider
from sitian.service import SiTianReadState, SiTianRunLoop, SiTianRunOnce
from sitian.store import SiTianRecordsStore, resolve_sitian_root


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """SiTian command line interface."""


@main.command("run-once")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Kongming config path.",
)
@click.option(
    "--root-dir",
    type=click.Path(file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Override SiTianRecords root directory.",
)
def run_once_command(config_path: Path | None, root_dir: Path | None) -> None:
    asyncio.run(_run_once(config_path=config_path, root_dir=root_dir))


@main.command("loop")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Kongming config path.",
)
@click.option(
    "--root-dir",
    type=click.Path(file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Override SiTianRecords root directory.",
)
def loop_command(config_path: Path | None, root_dir: Path | None) -> None:
    asyncio.run(_run_loop(config_path=config_path, root_dir=root_dir))


@main.command("state")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Kongming config path. If set, sitian.output_subdir is honored.",
)
@click.option(
    "--root-dir",
    type=click.Path(file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Override SiTianRecords root directory.",
)
def state_command(config_path: Path | None, root_dir: Path | None) -> None:
    asyncio.run(_print_state(config_path=config_path, root_dir=root_dir))


def _resolve_records_root(root_dir: Path | None, cfg: Config | None) -> Path:
    """根据 cfg.sitian.output_subdir 拼最终 store 路径。

    - root_dir 是 ``--root-dir`` 传的值（或 None → 走默认 ``<kongming_home>/sitian``）
    - 若 cfg.sitian.output_subdir 已设置，进一步拼到子目录
    - 其余产物文件名/结构都由 SiTianRecordsStore 内部决定，本函数只负责 root
    """
    base = resolve_sitian_root(root_dir)
    if cfg is not None and cfg.sitian.output_subdir:
        return base / cfg.sitian.output_subdir
    return base


async def _run_once(*, config_path: Path | None, root_dir: Path | None) -> None:
    cfg = load_config(config_path)
    store = SiTianRecordsStore(_resolve_records_root(root_dir, cfg))
    provider = _build_analyzer_provider(cfg)
    result = await SiTianRunOnce(cfg, store=store, llm_provider=provider)
    click.echo(
        json.dumps(
            {
                "observedAt": result.observed_at,
                "readySourceIds": list(result.ready_source_ids),
                "scannedSourceIds": list(result.scanned_source_ids),
                "failedSources": result.failed_sources,
                "observationCount": result.observation_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def _run_loop(*, config_path: Path | None, root_dir: Path | None) -> None:
    cfg = load_config(config_path)
    store = SiTianRecordsStore(_resolve_records_root(root_dir, cfg))
    provider = _build_analyzer_provider(cfg)
    await SiTianRunLoop(cfg, store=store, llm_provider=provider)


async def _print_state(*, config_path: Path | None, root_dir: Path | None) -> None:
    # state 也读 cfg，让 sitian.output_subdir 自动生效；
    # 不传 --config 时走 KONGMING_CONFIG / 默认 config/setting.yaml。
    # 配置文件不存在时降级为 None（state 只读已有产物，没 cfg 也能跑）。
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = None
    store = SiTianRecordsStore(_resolve_records_root(root_dir, cfg))
    payload = await SiTianReadState(store=store)
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_analyzer_provider(cfg: Config) -> LLMProvider | None:
    """如果 analyzer.enabled 且有有效模型配置，构造 LLM provider；否则返回 None。"""
    analyzer_cfg = cfg.sitian.analyzer
    if not analyzer_cfg.enabled:
        return None
    if not analyzer_cfg.model_name and not analyzer_cfg.base_url:
        return None

    import os

    from infrastructure.config.models import ModelConfig

    api_key = ""
    if analyzer_cfg.api_key_env:
        api_key = os.environ.get(analyzer_cfg.api_key_env, "")

    sitian_model = ModelConfig(
        name=analyzer_cfg.model_name or cfg.model.name,
        base_url=analyzer_cfg.base_url or cfg.model.base_url,
        api_key=api_key or cfg.model.api_key,
        timeout=analyzer_cfg.timeout,
        max_tokens=analyzer_cfg.max_tokens,
        temperature=analyzer_cfg.temperature,
    )
    provider_cfg = cfg.model_copy(update={"model": sitian_model})

    from infrastructure.llm_providers.provider_factory import build_provider

    return build_provider(provider_cfg)


__all__ = ["main"]
