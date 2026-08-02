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
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ROLE_ID_LENGTH = 64
_MAX_TITLE_LENGTH = 80
_MAX_ROLE_LENGTH = 1200
_MAX_MODEL_LENGTH = 120
_MAX_REASONING_EFFORT_LENGTH = 32
_EMPTY_ROLE_MESSAGE = (
    "No agent roles are available. Call create_agent_role with id, title, and role."
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRolePreset:
    """单个可复用子 agent 角色，输入为稳定 id、昵称和职责说明。"""

    role_id: str
    nickname: str
    role_desc: str
    model: str = ""
    reasoning_effort: str | None = None
    max_turns: int = 3
    source: Literal["builtin", "runtime", "user"] = "builtin"
    source_path: str = ""
    editable: bool = False

    @property
    def title(self) -> str:
        """返回旧 workflow 使用的展示名，输入为角色实例，输出 nickname。"""
        return self.nickname

    @property
    def role(self) -> str:
        """返回旧 workflow 使用的职责说明，输入为角色实例，输出 role_desc。"""
        return self.role_desc

    def summary(self) -> dict[str, object]:
        """返回 LLM 可见摘要，输入为角色实例，输出 agent.toml 字段。"""
        role_id: int | str = int(self.role_id) if self.role_id.isdecimal() else self.role_id
        return {
            "id": role_id,
            "nickname": self.nickname,
            "model": self.model,
            "role_desc": self.role_desc,
            "reasoning_effort": self.reasoning_effort,
            "max_turns": self.max_turns,
            "source": self.source,
            "path": self.source_path,
            "editable": self.editable,
        }


@dataclass(frozen=True)
class AgentRoleListResult:
    """角色列表工具结果，输入为 manager 状态，输出给 tool 原样返回。"""

    roles: list[dict[str, object]]
    current_roundtable_agents: list[dict[str, object]]
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
    role: dict[str, object]
    path: str
    current_roundtable_agents: list[dict[str, object]]

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
        config_path: Path | None = None,
    ) -> None:
        """初始化角色管理器，输入为持久化目录和内置角色，输出为可调用门户。"""
        self._role_dir = role_dir.expanduser().resolve()
        self._runtime_role_dir = self._role_dir / "runtime"
        self._user_role_dir = self._role_dir / "user"
        self._config_path = config_path.expanduser().resolve() if config_path is not None else None
        injected_builtin_roles = tuple(
            _with_role_metadata(role, source="builtin", source_path="", editable=False)
            for role in builtin_roles
        )
        self._builtin_roles = {
            role.role_id: role
            for role in (*injected_builtin_roles, *_load_config_roles(self._config_path))
        }
        self._session_rosters: dict[str, list[dict[str, object]]] = {}
        self._migrate_legacy_json_roles()

    @property
    def role_dir(self) -> Path:
        """返回用户角色目录，输入为 manager 状态，输出为绝对路径。"""
        return self._role_dir

    def list_roles(self) -> tuple[AgentRolePreset, ...]:
        """读取全部角色，输入为空，输出按 role id 排序的角色元组。"""
        roles: dict[str, AgentRolePreset] = dict(self._builtin_roles)
        role_sources: tuple[tuple[Literal["runtime", "user"], Path], ...] = (
            ("runtime", self._runtime_role_dir),
            ("user", self._user_role_dir),
        )
        for source, directory in role_sources:
            for path in sorted(directory.glob("*.toml")):
                role = self._read_toml_role_file(path, source=source)
                if role is None or role.role_id in roles:
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

    def get_role(self, role_id: object) -> AgentRolePreset | None:
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
        existing = self.get_role(normalized)
        status: Literal["created", "existing"]
        if existing is None:
            path = self._runtime_role_path(normalized)
            preset = _validated_preset(
                normalized,
                title,
                role,
                source="runtime",
                source_path=_display_path(path),
                editable=True,
            )
            self._write_toml_role_file(path, preset)
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

    def resolve_participants(self, role_ids: Sequence[object]) -> tuple[AgentRolePreset, ...]:
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

    def _migrate_legacy_json_roles(self) -> None:
        """迁移旧 JSON role，输入为 role_dir 根目录，输出为 runtime TOML 新文件。"""
        for path in sorted(self._role_dir.glob("*.json")):
            role = self._read_json_role_file(path)
            if role is None:
                continue
            if role.role_id in self._builtin_roles:
                continue
            target = self._runtime_role_path(role.role_id)
            if target.exists():
                continue
            migrated = _with_role_metadata(
                role,
                source="runtime",
                source_path=_display_path(target),
                editable=True,
            )
            self._write_toml_role_file(target, migrated)

    def _read_json_role_file(self, path: Path) -> AgentRolePreset | None:
        """读取单个旧角色 JSON，输入为路径，输出角色或 None。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("skip invalid agent role file %s: root is not object", path)
            return None
        try:
            return _validated_role_mapping(
                raw,
                source="runtime",
                source_path=_display_path(path),
                editable=True,
                field_prefix="agent role file",
            )
        except ValueError as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None

    def _read_toml_role_file(
        self,
        path: Path,
        *,
        source: Literal["runtime", "user"],
    ) -> AgentRolePreset | None:
        """读取单个 runtime/user TOML，输入为路径和来源，输出角色或 None。"""
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("skip invalid agent role file %s: root is not object", path)
            return None
        try:
            return _validated_role_mapping(
                raw,
                source=source,
                source_path=_display_path(path),
                editable=True,
                field_prefix="agent role file",
            )
        except ValueError as exc:
            logger.warning("skip invalid agent role file %s: %s", path, exc)
            return None

    def _write_toml_role_file(self, path: Path, role: AgentRolePreset) -> None:
        """写入角色 TOML，输入为目标路径和角色，输出为文件落盘。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _role_toml_text(role)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    def _runtime_role_path(self, role_id: str) -> Path:
        """生成 runtime TOML 路径，输入为规范 role id，输出为角色文件路径。"""
        path = (self._runtime_role_dir / f"{role_id}.toml").resolve()
        if path.parent != self._runtime_role_dir:
            raise ValueError("invalid role id")
        return path

    def _path_for_existing(self, role: AgentRolePreset) -> str:
        """返回已有角色路径，输入为角色，输出内置或文件路径文本。"""
        if role.source_path:
            return role.source_path
        return f"{role.source}:{role.role_id}"

    def _add_to_roster(self, session_id: str, summary: dict[str, object]) -> None:
        """加入 session roster，输入为 session 和角色摘要，输出为内存状态更新。"""
        if not session_id:
            raise ValueError("session_id is required")
        roster = self._session_rosters.setdefault(session_id, [])
        role_id = str(summary["id"])
        if all(str(item.get("id")) != role_id for item in roster):
            roster.append(dict(summary))

    def _roster_for(self, session_id: str | None) -> list[dict[str, object]]:
        """读取 session roster，输入为可选 session，输出摘要副本。"""
        if not session_id:
            return []
        return [dict(item) for item in self._session_rosters.get(session_id, [])]


