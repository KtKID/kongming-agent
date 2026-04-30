"""Web 鉴权秘钥与密码管理（v0.1.5）。

本模块负责两件互不相干但都"安全敏感"的事：

1. **session signing secret**：itsdangerous 用的 32 字节 secret。env > 文件 > 首次
   随机生成 + 0600 落盘。
2. **password bcrypt hash**：登录密码的 bcrypt 哈希。env > 文件 > 缺失则报错。

落盘路径：

- ``<home>/web/session_secret``：urlsafe-base64 编码的 32 字节 secret
- ``<home>/web/password.hash``：bcrypt hash 字符串（``$2b$...``）

设计要点：

- **0600 权限**：写文件后立即 ``os.chmod(path, 0o600)``，防止其他用户读到。
- **首次 env 必须清空**：``KONGMING_WEB_PASSWORD`` 写盘后调
  ``os.environ.pop`` 把进程内变量清掉，防止 fork 子进程再用到明文。
- **不依赖 web.protocol / fastapi**：本模块是底层 IO + 哈希，独立可测。
- **bcrypt rounds**：用 :mod:`bcrypt` 默认（12，~250ms 单次）。
- **>72 字节自动截断**：bcrypt 协议固有限制，超长直接截断（单用户场景的
  防御性兜底；不抛错以避免 UI/路由侧再做长度校验）。

依赖：``bcrypt>=4.0``（直接调，绕开 passlib 1.7 + bcrypt 4.x 的兼容性 bug）。
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SESSION_SECRET_BYTES = 32
"""itsdangerous secret 长度（字节）。"""

ENV_SESSION_SECRET = "KONGMING_WEB_SECRET"
"""可选 env：urlsafe-base64 编码的 32 字节 secret。"""

ENV_WEB_PASSWORD = "KONGMING_WEB_PASSWORD"
"""可选 env：明文密码，首次写入后立即从进程 env 弹出。"""

SESSION_SECRET_FILENAME = "session_secret"
PASSWORD_HASH_FILENAME = "password.hash"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WebAuthNotConfiguredError(RuntimeError):
    """缺少必要的鉴权配置（password.hash 文件 + env 都没有）。

    抛出时机：``load_or_init_password_hash`` 没有 env，盘上也没有 hash 文件。
    用户首次启动时应当设置 :data:`ENV_WEB_PASSWORD`。
    """


# ---------------------------------------------------------------------------
# session secret
# ---------------------------------------------------------------------------


def _web_dir(home_dir: Path) -> Path:
    """返回 web 鉴权数据子目录（``<home>/web``）。

    不创建；调用方按需 ``mkdir(parents=True, exist_ok=True)``。
    """
    return home_dir / "web"


def _session_secret_path(home_dir: Path) -> Path:
    return _web_dir(home_dir) / SESSION_SECRET_FILENAME


def _password_hash_path(home_dir: Path) -> Path:
    return _web_dir(home_dir) / PASSWORD_HASH_FILENAME


def _ensure_web_dir(home_dir: Path) -> None:
    web_dir = _web_dir(home_dir)
    web_dir.mkdir(parents=True, exist_ok=True)


def _write_secret_text(path: Path, text: str) -> None:
    """以 0600 权限原子写入文本。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:
        # Windows 下 chmod 部分场景不生效；记 warning 不阻断
        logger.warning("chmod 0600 failed for %s: %s", tmp, exc)
    os.replace(tmp, path)
    # 防御性：再 chmod 一次目标路径（os.replace 在某些平台保留旧 inode 权限）
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("chmod 0600 failed for %s: %s", path, exc)


def load_or_init_session_secret(home_dir: Path) -> bytes:
    """解析 session 签名 secret，返回 32 字节裸数据。

    优先级：

    1. env :data:`ENV_SESSION_SECRET`（urlsafe-base64 编码）→ 解码返回；
       解码失败抛 :class:`ValueError`。
    2. 盘上 ``<home>/web/session_secret`` 存在 → 读 + base64 解码返回。
    3. 都没有 → 调 :func:`secrets.token_bytes` 生成 32 字节，写盘 + 0600，
       并返回。

    Args:
        home_dir: ``.kongming/`` 根目录。函数会确保 ``<home>/web/`` 存在。

    Returns:
        32 字节 secret。

    Raises:
        ValueError: env 提供的 secret 不是合法 urlsafe-base64 / 长度不对。
    """
    env_val = os.environ.get(ENV_SESSION_SECRET)
    if env_val and env_val.strip():
        cleaned = env_val.strip()
        # urlsafe_b64decode 不支持 validate=；先用 b64decode(altchars="-_", validate=True)
        # 拒非法字符；validate 不接受 padding 缺失/字母外字符
        try:
            decoded = base64.b64decode(cleaned, altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{ENV_SESSION_SECRET} is not valid urlsafe-base64: {exc}") from exc
        if len(decoded) != SESSION_SECRET_BYTES:
            raise ValueError(
                f"{ENV_SESSION_SECRET} decodes to {len(decoded)} bytes; "
                f"expected exactly {SESSION_SECRET_BYTES}"
            )
        return decoded

    path = _session_secret_path(home_dir)
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8").strip()
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) == SESSION_SECRET_BYTES:
                return decoded
            logger.warning(
                "session_secret at %s has unexpected length %d; regenerating",
                path,
                len(decoded),
            )
        except (ValueError, TypeError, OSError) as exc:
            logger.warning(
                "failed to read session_secret at %s: %s; regenerating",
                path,
                exc,
            )

    # 首次生成（或既有文件损坏）
    _ensure_web_dir(home_dir)
    fresh = secrets.token_bytes(SESSION_SECRET_BYTES)
    encoded = base64.urlsafe_b64encode(fresh).decode("ascii")
    _write_secret_text(path, encoded)
    return fresh


