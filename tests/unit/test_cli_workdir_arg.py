"""``--workdir / -C`` 参数：CLI 启动时切换工作目录。

测试覆盖：

- ``--help`` 列出选项（用 CliRunner 跑 ``--help``，eager 退出，不进入 main 函数体）
- click 自带校验：路径不存在 / 不是目录 → 非零退出（同上，校验失败不进入主函数体）
- ``_chdir_or_exit`` 成功 / 失败行为
- 直接 ``await _run(workdir=...)`` 验证早期 chdir（与 ``test_cli_main.py`` 保持同款 pattern；
  不通过 CliRunner 触发 ``asyncio.run`` 关默认 event loop，避免污染
  ``test_instruction_loader.py`` 等使用 ``asyncio.get_event_loop()`` 的旧式测试）

注：``-C`` 短选项语义由 click 自身的 alias 装配保证；``test_workdir_appears_in_help``
已在 ``--help`` 输出中验证 ``-C`` 已注册，无需再起独立测试。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import cli.main as cli_main


@pytest.fixture(autouse=True)
def _restore_cwd() -> Iterator[None]:
    """每个测试结束后恢复原 cwd，避免 chdir 污染后续用例。"""
    original = Path.cwd()
    try:
        yield
    finally:
        os.chdir(original)


# ---------------------------------------------------------------------------
# 仅靠 click 校验/--help 的安全测试（不会触发 asyncio.run）
# ---------------------------------------------------------------------------


def test_workdir_appears_in_help() -> None:
    """``--help`` 输出应包含 ``--workdir`` 与短选项 ``-C``。"""
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "--workdir" in result.output
    assert "-C" in result.output


def test_workdir_rejects_nonexistent_path(tmp_path: Path) -> None:
    """传不存在的路径 → click 校验失败，非零 exit（不进入 main 函数体）。"""
    runner = CliRunner()
    bogus = tmp_path / "definitely-not-here"
    result = runner.invoke(cli_main.main, ["--workdir", str(bogus)])
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower()


def test_workdir_rejects_file_path(tmp_path: Path) -> None:
    """传文件路径（非目录）→ click 校验失败（不进入 main 函数体）。"""
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--workdir", str(f)])
    assert result.exit_code != 0
    assert "is a file" in result.output.lower()


# ---------------------------------------------------------------------------
# _chdir_or_exit 行为单测
# ---------------------------------------------------------------------------


def test_chdir_or_exit_success(tmp_path: Path) -> None:
    """``_chdir_or_exit`` 成功路径：进程 cwd 切到目标目录。"""
    cli_main._chdir_or_exit(tmp_path)
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_chdir_or_exit_failure_raises_systemexit(tmp_path: Path) -> None:
    """``_chdir_or_exit`` 失败时 ``SystemExit(2)``。

    覆盖 click 路径校验拦不住的边界（race / 权限），直接传不存在的
    路径调内部函数。
    """
    bogus = tmp_path / "no-such-dir"
    with pytest.raises(SystemExit) as excinfo:
        cli_main._chdir_or_exit(bogus)
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 直接 await _run 触发 chdir 的集成单测（同 test_cli_main.py pattern）
# ---------------------------------------------------------------------------


async def test_run_with_workdir_chdirs_before_load_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run(workdir=...)`` 必须在 ``_load_config_or_exit`` 之前 chdir 完成。"""
    captured: dict[str, Path] = {}

    def _capture_cfg_load(_cfg_path: Path | None) -> None:
        captured["cwd_at_load"] = Path.cwd()
        raise SystemExit(99)

    monkeypatch.setattr(cli_main, "_load_config_or_exit", _capture_cfg_load)

    with pytest.raises(SystemExit) as excinfo:
        await cli_main._run(
            config_path=None,
            session_id=None,
            list_sessions=False,
            resume_last=False,
            verbose=False,
            smoke=False,
            instructions_files=[],
            trace_enabled=False,
            workdir=tmp_path,
        )

    assert excinfo.value.code == 99
    assert captured["cwd_at_load"].resolve() == tmp_path.resolve()


