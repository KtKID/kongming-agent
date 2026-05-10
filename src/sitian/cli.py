from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from config_loader import load_config
from config_loader.models import Config
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

    - root_dir 是 ``--root-dir`` 传的值（或 None → 走默认 ``~/.kongming/SiTian``）
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
    result = await SiTianRunOnce(cfg, store=store)
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
    await SiTianRunLoop(cfg, store=store)


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


__all__ = ["main"]
