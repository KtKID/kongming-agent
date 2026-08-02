"""Agent role manager unit tests.

These tests pin the role registry contract used by tools and workflows: builtin
agent.toml roles, runtime-created TOML roles, user TOML roles, legacy JSON
migration, participant resolution, and workflow snapshots.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from application.agent_roles import AgentRoleManager, AgentRolePreset

BUILTIN_NICKNAME = "Builtin architect"
BUILTIN_ROLE_DESC = "Review architecture boundaries"
RISK_NICKNAME = "Risk skeptic"
RISK_ROLE_DESC = "Find hidden risks"


def _manager(tmp_path: Path) -> AgentRoleManager:
    """Build a manager with one builtin role for focused unit tests."""
    return AgentRoleManager(
        role_dir=tmp_path / "roles",
        builtin_roles=(
            AgentRolePreset(
                role_id="builtin_architect",
                nickname=BUILTIN_NICKNAME,
                role_desc=BUILTIN_ROLE_DESC,
            ),
        ),
    )


def _display_path(path: Path) -> str:
    """Mirror the manager display path helper used in result assertions."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _expected_role(
    *,
    role_id: int | str,
    nickname: str,
    role_desc: str,
    model: str = "",
    reasoning_effort: str | None = None,
    max_turns: int = 3,
    source: str,
    path: Path | str,
    editable: bool,
) -> dict[str, object]:
    """Build an expected summary matching AgentRolePreset.summary()."""
    return {
        "id": role_id,
        "nickname": nickname,
        "model": model,
        "role_desc": role_desc,
        "reasoning_effort": reasoning_effort,
        "max_turns": max_turns,
        "source": source,
        "path": _display_path(path) if isinstance(path, Path) else path,
        "editable": editable,
    }


def test_agent_role_manager_lists_empty_roles_with_create_hint(tmp_path: Path) -> None:
    """An empty registry returns an empty list plus create guidance."""
    manager = AgentRoleManager(role_dir=tmp_path / "roles")

    result = manager.list_role_summaries(session_id="s1")

    assert result.roles == []
    assert result.current_roundtable_agents == []
    assert result.empty_message


def test_create_role_saves_runtime_toml_and_returns_roster(tmp_path: Path) -> None:
    """Created roles are persisted as runtime TOML and added to the session roster."""
    manager = _manager(tmp_path)

    result = manager.create_role(
        session_id="s1",
        role_id="risk_skeptic",
        title=RISK_NICKNAME,
        role=RISK_ROLE_DESC,
    )

    role_file = tmp_path / "roles" / "runtime" / "risk_skeptic.toml"
    expected = _expected_role(
        role_id="risk_skeptic",
        nickname=RISK_NICKNAME,
        role_desc=RISK_ROLE_DESC,
        source="runtime",
        path=role_file,
        editable=True,
    )
    assert result.status == "created"
    assert result.role == expected
    assert result.current_roundtable_agents == [expected]
    assert result.path == _display_path(role_file)
    assert not (tmp_path / "roles" / "risk_skeptic.json").exists()
    assert tomllib.loads(role_file.read_text(encoding="utf-8")) == {
        "id": "risk_skeptic",
        "nickname": RISK_NICKNAME,
        "model": "",
        "role_desc": RISK_ROLE_DESC,
        "reasoning_effort": "",
        "max_turns": 3,
    }


def test_create_role_existing_returns_existing_and_dedupes_roster(tmp_path: Path) -> None:
    """Repeated creation reuses the existing role and keeps one roster entry."""
    manager = _manager(tmp_path)

    first = manager.create_role(
        session_id="s1", role_id="risk_skeptic", title=RISK_NICKNAME, role=RISK_ROLE_DESC
    )
    second = manager.create_role(
        session_id="s1", role_id="risk_skeptic", title=RISK_NICKNAME, role=RISK_ROLE_DESC
    )

    assert first.status == "created"
    assert second.status == "existing"
    assert second.current_roundtable_agents == [first.role]