def _load_config_roles(config_path: Path | None) -> tuple[AgentRolePreset, ...]:
    """读取 agent.toml 角色配置，输入为配置路径，输出内置角色元组。"""
    if config_path is None or not config_path.exists():
        return ()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid agent config: {config_path}") from exc
    agents = raw.get("agents")
    if not isinstance(agents, list):
        raise ValueError(f"invalid agent config: {config_path}: agents must be an array")
    roles: list[AgentRolePreset] = []
    seen: set[str] = set()
    source_path = _display_path(config_path)
    for index, item in enumerate(agents):
        if not isinstance(item, dict):
            raise ValueError(f"invalid agent config: agents[{index}] must be an object")
        role = _validated_config_preset(item, index=index, source_path=source_path)
        if role.role_id in seen:
            raise ValueError(f"invalid agent config: duplicate id {role.role_id}")
        roles.append(role)
        seen.add(role.role_id)
    return tuple(roles)


def _validated_config_preset(
    raw: dict[str, object],
    *,
    index: int,
    source_path: str,
) -> AgentRolePreset:
    """校验 agent.toml 单个角色，输入为 TOML item，输出 AgentRolePreset。"""
    required = ("id", "nickname", "model", "reasoning_effort", "max_turns")
    missing = [key for key in required if key not in raw]
    role_desc = raw.get("role_desc") or raw.get("desc")
    if role_desc is None:
        missing.append("role_desc")
    if missing:
        raise ValueError(f"invalid agent config: agents[{index}] missing {', '.join(missing)}")
    role_id = raw["id"]
    if not isinstance(role_id, int) or isinstance(role_id, bool) or role_id <= 0:
        raise ValueError(f"invalid agent config: agents[{index}].id must be a positive integer")
    return _validated_preset(
        role_id,
        raw["nickname"],
        role_desc,
        model=raw["model"],
        reasoning_effort=raw["reasoning_effort"],
        max_turns=raw["max_turns"],
        source="builtin",
        source_path=source_path,
        editable=False,
    )


