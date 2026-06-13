"""Agent 角色库管理器。

本脚本负责 agent role preset 的统一管理，包含内置角色、用户创建角色、
session scoped roster、roundtable participant 解析和 workflow 角色快照。
作用是让 tool 层和 workflow strategy 共用同一个角色门户，避免创建、查找、
消耗和审计快照分散在多个模块。
关键执行流程：启动时注入角色目录和内置角色，LLM 通过 tool 调用 list/create，
manager 保存角色并维护 session roster，roundtable strategy 再通过 manager 解析
participants.select 并写 roles.json 快照。
关键函数：list_roles 读取全部角色，create_role 保存或复用角色，
resolve_participants 解析 role id，write_workflow_snapshot 写入本次 workflow 快照。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ROLE_ID_LENGTH = 64
_MAX_TITLE_LENGTH = 80
_MAX_ROLE_LENGTH = 1200
_EMPTY_ROLE_MESSAGE = (
    "No agent roles are available. Call create_agent_role with id, title, and role."
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRolePreset:
    """单个可复用子 agent 角色，输入为稳定 id、短标题和职责说明。"""

    role_id: str
    title: str
    role: str

    def summary(self) -> dict[str, str]:
        """返回 LLM 可见摘要，输入为角色实例，输出只含 id/title/role。"""
        return {"id": self.role_id, "title": self.title, "role": self.role}


@dataclass(frozen=True)
class AgentRoleListResult:
    """角色列表工具结果，输入为 manager 状态，输出给 tool 原样返回。"""

    roles: list[dict[str, str]]
    current_roundtable_agents: list[dict[str, str]]
    empty_message: str | None = None

    def to_data(self) -> dict[str, object]:
        """转换为 tool data，输入为列表结果，输出为 JSON 友好 dict。"""
        data: dict[str, object] = {
            "roles": self.roles,
            "current_roundtable_agents": self.current_roundtable_agents,
        }
        if self.empty_message is not None:
            data["empty_message"] = self.empty_message
        return data


@dataclass(frozen=True)
class AgentRoleCreateResult:
    """角色创建工具结果，输入为创建动作，输出给 tool 原样返回。"""

    saved: bool
    status: Literal["created", "existing"]
    role: dict[str, str]
    path: str
    current_roundtable_agents: list[dict[str, str]]

    def to_data(self) -> dict[str, object]:
        """转换为 tool data，输入为创建结果，输出为 JSON 友好 dict。"""
        return {
            "saved": self.saved,
            "status": self.status,
            "role": self.role,
            "path": self.path,
            "current_roundtable_agents": self.current_roundtable_agents,
        }


class AgentRoleManager:
    """统一管理 agent role preset 的应用层门户。"""

    def __init__(
        self,
        *,
        role_dir: Path,
        builtin_roles: Iterable[AgentRolePreset] = (),
    ) -> None:
        """初始化角色管理器，输入为持久化目录和内置角色，输出为可调用门户。"""
        self._role_dir = role_dir.expanduser().resolve()
        self._builtin_roles = {
            role.role_id: _validated_preset(role.role_id, role.title, role.role)
            for role in builtin_roles
        }
        self._session_rosters: dict[str, list[dict[str, str]]] = {}

    @property
    def role_dir(self) -> Path:
        """返回用户角色目录，输入为 manager 状态，输出为绝对路径。"""
        return self._role_dir

    def list_roles(self) -> tuple[AgentRolePreset, ...]:
        """读取全部角色，输入为空，输出按 role id 排序的角色元组。"""
        roles: dict[str, AgentRolePreset] = dict(self._builtin_roles)
        for path in sorted(self._role_dir.glob("*.json")):
            role = self._read_role_file(path)
            if role is None:
                continue
            roles[role.role_id] = role
        return tuple(roles[key] for key in sorted(roles))

    def list_role_summaries(self, *, session_id: str | None = None) -> AgentRoleListResult:
        """返回角色摘要，输入为可选 session id，输出 roles 和当前 session roster。"""
        summaries = [role.summary() for role in self.list_roles()]
        return AgentRoleListResult(
            roles=summaries,
            current_roundtable_agents=self._roster_for(session_id),
            empty_message=_EMPTY_ROLE_MESSAGE if not summaries else None,
        )

    def get_role(self, role_id: str) -> AgentRolePreset | None:
        """按 id 查询角色，输入为 role id，输出角色或 None。"""
        normalized = _validate_role_id(role_id)
        for role in self.list_roles():
            if role.role_id == normalized:
                return role
        return None

    def create_role(
        self,
        *,
        session_id: str,
        role_id: str,
        title: str,
        role: str,
    ) -> AgentRoleCreateResult:
        """创建或复用角色，输入为 session 和三字段，输出创建结果与 roster。"""
        normalized = _validate_role_id(role_id)
        preset = _validated_preset(normalized, title, role)
        existing = self.get_role(normalized)
        status: Literal["created", "existing"]
        if existing is None:
            path = self._role_path(normalized)
            self._write_role_file(path, preset)
            role_for_result = preset
            status = "created"
            path_text = _display_path(path)
        else:
            role_for_result = existing
            status = "existing"
            path_text = self._path_for_existing(existing)
        self._add_to_roster(session_id, role_for_result.summary())
        return AgentRoleCreateResult(
            saved=True,
            status=status,
            role=role_for_result.summary(),
            path=path_text,
            current_roundtable_agents=self._roster_for(session_id),
        )

    def resolve_participants(self, role_ids: Sequence[str]) -> tuple[AgentRolePreset, ...]:
        """解析 participant id 列表，输入为 role ids，输出去重保序角色元组。"""
        if not role_ids:
            raise ValueError("participants.select is required")
        resolved: list[AgentRolePreset] = []
        seen: set[str] = set()
        for raw_id in role_ids:
            normalized = _validate_role_id(raw_id)
            if normalized in seen:
                continue
            role = self.get_role(normalized)
            if role is None:
                raise ValueError(f"unknown role id: {normalized}")
            resolved.append(role)
            seen.add(normalized)
        if not resolved:
            raise ValueError("participants.select is required")
        return tuple(resolved)

    def write_workflow_snapshot(
        self,
        workflow_dir: Path,
        roles: Sequence[AgentRolePreset],
    ) -> Path:
        """写入 workflow 角色快照，输入为目录和角色，输出 roles.json 路径。"""
        path = workflow_dir / "roles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": "agent_role_manager",
            "roles": [role.summary() for role in roles],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def _read_role_file(self, path: Path) -> AgentRolePreset | None:
        """读取单个角色 JSON，输入为路径，输出角色或 None。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("skip invalid agent role file %s: root is not object", path)
            return None
        try:
            role_id = str(raw.get("id") or raw.get("role_id") or "")
            title = str(raw.get("title") or "")
            role = str(raw.get("role") or "")
            return _validated_preset(role_id, title, role)
        except ValueError as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None

    def _write_role_file(self, path: Path, role: AgentRolePreset) -> None:
        """写入角色 JSON，输入为目标路径和角色，输出为文件落盘。"""
        self._role_dir.mkdir(parents=True, exist_ok=True)
        payload = role.summary()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _role_path(self, role_id: str) -> Path:
        """生成角色 JSON 路径，输入为规范 role id，输出为角色文件路径。"""
        path = (self._role_dir / f"{role_id}.json").resolve()
        if path.parent != self._role_dir:
            raise ValueError("invalid role id")
        return path

    def _path_for_existing(self, role: AgentRolePreset) -> str:
        """返回已有角色路径，输入为角色，输出内置或文件路径文本。"""
        path = self._role_path(role.role_id)
        if path.exists():
            return _display_path(path)
        return f"builtin:{role.role_id}"

    def _add_to_roster(self, session_id: str, summary: dict[str, str]) -> None:
        """加入 session roster，输入为 session 和角色摘要，输出为内存状态更新。"""
        if not session_id:
            raise ValueError("session_id is required")
        roster = self._session_rosters.setdefault(session_id, [])
        role_id = summary["id"]
        if all(item.get("id") != role_id for item in roster):
            roster.append(dict(summary))

    def _roster_for(self, session_id: str | None) -> list[dict[str, str]]:
        """读取 session roster，输入为可选 session，输出摘要副本。"""
        if not session_id:
            return []
        return [dict(item) for item in self._session_rosters.get(session_id, [])]


