"""auth_secrets.py 单测（v0.1.5 web 鉴权秘钥与密码管理）。

覆盖点：

1. session_secret：env > 文件 > 首次随机生成 + 0600 权限
2. session_secret env 解码失败 / 长度不对 → ValueError
3. password_hash：env 落盘 + 清空 env / 文件读取 / 缺配置抛
   :class:`WebAuthNotConfiguredError`
4. hash_password / verify_password 基本正确性
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from hosts.web.auth.secrets import (
    ENV_SESSION_SECRET,
    ENV_WEB_PASSWORD,
    PASSWORD_HASH_FILENAME,
    SESSION_SECRET_BYTES,
    SESSION_SECRET_FILENAME,
    WebAuthNotConfiguredError,
    hash_password,
    load_or_init_password_hash,
    load_or_init_session_secret,
    verify_password,
)

# ---------------------------------------------------------------------------
# session_secret
# ---------------------------------------------------------------------------


def test_session_secret_env_takes_priority(monkeypatch, tmp_path: Path) -> None:
    """ENV 提供合法 base64 secret 时直接返回，不读盘。"""
    fresh = b"\x01" * SESSION_SECRET_BYTES
    encoded = base64.urlsafe_b64encode(fresh).decode("ascii")
    monkeypatch.setenv(ENV_SESSION_SECRET, encoded)

    secret = load_or_init_session_secret(tmp_path)

    assert secret == fresh
    # 不应落盘
    assert not (tmp_path / "web" / SESSION_SECRET_FILENAME).exists()


def test_session_secret_env_invalid_length(monkeypatch, tmp_path: Path) -> None:
    """ENV 长度不对 → ValueError。"""
    encoded = base64.urlsafe_b64encode(b"only-10-bytes").decode("ascii")
    monkeypatch.setenv(ENV_SESSION_SECRET, encoded)

    with pytest.raises(ValueError, match="expected exactly 32"):
        load_or_init_session_secret(tmp_path)


def test_session_secret_env_invalid_base64(monkeypatch, tmp_path: Path) -> None:
    """ENV 不是合法 base64 → ValueError。"""
    monkeypatch.setenv(ENV_SESSION_SECRET, "@@@not-valid-base64@@@")

    with pytest.raises(ValueError, match="urlsafe-base64"):
        load_or_init_session_secret(tmp_path)


def test_session_secret_first_run_writes_file_with_0600(monkeypatch, tmp_path: Path) -> None:
    """env 不在 + 文件不在 → 生成新 secret，落盘，权限 0600。"""
    monkeypatch.delenv(ENV_SESSION_SECRET, raising=False)

    secret = load_or_init_session_secret(tmp_path)

    assert isinstance(secret, bytes)
    assert len(secret) == SESSION_SECRET_BYTES
    path = tmp_path / "web" / SESSION_SECRET_FILENAME
    assert path.is_file()
    # 权限 0600（仅 owner rw）
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_session_secret_file_persistence(monkeypatch, tmp_path: Path) -> None:
    """二次调用读现有文件，secret 不变。"""
    monkeypatch.delenv(ENV_SESSION_SECRET, raising=False)

    s1 = load_or_init_session_secret(tmp_path)
    s2 = load_or_init_session_secret(tmp_path)

    assert s1 == s2


def test_session_secret_corrupt_file_regenerates(monkeypatch, tmp_path: Path) -> None:
    """文件存在但损坏 → 重新生成新 secret。"""
    monkeypatch.delenv(ENV_SESSION_SECRET, raising=False)
    path = tmp_path / "web" / SESSION_SECRET_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage---not-base64", encoding="utf-8")

    secret = load_or_init_session_secret(tmp_path)

    assert len(secret) == SESSION_SECRET_BYTES
    # 文件被覆盖
    new_raw = path.read_text(encoding="utf-8").strip()
    assert base64.urlsafe_b64decode(new_raw) == secret


# ---------------------------------------------------------------------------
# password_hash
# ---------------------------------------------------------------------------


def test_password_hash_env_writes_file_and_clears_env(monkeypatch, tmp_path: Path) -> None:
    """缺文件时 env 提供明文 → bcrypt 落盘 + 清 env。"""
    monkeypatch.setenv(ENV_WEB_PASSWORD, "test-password-123")

    h = load_or_init_password_hash(tmp_path)

    assert h.startswith("$2")  # bcrypt 标识
    # env 必须被清空
    assert os.environ.get(ENV_WEB_PASSWORD) is None
    # 文件存在 + 0600
    path = tmp_path / "web" / PASSWORD_HASH_FILENAME
    assert path.is_file()
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_password_hash_initial_password_writes_file(monkeypatch, tmp_path: Path) -> None:
    """配置初始密码可完成首启 bootstrap。"""
    monkeypatch.delenv(ENV_WEB_PASSWORD, raising=False)

    h = load_or_init_password_hash(tmp_path, initial_password="bootstrap-password")

    assert h.startswith("$2")
    assert verify_password("bootstrap-password", h) is True
    path = tmp_path / "web" / PASSWORD_HASH_FILENAME
    assert path.is_file()


def test_password_hash_file_only(monkeypatch, tmp_path: Path) -> None:
    """env 不在但文件已落 → 直接读。"""
    monkeypatch.delenv(ENV_WEB_PASSWORD, raising=False)
    h_orig = hash_password("preexisting")
    web_dir = tmp_path / "web"
    web_dir.mkdir(parents=True)
    (web_dir / PASSWORD_HASH_FILENAME).write_text(h_orig, encoding="utf-8")

    h = load_or_init_password_hash(tmp_path)

    assert h == h_orig


def test_password_hash_existing_file_beats_env(monkeypatch, tmp_path: Path) -> None:
    """已有 password.hash 时保持文件为真源，忽略残留 env。"""
    h_orig = hash_password("persisted-password")
    web_dir = tmp_path / "web"
    web_dir.mkdir(parents=True)
    (web_dir / PASSWORD_HASH_FILENAME).write_text(h_orig, encoding="utf-8")
    monkeypatch.setenv(ENV_WEB_PASSWORD, "stale-env-password")

    h = load_or_init_password_hash(tmp_path)

    assert h == h_orig
    assert verify_password("persisted-password", h) is True
    assert verify_password("stale-env-password", h) is False


def test_password_hash_missing_raises(monkeypatch, tmp_path: Path) -> None:
    """env / 文件都缺 → :class:`WebAuthNotConfiguredError`。"""
    monkeypatch.delenv(ENV_WEB_PASSWORD, raising=False)

    with pytest.raises(WebAuthNotConfiguredError):
        load_or_init_password_hash(tmp_path)


def test_password_hash_empty_env_treated_as_missing(monkeypatch, tmp_path: Path) -> None:
    """env 为空字符串 → 视作未配置。"""
    monkeypatch.setenv(ENV_WEB_PASSWORD, "   ")

    with pytest.raises(WebAuthNotConfiguredError):
        load_or_init_password_hash(tmp_path)


def test_hash_password_empty_raises() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_round_trip() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True
    assert verify_password("wrong", h) is False


def test_verify_password_corrupt_hash_returns_false() -> None:
    """坏 hash → 不抛，直接 False。"""
    assert verify_password("any", "not-a-bcrypt-hash") is False


def test_verify_password_empty_inputs_returns_false() -> None:
    assert verify_password("", "$2b$12$abc") is False
    assert verify_password("pwd", "") is False