# ---------------------------------------------------------------------------
# password hash
# ---------------------------------------------------------------------------


_BCRYPT_MAX_PASSWORD_BYTES = 72
"""bcrypt 协议固有限制：超 72 字节会被截断（部分 backend 直接抛错）。

我们不抛 ValueError，按 bcrypt 协议惯例自动截断 —— 单用户场景不会有这么长
的密码，截断仅是防御性兜底。"""


def _truncate_password(plain: str) -> bytes:
    """把明文密码按 UTF-8 编码 + 截断到 72 字节（bcrypt 协议上限）。"""
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    return raw


def hash_password(plain: str) -> str:
    """计算 bcrypt hash。

    直接调用 :mod:`bcrypt` 而非 ``passlib`` —— passlib 1.7.4 + bcrypt 4.x
    存在 wrap bug 探测时传 >72 字节明文触发新 bcrypt 拒绝的兼容性问题。
    直接用 bcrypt 包更可靠，单用户场景也没必要走 passlib 的多算法切换。

    Args:
        plain: 明文密码（非空字符串）。

    Returns:
        ``$2b$...`` 格式的 bcrypt hash 字符串。

    Raises:
        ValueError: 密码空。
    """
    if not plain:
        raise ValueError("password must not be empty")
    import bcrypt

    raw = _truncate_password(plain)
    hashed = bcrypt.hashpw(raw, bcrypt.gensalt())
    return hashed.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """对照 hash 校验明文密码。

    密码 / hash 任一为空时返回 False（不抛），便于路由层统一处理。
    坏 hash 也认为 verify 失败，不抛。
    """
    if not plain or not hashed:
        return False
    import bcrypt

    try:
        return bool(bcrypt.checkpw(_truncate_password(plain), hashed.encode("ascii")))
    except (ValueError, TypeError) as exc:
        # hash 格式坏 / 编码异常 → False，不抛
        logger.warning("verify_password: invalid hash format: %s", exc)
        return False


def load_or_init_password_hash(home_dir: Path) -> str:
    """解析或初始化登录密码 hash。

    优先级：

    1. env :data:`ENV_WEB_PASSWORD`（明文）→ ``hash_password`` 后写盘 0600，
       并 ``os.environ.pop`` 清空 env。**不**回读文件，直接返回新算的 hash。
    2. 盘上 ``<home>/web/password.hash`` 存在 → 读返回。
    3. 都没有 → 抛 :class:`WebAuthNotConfiguredError`。

    Args:
        home_dir: ``.kongming/`` 根目录。

    Returns:
        bcrypt hash 字符串。

    Raises:
        WebAuthNotConfiguredError: env 与文件都缺。
    """
    env_val = os.environ.get(ENV_WEB_PASSWORD)
    if env_val:
        # env 可能是 ""；strip 后为空也视作未设置（避免误把空字符串当合法密码）
        plain = env_val.strip()
        if plain:
            new_hash = hash_password(plain)
            path = _password_hash_path(home_dir)
            _ensure_web_dir(home_dir)
            _write_secret_text(path, new_hash)
            # 清掉 env，防止子进程 / 错误日志泄露
            os.environ.pop(ENV_WEB_PASSWORD, None)
            return new_hash

    path = _password_hash_path(home_dir)
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WebAuthNotConfiguredError(
                f"failed to read password hash at {path}: {exc}"
            ) from exc
        if existing:
            return existing

    raise WebAuthNotConfiguredError(
        f"web auth not configured: set {ENV_WEB_PASSWORD} env on first run (checked file at {path})"
    )


__all__ = [
    "ENV_SESSION_SECRET",
    "ENV_WEB_PASSWORD",
    "PASSWORD_HASH_FILENAME",
    "SESSION_SECRET_BYTES",
    "SESSION_SECRET_FILENAME",
    "WebAuthNotConfiguredError",
    "hash_password",
    "load_or_init_password_hash",
    "load_or_init_session_secret",
    "verify_password",
]