def test_agent_role_manager_roster_is_session_scoped(tmp_path: Path) -> None:
    """Session rosters remain isolated across sessions."""
    manager = _manager(tmp_path)

    manager.create_role(session_id="s1", role_id="role_a", title="A", role="Role A")
    manager.create_role(session_id="s2", role_id="role_b", title="B", role="Role B")

    s1 = manager.list_role_summaries(session_id="s1")
    s2 = manager.list_role_summaries(session_id="s2")
    assert [item["id"] for item in s1.current_roundtable_agents] == ["role_a"]
    assert [item["id"] for item in s2.current_roundtable_agents] == ["role_b"]


def test_agent_role_manager_skips_corrupt_legacy_json_and_migrates_good_file(
    tmp_path: Path,
) -> None:
    """Legacy JSON migration skips corrupt files and writes runtime TOML for valid ones."""
    role_dir = tmp_path / "roles"
    role_dir.mkdir()
    (role_dir / "bad.json").write_text("{", encoding="utf-8")
    (role_dir / "good.json").write_text(
        json.dumps({"id": "good", "title": "Good role", "role": "Readable role"}),
        encoding="utf-8",
    )

    manager = AgentRoleManager(role_dir=role_dir)

    assert [role.role_id for role in manager.list_roles()] == ["good"]
    migrated = role_dir / "runtime" / "good.toml"
    assert migrated.exists()
    assert tomllib.loads(migrated.read_text(encoding="utf-8"))["id"] == "good"


def test_agent_role_manager_loads_agent_toml_and_numeric_ids(tmp_path: Path) -> None:
    """Builtin agent.toml roles keep numeric ids visible while resolving as strings."""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "explorer"
model = "minimax-m3"
role_desc = "Explore current behavior."
reasoning_effort = "medium"
max_turns = 6
""".strip(),
        encoding="utf-8",
    )
    manager = AgentRoleManager(role_dir=tmp_path / "roles", config_path=agent_config)

    listed = manager.list_role_summaries(session_id="s1")
    roles_from_int = manager.resolve_participants([1])
    roles_from_str = manager.resolve_participants(["1"])

    assert listed.roles == [
        _expected_role(
            role_id=1,
            nickname="explorer",
            model="minimax-m3",
            role_desc="Explore current behavior.",
            reasoning_effort="medium",
            max_turns=6,
            source="builtin",
            path=agent_config,
            editable=False,
        )
    ]
    assert roles_from_int == roles_from_str
    assert roles_from_int[0].role_id == "1"


def test_agent_role_manager_accepts_desc_alias_in_agent_toml(tmp_path: Path) -> None:
    """Builtin agent.toml may use desc while manager exposes role_desc."""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "explorer"
model = "minimax-m3"
desc = "Explore with alias."
reasoning_effort = "high"
max_turns = 6
""".strip(),
        encoding="utf-8",
    )

    manager = AgentRoleManager(role_dir=tmp_path / "roles", config_path=agent_config)
    role = manager.get_role(1)

    assert role is not None
    assert role.role_desc == "Explore with alias."
    assert role.summary()["role_desc"] == "Explore with alias."


def test_agent_toml_overrides_injected_builtin_role_with_same_id(tmp_path: Path) -> None:
    """Explicit agent.toml entries win over injected code presets with the same id."""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "toml architect"