def _validated_preset(role_id: str, title: str, role: str) -> AgentRolePreset:
    """校验并构造角色，输入为三字段，输出 AgentRolePreset。"""
    normalized = _validate_role_id(role_id)
    normalized_title = _validate_text(title, field="title", max_length=_MAX_TITLE_LENGTH)
    normalized_role = _validate_text(role, field="role", max_length=_MAX_ROLE_LENGTH)
    return AgentRolePreset(role_id=normalized, title=normalized_title, role=normalized_role)


def _validate_role_id(value: str) -> str:
    """校验 role id，输入为原始值，输出为规范 id。"""
    if not isinstance(value, str):
        raise ValueError("invalid role id")
    role_id = value.strip()
    if not role_id or len(role_id) > _MAX_ROLE_ID_LENGTH or not _ROLE_ID_RE.fullmatch(role_id):
        raise ValueError("invalid role id")
    return role_id


def _validate_text(value: str, *, field: str, max_length: int) -> str:
    """校验文本字段，输入为原始值、字段名和长度，输出为去空白文本。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long")
    return text


def _display_path(path: Path) -> str:
    """生成可读路径，输入为绝对路径，输出优先相对当前目录的文本。"""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "AgentRoleCreateResult",
    "AgentRoleListResult",
    "AgentRoleManager",
    "AgentRolePreset",
]
