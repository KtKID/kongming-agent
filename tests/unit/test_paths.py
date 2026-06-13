"""unit：infrastructure.config.paths 路径入口覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config import get_kongming_home, resolve_kongming_path


def _patch_user_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home)


@pytest.mark.unit
def test_default_returns_user_home_kongming(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    _patch_user_home(monkeypatch, user_home)
    monkeypatch.delenv("KONGMING_HOME", raising=False)

    result = get_kongming_home()

    assert result == (user_home / ".kongming").resolve()


@pytest.mark.unit
def test_env_absolute_path_used_as_is(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom = tmp_path / "km-custom"
    monkeypatch.setenv("KONGMING_HOME", str(custom))

    result = get_kongming_home()

    assert result == custom.resolve()
    assert result.is_absolute()


@pytest.mark.unit
def test_env_relative_path_resolved_against_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KONGMING_HOME", "relative/km")

    result = get_kongming_home()

    assert result == (tmp_path / "relative/km").resolve()
    assert result.is_absolute()


@pytest.mark.unit
def test_env_tilde_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KONGMING_HOME", "~/km-tilde")

    result = get_kongming_home()

    expected = (Path.home() / "km-tilde").resolve()
    assert result == expected
    assert "~" not in str(result)


@pytest.mark.unit
def test_env_empty_string_falls_back_to_user_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _patch_user_home(monkeypatch, user_home)
    monkeypatch.setenv("KONGMING_HOME", "")
    monkeypatch.chdir(cwd)

    result = get_kongming_home()

    assert result == (user_home / ".kongming").resolve()


@pytest.mark.unit
def test_env_whitespace_only_falls_back_to_user_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _patch_user_home(monkeypatch, user_home)
    monkeypatch.setenv("KONGMING_HOME", "   \t  ")
    monkeypatch.chdir(cwd)

    result = get_kongming_home()

    assert result == (user_home / ".kongming").resolve()


@pytest.mark.unit
def test_does_not_create_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    _patch_user_home(monkeypatch, user_home)
    monkeypatch.delenv("KONGMING_HOME", raising=False)

    result = get_kongming_home()

    assert not result.exists()

    custom = tmp_path / "never-created"
    monkeypatch.setenv("KONGMING_HOME", str(custom))
    result2 = get_kongming_home()
    assert not result2.exists()


@pytest.mark.unit
def test_resolve_kongming_relative_path_uses_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.chdir(cwd)

    result = resolve_kongming_path(".kongming/logs/full_log.jsonl")

    assert result == (home / "logs" / "full_log.jsonl").resolve()


@pytest.mark.unit
def test_resolve_kongming_path_accepts_injected_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "app-home"

    result = resolve_kongming_path(".kongming/trace.jsonl", kongming_home=home)

    assert result == (home / "trace.jsonl").resolve()


@pytest.mark.unit
def test_resolve_other_relative_path_uses_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    result = resolve_kongming_path("custom/log.jsonl")

    assert result == (cwd / "custom" / "log.jsonl").resolve()


@pytest.mark.unit
def test_returned_path_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    _patch_user_home(monkeypatch, user_home)
    monkeypatch.delenv("KONGMING_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    assert get_kongming_home().is_absolute()
    assert resolve_kongming_path(".kongming/sessions").is_absolute()

    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / "abs"))
    assert get_kongming_home().is_absolute()

    monkeypatch.setenv("KONGMING_HOME", "rel/km")
    assert get_kongming_home().is_absolute()

    monkeypatch.setenv("KONGMING_HOME", "")
    assert get_kongming_home().is_absolute()

    monkeypatch.setenv("KONGMING_HOME", "~/km-abs-check")
    assert get_kongming_home().is_absolute()