model = "toml-model"
role_desc = "Configured from TOML."
reasoning_effort = "high"
max_turns = 7
""".strip(),
        encoding="utf-8",
    )
    injected = AgentRolePreset(
        role_id="1",
        nickname="code architect",
        role_desc="Configured in code.",
        model="code-model",
        reasoning_effort="low",
        max_turns=2,
    )

    manager = AgentRoleManager(
        role_dir=tmp_path / "roles",
        config_path=agent_config,
        builtin_roles=(injected,),
    )
    role = manager.get_role("1")

    assert role is not None
    assert role.nickname == "toml architect"
    assert role.model == "toml-model"
    assert role.reasoning_effort == "high"
    assert role.max_turns == 7
    assert role.source == "builtin"
    assert role.source_path == _display_path(agent_config)


def test_agent_role_manager_aggregates_builtin_runtime_and_user_roles(tmp_path: Path) -> None:
    """Role listing aggregates builtin, runtime, and user TOML sources."""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "builtin"
model = "m1"
role_desc = "Builtin role."
reasoning_effort = "medium"
max_turns = 3
""".strip(),
        encoding="utf-8",
    )
    role_dir = tmp_path / "roles"
    runtime_file = role_dir / "runtime" / "runtime_role.toml"
    user_file = role_dir / "user" / "user_role.toml"
    runtime_file.parent.mkdir(parents=True)
    user_file.parent.mkdir(parents=True)
    runtime_file.write_text(
        """
id = "runtime_role"
nickname = "runtime"
model = ""
role_desc = "Runtime role."
reasoning_effort = "medium"
max_turns = 4
""".strip(),
        encoding="utf-8",
    )
    user_file.write_text(
        """
id = "user_role"
nickname = "user"
model = "m2"
role_desc = "User role."
reasoning_effort = "high"
max_turns = 5
""".strip(),
        encoding="utf-8",
    )

    manager = AgentRoleManager(role_dir=role_dir, config_path=agent_config)
    roles = {str(role["id"]): role for role in manager.list_role_summaries().roles}

    assert roles["1"] == _expected_role(
        role_id=1,
        nickname="builtin",
        model="m1",
        role_desc="Builtin role.",
        reasoning_effort="medium",
        source="builtin",
        path=agent_config,
        editable=False,
    )
    assert roles["runtime_role"] == _expected_role(
        role_id="runtime_role",
        nickname="runtime",
        role_desc="Runtime role.",
        reasoning_effort="medium",
        max_turns=4,
        source="runtime",
        path=runtime_file,
        editable=True,
    )
    assert roles["user_role"] == _expected_role(
        role_id="user_role",
        nickname="user",
        model="m2",
        role_desc="User role.",
        reasoning_effort="high",
        max_turns=5,
        source="user",
        path=user_file,
        editable=True,
    )


def test_builtin_role_ids_are_not_shadowed_by_runtime_or_user_toml(tmp_path: Path) -> None:
    """Builtin role ids keep precedence over runtime and user TOML with the same id."""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "builtin"
model = "builtin-model"
role_desc = "Builtin role."
reasoning_effort = "medium"
max_turns = 3
""".strip(),
        encoding="utf-8",
    )
    role_dir = tmp_path / "roles"
    runtime_file = role_dir / "runtime" / "1.toml"
    user_file = role_dir / "user" / "1.toml"
    runtime_file.parent.mkdir(parents=True)
    user_file.parent.mkdir(parents=True)
    runtime_file.write_text(
        """
id = 1
nickname = "runtime"
model = "runtime-model"
role_desc = "Runtime role."
reasoning_effort = "low"
max_turns = 2
""".strip(),
        encoding="utf-8",
    )
    user_file.write_text(
        """
id = 1
nickname = "user"
model = "user-model"
role_desc = "User role."
reasoning_effort = "high"
max_turns = 9
""".strip(),
        encoding="utf-8",
    )

    manager = AgentRoleManager(role_dir=role_dir, config_path=agent_config)
    role = manager.get_role(1)

    assert role is not None
    assert role.nickname == "builtin"
    assert role.model == "builtin-model"
    assert role.source == "builtin"
    assert role.source_path == _display_path(agent_config)


def test_agent_role_manager_migrates_legacy_json_without_overwriting_toml_or_builtin(
    tmp_path: Path,
) -> None:
    """Legacy JSON migration only adds missing runtime TOML files."""
    agent_config = tmp_path / "agent.toml"
    builtin_text = """
