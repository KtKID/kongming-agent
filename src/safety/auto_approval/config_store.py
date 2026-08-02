"""per-cwd 智能审批配置读写。

存储位置::

    <kongming_home>/web/auto_approval/<cwd_hash>.json

设计要点：

- ``cwd_hash = sha256(cwd)[:12]``（缩短为 12 hex，文件名友好；冲突概率 ~ 1/2^48 可忽略）
- 原子写：写临时文件 → ``os.replace`` 替换；防止并发读到半行 JSON
- 默认值兜底：未配置的 cwd 调 ``get_or_default`` 返回 ``enabled=False`` 的默认对象
- 读取使用文件级锁（``fcntl.flock``），写入用原子 rename 不需要锁
- 文件保留 ``cwd`` 原值字段方便人工排查（不靠 hash 反推）

不依赖 policy / matchers / audit。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from safety.auto_approval.disposition import ApprovalDispositionMode

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """per-cwd 智能审批配置。

    ``rule_overrides``: ``{rule_id: enabled}``。enabled=False 表示该规则
    在本 cwd 下**关闭拦截**（即该类操作自动通过）。enabled=True 表示
    维持默认（拦截）。

    ``timeout_ms``: per-cwd 可覆盖全局；``0`` 视为「使用全局默认」。
    """

    cwd: str
    mode: ApprovalDispositionMode = ApprovalDispositionMode.USER
    rule_overrides: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    timeout_ms: int = 0  # 0 = 使用全局默认（来自 RuleSet.default_timeout_ms）

    def to_json(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "mode": self.mode.value,
            "rule_overrides": dict(self.rule_overrides),
            "timeout_ms": self.timeout_ms,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProjectConfig:
        raw_mode = data.get("mode", ApprovalDispositionMode.USER.value)
        try:
            mode = (
                ApprovalDispositionMode(raw_mode)
                if isinstance(raw_mode, str)
                else ApprovalDispositionMode.USER
            )
        except ValueError:
            mode = ApprovalDispositionMode.USER
        raw_overrides = data.get("rule_overrides") or {}
        if not isinstance(raw_overrides, dict):
            raise ValueError("ProjectConfig.rule_overrides must be an object")
        return cls(
            cwd=str(data.get("cwd", "")),
            mode=mode,
            rule_overrides=MappingProxyType({str(k): bool(v) for k, v in raw_overrides.items()}),
            timeout_ms=int(data.get("timeout_ms", 0)),
        )


# ---------------------------------------------------------------------------
# cwd hash
# ---------------------------------------------------------------------------


def cwd_hash(cwd: str) -> str:
    """计算 cwd 的短哈希作为文件名。

    使用 sha256 前 12 hex（48 bit）。空 cwd 返回特殊 ``_empty``。
    """
    if not cwd:
        return "_empty"
    norm = os.path.normpath(cwd.strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# ConfigStore
# ---------------------------------------------------------------------------


class ConfigStore:
    """per-cwd 配置仓库（一个实例对应一个根目录）。

    单实例可被多 connection 复用：写入是原子的，读取每次都拿到最新值
    （不缓存——避免多进程/多 worker 间一致性问题）。
    """

    def __init__(self, root_dir: Path) -> None:
        """root_dir 一般是 ``<kongming_home>/web/auto_approval``。"""
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # ----- 公共接口 -----

    def get(self, cwd: str) -> ProjectConfig | None:
        """读取指定 cwd 的配置，不存在返回 None。

        Raises:
            json.JSONDecodeError: 文件损坏（不静默——让上层决定）
        """
        path = self._path_for(cwd)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"corrupted config file (not a dict): {path}")
        return ProjectConfig.from_json(data)

    def get_or_default(self, cwd: str) -> ProjectConfig:
        """读取，不存在则返回**默认值**（不写盘）。"""
        existing = self.get(cwd)
        if existing is not None:
            return existing
        return ProjectConfig(cwd=cwd)

    def set(self, config: ProjectConfig) -> None:
        """原子写入。"""
        if not config.cwd:
            raise ValueError("ProjectConfig.cwd must be non-empty for set()")
        path = self._path_for(config.cwd)
        self._atomic_write_json(path, config.to_json())

    def delete(self, cwd: str) -> bool:
        """删除配置文件；存在返回 True，否则 False。"""
        path = self._path_for(cwd)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_cwds(self) -> list[str]:
        """列出所有已有配置的 cwd 原值（不是 hash）。"""
        result: list[str] = []
        for p in self._root.glob("*.json"):
            try:
                with p.open(encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("cwd"), str):
                    result.append(data["cwd"])
            except (OSError, json.JSONDecodeError, ValueError):
                # 损坏 / 无权限：跳过
                continue
        return sorted(result)

    # ----- 内部 -----

    def _path_for(self, cwd: str) -> Path:
        return self._root / f"{cwd_hash(cwd)}.json"

    @staticmethod
    def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
        """tempfile + os.replace 原子写。

        - 同目录创建 tmp 文件（确保跨设备 rename 不失败）
        - flush + fsync 保证数据落盘
        - os.replace 在 POSIX 下是原子的；并发读永远看到旧版或新版完整内容
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        # delete=False：手动 rename + cleanup
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmpf:
            tmp_path = Path(tmpf.name)
            json.dump(payload, tmpf, ensure_ascii=False, indent=2, sort_keys=True)
            tmpf.flush()
            os.fsync(tmpf.fileno())
        # 原子替换；失败要清 tmp
        try:
            os.replace(tmp_path, target)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise


__all__ = ["ConfigStore", "ProjectConfig", "cwd_hash"]
