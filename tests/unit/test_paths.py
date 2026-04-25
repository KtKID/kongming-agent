"""unit：config_loader.paths.get_kongming_home() 覆盖。

模块 1 / task prompt-modules-v0.1.3。覆盖 env KONGMING_HOME 优先级、
`~` 展开、空字符串 fallback、cwd fallback、不触发 I/O 等全部分支。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config_loader import get_kongming_home


@pytest.mark.unit
def test_default_returns_cwd_kongming(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """无 KONGMING_HOME env 时走 cwd fallback 分支，返回 cwd / ".kongming"。"""
    monkeypatch.delenv("KONGMING_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    result = get_kongming_home()

    assert result == (tmp_path / ".kongming").resolve()


@pytest.mark.unit
def test_env_absolute_path_used_as_is(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """KONGMING_HOME 设为绝对路径时原样消费（仅 expanduser + resolve）。"""
    custom = tmp_path / "km-custom"
    monkeypatch.setenv("KONGMING_HOME", str(custom))

    result = get_kongming_home()

    assert result == custom.resolve()
    # 无论 env 里写的是否含 /private 前缀，resolve 后应与期望一致
    assert result.is_absolute()


@pytest.mark.unit
def test_env_relative_path_resolved_against_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """KONGMING_HOME 为相对路径时按 cwd 解析为绝对路径。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KONGMING_HOME", "relative/km")

    result = get_kongming_home()

    assert result == (tmp_path / "relative/km").resolve()
    assert result.is_absolute()


@pytest.mark.unit
def test_env_tilde_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KONGMING_HOME 以 `~` 开头时应调用 expanduser 展开为 $HOME。"""
    monkeypatch.setenv("KONGMING_HOME", "~/km-tilde")

    result = get_kongming_home()

    expected = (Path.home() / "km-tilde").resolve()
    assert result == expected
    # 保证路径里不再有字面量 ~（expanduser 真的生效）
    assert "~" not in str(result)


@pytest.mark.unit
def test_env_empty_string_falls_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """KONGMING_HOME="" 视作未设置，走 cwd fallback（防御 Path("") == cwd 的坑）。"""
    monkeypatch.setenv("KONGMING_HOME", "")
    monkeypatch.chdir(tmp_path)

    result = get_kongming_home()

    assert result == (tmp_path / ".kongming").resolve()


@pytest.mark.unit
def test_env_whitespace_only_falls_back_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """纯空白字符（含 \\t）的 KONGMING_HOME 走 cwd fallback，验证 .strip() 防御。"""
    monkeypatch.setenv("KONGMING_HOME", "   \t  ")
    monkeypatch.chdir(tmp_path)

    result = get_kongming_home()

    assert result == (tmp_path / ".kongming").resolve()


@pytest.mark.unit
def test_does_not_create_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """get_kongming_home 是纯函数，不应触发任何文件系统写入。"""
    monkeypatch.delenv("KONGMING_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    result = get_kongming_home()

    # cwd 分支下 .kongming 不应被创建
    assert not result.exists()

    # env 绝对路径分支同样不创建
    custom = tmp_path / "never-created"
    monkeypatch.setenv("KONGMING_HOME", str(custom))
    result2 = get_kongming_home()
    assert not result2.exists()


@pytest.mark.unit
def test_returned_path_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """所有分支返回的 Path 都必须是绝对路径。"""
    # 1) cwd fallback 分支
    monkeypatch.delenv("KONGMING_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert get_kongming_home().is_absolute()

    # 2) env 绝对路径分支
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / "abs"))
    assert get_kongming_home().is_absolute()

    # 3) env 相对路径分支
    monkeypatch.setenv("KONGMING_HOME", "rel/km")
    assert get_kongming_home().is_absolute()

    # 4) env 空字符串 fallback 分支
    monkeypatch.setenv("KONGMING_HOME", "")
    assert get_kongming_home().is_absolute()

    # 5) env tilde 展开分支
    monkeypatch.setenv("KONGMING_HOME", "~/km-abs-check")
    assert get_kongming_home().is_absolute()