def _validated_role_mapping(
    raw: dict[str, object],
    *,
    source: Literal["runtime", "user"],
    source_path: str,
    editable: bool,
    field_prefix: str,
) -> AgentRolePreset:
    """校验单文件 role 映射，输入为 JSON/TOML 字典，输出 AgentRolePreset。"""
    role_id = raw.get("id") or raw.get("role_id") or ""
    nickname = raw.get("nickname") or raw.get("title") or ""
    role_desc = raw.get("role_desc") or raw.get("role") or ""
    model = raw.get("model") or ""
    reasoning_effort = raw.get("reasoning_effort")
    max_turns = raw.get("max_turns", 3)
    try:
        return _validated_preset(
            role_id,
            nickname,
            role_desc,
            model=model,
            reasoning_effort=reasoning_effort,
            max_turns=max_turns,
            source=source,
            source_path=source_path,
            editable=editable,
        )
    except ValueError as exc:
        raise ValueError(f"invalid {field_prefix}: {exc}") from exc


def _with_role_metadata(
    role: AgentRolePreset,
    *,
    source: Literal["builtin", "runtime", "user"],
    source_path: str,
    editable: bool,
) -> AgentRolePreset:
    """复制 role 并替换来源元信息，输入为 role 和来源，输出新 role。"""
    return AgentRolePreset(
        role_id=role.role_id,
        nickname=role.nickname,
        role_desc=role.role_desc,
        model=role.model,
        reasoning_effort=role.reasoning_effort,
        max_turns=role.max_turns,
        source=source,
        source_path=source_path,
        editable=editable,
    )


def _role_toml_text(role: AgentRolePreset) -> str:
    """序列化 role TOML，输入为角色，输出单文件 TOML 文本。"""
    lines = [
        f"id = {_toml_value(role.role_id)}",
        f"nickname = {_toml_value(role.nickname)}",
        f"model = {_toml_value(role.model)}",
        f"role_desc = {_toml_value(role.role_desc)}",
        f"reasoning_effort = {_toml_value(role.reasoning_effort or '')}",
        f"max_turns = {role.max_turns}",
        "",
    ]
    return "\n".join(lines)


def _toml_value(value: int | str) -> str:
    """序列化 TOML 标量，输入为 int/str，输出 TOML 字面量。"""
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _validated_preset(
    role_id: object,
    title: object,
    role: object,
    *,
    model: object = "",
    reasoning_effort: object = None,
    max_turns: object = 3,
    source: Literal["builtin", "runtime", "user"] = "builtin",
    source_path: str = "",
    editable: bool = False,
) -> AgentRolePreset:
    """校验并构造角色，输入为字段值，输出 AgentRolePreset。"""
    normalized = _validate_role_id(role_id)
    normalized_title = _validate_text(title, field="title", max_length=_MAX_TITLE_LENGTH)
    normalized_role = _validate_text(role, field="role", max_length=_MAX_ROLE_LENGTH)
    normalized_model = _validate_optional_text(model, field="model", max_length=_MAX_MODEL_LENGTH)
    normalized_effort = _validate_optional_reasoning_effort(
        reasoning_effort,
    )
    return AgentRolePreset(
        role_id=normalized,
        nickname=normalized_title,
        role_desc=normalized_role,
        model=normalized_model,
        reasoning_effort=normalized_effort,
        max_turns=_validate_positive_int(max_turns, field="max_turns"),
        source=source,
        source_path=source_path,
        editable=editable,
    )


def _validate_role_id(value: object) -> str:
    """校验 role id，输入为原始值，输出为规范 id。"""
    if isinstance(value, bool):
        raise ValueError("invalid role id")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("invalid role id")
        role_id = str(value)
    elif isinstance(value, str):
        role_id = value.strip()
    else:
        raise ValueError("invalid role id")
    if not role_id or len(role_id) > _MAX_ROLE_ID_LENGTH or not _ROLE_ID_RE.fullmatch(role_id):
        raise ValueError("invalid role id")
    return role_id


def _validate_text(value: object, *, field: str, max_length: int) -> str:
    """校验文本字段，输入为原始值、字段名和长度，输出为去空白文本。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long")
    return text


def _validate_optional_text(value: object, *, field: str, max_length: int) -> str:
    """校验可空文本字段，输入为原始值、字段名和长度，输出去空白文本。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{field} is too long")
    return text


def _validate_optional_reasoning_effort(value: object) -> str | None:
    """校验可空 reasoning effort，输入原始值，输出规范字符串或 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reasoning_effort must be a string or null")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_REASONING_EFFORT_LENGTH:
        raise ValueError("reasoning_effort is too long")
    return text


def _validate_positive_int(value: object, *, field: str) -> int:
    """校验正整数，输入为原始值和字段名，输出正整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


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
