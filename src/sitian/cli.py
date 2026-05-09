from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from config_loader import load_config
from sitian.service import SiTianReadState, SiTianRunLoop, SiTianRunOnce
from sitian.store import SiTianRecordsStore


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
    "--root-dir",
    type=click.Path(file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Override SiTianRecords root directory.",
)
def state_command(root_dir: Path | None) -> None:
    asyncio.run(_print_state(root_dir=root_dir))


async def _run_once(*, config_path: Path | None, root_dir: Path | None) -> None:
    cfg = load_config(config_path)
    store = SiTianRecordsStore(root_dir)
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
    store = SiTianRecordsStore(root_dir)
    await SiTianRunLoop(cfg, store=store)


async def _print_state(*, root_dir: Path | None) -> None:
    payload = await SiTianReadState(store=SiTianRecordsStore(root_dir))
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


__all__ = ["main"]