[[agents]]
id = 1
nickname = "builtin"
model = "m1"
role_desc = "Builtin role."
reasoning_effort = "medium"
max_turns = 3
""".strip()
    agent_config.write_text(builtin_text, encoding="utf-8")
    role_dir = tmp_path / "roles"
    role_dir.mkdir()
    (role_dir / "legacy.json").write_text(
        json.dumps({"id": "legacy", "title": "Legacy", "role": "Legacy JSON"}),
        encoding="utf-8",
    )
    (role_dir / "1.json").write_text(
        json.dumps({"id": 1, "title": "Shadow", "role": "Do not shadow builtin"}),
        encoding="utf-8",
    )
    (role_dir / "existing.json").write_text(
        json.dumps({"id": "existing", "title": "Skip", "role": "Do not overwrite"}),
        encoding="utf-8",
    )
    existing_toml = role_dir / "runtime" / "existing.toml"
    existing_toml.parent.mkdir(parents=True)
    existing_text = (
        """
id = "existing"
nickname = "Existing"
model = ""
role_desc = "Preserved"
reasoning_effort = "medium"
max_turns = 3
""".strip()
        + "\n"
    )
    existing_toml.write_text(existing_text, encoding="utf-8")

    manager = AgentRoleManager(role_dir=role_dir, config_path=agent_config)
    roles = {role.role_id: role for role in manager.list_roles()}

    assert agent_config.read_text(encoding="utf-8") == builtin_text
    assert (role_dir / "runtime" / "legacy.toml").exists()
    assert not (role_dir / "runtime" / "1.toml").exists()
    assert existing_toml.read_text(encoding="utf-8") == existing_text
    assert roles["legacy"].source == "runtime"
    assert roles["existing"].nickname == "Existing"
    assert roles["1"].nickname == "builtin"


@pytest.mark.parametrize("role_id", ["", "../bad", "bad/path", "x" * 65])
def test_agent_role_manager_rejects_invalid_role_id(tmp_path: Path, role_id: str) -> None:
    """Invalid role ids raise ValueError before writing files."""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="invalid role id"):
        manager.create_role(session_id="s1", role_id=role_id, title="T", role="R")


@pytest.mark.parametrize(
    ("title", "role", "match"),
    [
        ("", "R", "title is required"),
        ("T", "", "role is required"),
        ("T", "x" * 1201, "role is too long"),
    ],
)
def test_agent_role_manager_rejects_invalid_text(
    tmp_path: Path,
    title: str,
    role: str,
    match: str,
) -> None:
    """Invalid title and role text are rejected by manager validation."""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match=match):
        manager.create_role(session_id="s1", role_id="role_a", title=title, role=role)


def test_resolve_participants_dedupes_and_preserves_order(tmp_path: Path) -> None:
    """Participant resolution deduplicates ids while preserving first-use order."""
    manager = _manager(tmp_path)
    manager.create_role(session_id="s1", role_id="risk_skeptic", title="Risk", role="Find risk")

    roles = manager.resolve_participants(["risk_skeptic", "builtin_architect", "risk_skeptic"])

    assert [role.role_id for role in roles] == ["risk_skeptic", "builtin_architect"]


def test_resolve_participants_rejects_unknown_role_id(tmp_path: Path) -> None:
    """Unknown participant role ids fail clearly."""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="unknown role id: missing"):
        manager.resolve_participants(["missing"])


def test_write_workflow_snapshot(tmp_path: Path) -> None:
    """Workflow snapshots contain manager-produced role summaries."""
    manager = _manager(tmp_path)
    roles = manager.resolve_participants(["builtin_architect"])

    path = manager.write_workflow_snapshot(tmp_path / "workflow", roles)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "source": "agent_role_manager",
        "roles": [
            _expected_role(
                role_id="builtin_architect",
                nickname=BUILTIN_NICKNAME,
                role_desc=BUILTIN_ROLE_DESC,
                source="builtin",
                path="",
                editable=False,
            )
        ],
    }
