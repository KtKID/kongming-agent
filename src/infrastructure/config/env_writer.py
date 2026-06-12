"""宿主无关 `.env` 写回工具。

本模块负责把受控环境变量写入 `.env` 文件，并同步当前进程的
``os.environ``。它只处理 key/value 文本，不感知 Web、模型服务商或 UI。
"""

from __future__ import annotations

import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class EnvWriterError(ValueError):
    """`.env` 写回错误。"""


@dataclass(frozen=True)
class EnvWriteResult:
    """`.env` 写回结果。"""

    path: str
    updated_keys: list[str]
    new_mtime: float


def write_env_values(env_path: Path, values: dict[str, str]) -> EnvWriteResult:
    """更新 `.env` 文件并同步 ``os.environ``。

    已存在的 key 原地替换，新增 key 追加到文件末尾。注释、空行和无关变量
    保持原有顺序。
    """
    invalid = [key for key in values if not _ENV_NAME_RE.fullmatch(key)]
    if invalid:
        raise EnvWriterError(f"invalid env variable name: {invalid[0]}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    original_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = {key: str(value) for key, value in values.items()}
    output: list[str] = []

    for line in original_lines:
        parsed_key = _parse_env_key(line)
        if parsed_key is not None and parsed_key in remaining:
            output.append(f"{parsed_key}={_quote_env_value(remaining.pop(parsed_key))}")
            continue
        output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={_quote_env_value(value)}")

    tmp_path = _make_tmp_path(env_path)
    try:
        tmp_path.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
        tmp_path.replace(env_path)
    finally:
        if tmp_path.exists():
            with suppress(OSError):
                tmp_path.unlink()
    for key, value in values.items():
        os.environ[key] = str(value)
    return EnvWriteResult(
        path=str(env_path),
        updated_keys=list(values.keys()),
        new_mtime=env_path.stat().st_mtime,
    )


def _parse_env_key(line: str) -> str | None:
    """从一行 `.env` 文本中解析 key。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key if _ENV_NAME_RE.fullmatch(key) else None


def _quote_env_value(value: str) -> str:
    """生成保守 `.env` value 表达。"""
    if (
        value == ""
        or any(ch.isspace() for ch in value)
        or any(ch in value for ch in ['"', "#", "="])
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _make_tmp_path(env_path: Path) -> Path:
    """构造与目标 `.env` 同目录的临时文件路径。"""
    return env_path.with_name(f"{env_path.name}.tmp.{os.getpid()}.{time.time_ns()}")


__all__ = ["EnvWriteResult", "EnvWriterError", "write_env_values"]