async def test_run_resolves_relative_config_path_before_chdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config <relative>`` 必须在 chdir 之前被 resolve 成绝对路径。

    回归测试：``cli.sh`` 写死 ``--config config/setting.yaml``（相对路径）；
    若先 chdir 到 ``--workdir`` 再 resolve，相对路径就落到新 cwd 下找不到，
    报 ``config file not found``。修复方式：在 chdir 前 resolve config_path。
    """
    captured: dict[str, Path] = {}

    def _capture_load(cfg_path: Path | None) -> None:
        assert cfg_path is not None
        captured["config_path"] = cfg_path
        captured["cwd_at_load"] = Path.cwd()
        raise SystemExit(99)

    monkeypatch.setattr(cli_main, "_load_config_or_exit", _capture_load)

    relative_config = Path("config/setting.yaml")
    assert not relative_config.is_absolute(), "fixture 前提：传入的是相对路径"

    with pytest.raises(SystemExit) as excinfo:
        await cli_main._run(
            config_path=relative_config,
            session_id=None,
            list_sessions=False,
            resume_last=False,
            verbose=False,
            smoke=False,
            instructions_files=[],
            trace_enabled=False,
            workdir=tmp_path,
        )

    assert excinfo.value.code == 99
    # config_path 已被 resolve 成绝对路径
    assert captured["config_path"].is_absolute(), (
        f"config_path should be resolved before chdir; got {captured['config_path']}"
    )
    # cwd 也已切到 workdir（确认 chdir 也发生了）
    assert captured["cwd_at_load"].resolve() == tmp_path.resolve()
    # config_path 不应被 workdir 污染（resolve 用的是原 cwd，不是 workdir）
    assert tmp_path.resolve() not in captured["config_path"].parents, (
        f"config_path resolved against workdir instead of original cwd: {captured['config_path']}"
    )


def test_resolve_helper_resolves_relative_config(tmp_path: Path) -> None:
    """``_resolve_input_paths_before_chdir`` 把相对 config_path 转绝对。"""
    relative = Path("config/setting.yaml")
    cfg_path, _ = cli_main._resolve_input_paths_before_chdir(relative, [])
    assert cfg_path is not None
    assert cfg_path.is_absolute()
    assert str(cfg_path).endswith("config/setting.yaml")


def test_resolve_helper_keeps_absolute_config_unchanged(tmp_path: Path) -> None:
    """绝对路径 config_path 原样返回（不调 resolve 多余动作）。"""
    absolute = (tmp_path / "x.yaml").resolve()
    cfg_path, _ = cli_main._resolve_input_paths_before_chdir(absolute, [])
    assert cfg_path == absolute


def test_resolve_helper_passes_through_none_config() -> None:
    """``config_path=None`` 不动。"""
    cfg_path, _ = cli_main._resolve_input_paths_before_chdir(None, [])
    assert cfg_path is None


def test_resolve_helper_resolves_relative_instructions_files(tmp_path: Path) -> None:
    """相对 instructions_files 全部转绝对；混合输入保持顺序。"""
    rel = Path("prompts/role.md")
    abs_path = (tmp_path / "x.md").resolve()
    _, resolved = cli_main._resolve_input_paths_before_chdir(None, [rel, abs_path])
    assert len(resolved) == 2
    assert resolved[0].is_absolute()
    assert str(resolved[0]).endswith("prompts/role.md")
    assert resolved[1] == abs_path


async def test_run_passes_resolved_config_to_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全链路：``_run(workdir=tmp, config_path=relative)`` 后，
    ``_load_config_or_exit`` 收到的 config_path 必须是绝对路径，
    且不被 workdir 污染。回归用例。
    """
    captured: dict[str, Path] = {}

    def _capture_load(cfg_path: Path | None) -> None:
        assert cfg_path is not None
        captured["config_path"] = cfg_path
        captured["cwd_at_load"] = Path.cwd()
        raise SystemExit(99)

    monkeypatch.setattr(cli_main, "_load_config_or_exit", _capture_load)

    relative_config = Path("config/setting.yaml")

    with pytest.raises(SystemExit) as excinfo:
        await cli_main._run(
            config_path=relative_config,
            session_id=None,
            list_sessions=False,
            resume_last=False,
            verbose=False,
            smoke=False,
            instructions_files=[],
            trace_enabled=False,
            workdir=tmp_path,
        )

    assert excinfo.value.code == 99
    assert captured["config_path"].is_absolute()
    # config_path 不应在 workdir 之下（说明 resolve 用的是原 cwd 而非新 workdir）
    assert tmp_path.resolve() not in captured["config_path"].parents
    # cwd 已切到 workdir
    assert captured["cwd_at_load"].resolve() == tmp_path.resolve()


async def test_run_without_workdir_does_not_chdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 ``workdir`` 时不调用 ``_chdir_or_exit``。"""
    chdir_calls: list[Path] = []

    def _track_chdir(p: Path) -> None:
        chdir_calls.append(p)

    def _capture_cfg_load(_cfg_path: Path | None) -> None:
        raise SystemExit(99)

    monkeypatch.setattr(cli_main, "_chdir_or_exit", _track_chdir)
    monkeypatch.setattr(cli_main, "_load_config_or_exit", _capture_cfg_load)

    with pytest.raises(SystemExit) as excinfo:
        await cli_main._run(
            config_path=None,
            session_id=None,
            list_sessions=False,
            resume_last=False,
            verbose=False,
            smoke=False,
            instructions_files=[],
            trace_enabled=False,
            workdir=None,
        )

    assert excinfo.value.code == 99
    assert chdir_calls == []
