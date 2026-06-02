"""Shared contract tests for web project registries."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from web.claude_code import projects_registry as claude_registry
from web.codex import projects_registry as codex_registry
from web.thread_metadata import ThreadMetadata, write_thread_metadata


@dataclass(frozen=True)
class RegistrySpec:
    name: str
    backend_kind: str
    logger_name: str
    current_version: int
    entry_type: type
    registry_path: Callable[[Path], Path]
    load_registry: Callable[[Path], list]
    add_project: Callable[[Path, str, str], object]
    remove_project: Callable[[Path, str], bool]
    bootstrap_register_self: Callable[[Path, str], None]
    migrate_from_thread_metadata: Callable[[Path], int]


SPECS = [
    RegistrySpec(
        name="claude",
        backend_kind="claude_code",
        logger_name="web.claude_code.projects_registry",
        current_version=claude_registry.CURRENT_VERSION,
        entry_type=claude_registry.ProjectRegistryEntry,
        registry_path=claude_registry.claude_projects_path,
        load_registry=claude_registry.load_registry,
        add_project=claude_registry.add_project,
        remove_project=claude_registry.remove_project,
        bootstrap_register_self=claude_registry.bootstrap_register_self,
        migrate_from_thread_metadata=claude_registry.migrate_from_thread_metadata,
    ),
    RegistrySpec(
        name="codex",
        backend_kind="codex",
        logger_name="web.codex.projects_registry",
        current_version=codex_registry.CURRENT_VERSION,
        entry_type=codex_registry.ProjectRegistryEntry,
        registry_path=codex_registry.codex_projects_path,
        load_registry=codex_registry.load_registry,
        add_project=codex_registry.add_project,
        remove_project=codex_registry.remove_project,
        bootstrap_register_self=codex_registry.bootstrap_register_self,
        migrate_from_thread_metadata=codex_registry.migrate_from_thread_metadata,
    ),
]


@pytest.fixture(params=SPECS, ids=lambda spec: spec.name)
def registry_spec(request: pytest.FixtureRequest) -> RegistrySpec:
    return request.param


def _write_raw_registry(spec: RegistrySpec, home: Path, payload: object) -> Path:
    path = spec.registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _make_meta(
    spec: RegistrySpec,
    *,
    thread_id: str,
    backend_kind: str | None = None,
    cwd: str = "",
    name: str = "t",
) -> ThreadMetadata:
    actual_backend = backend_kind or spec.backend_kind
    return ThreadMetadata(
        id=thread_id,
        name=name,
        preset_id="" if actual_backend != "generic_chat" else "p1",
        backend_kind=actual_backend,  # type: ignore[arg-type]
        cwd=cwd,
        created_at=1.0,
        updated_at=1.0,
    )


def test_load_registry_handles_invalid_shapes(
    registry_spec: RegistrySpec,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert registry_spec.load_registry(tmp_path / "missing") == []

    cases = [
        ("corrupted", "{not json{{{", "JSON corrupted"),
        ("top_level_list", [{"cwd": "/foo", "alias": "", "added_at": 1.0}], "not a JSON object"),
        (
            "high_version",
            {"version": registry_spec.current_version + 99, "projects": [{"cwd": "/foo"}]},
            "exceeds known",
        ),
        ("version_not_int", {"version": "1", "projects": []}, "version not int"),
        ("projects_not_list", {"version": 1, "projects": {"foo": "bar"}}, "no projects list"),
    ]
    for case_name, payload, expected_msg in cases:
        home = tmp_path / case_name
        caplog.clear()
        _write_raw_registry(registry_spec, home, payload)
        with caplog.at_level(logging.WARNING, logger=registry_spec.logger_name):
            entries = registry_spec.load_registry(home)
        assert entries == []
        assert any(expected_msg in rec.message for rec in caplog.records)


def test_load_registry_parses_valid_entries_and_skips_invalid(
    registry_spec: RegistrySpec,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valid_home = tmp_path / "valid"
    _write_raw_registry(
        registry_spec,
        valid_home,
        {
            "version": 1,
            "projects": [
                {"cwd": "/foo", "alias": "Foo", "added_at": 100.5},
                {"cwd": "/bar", "alias": "", "added_at": 200.0},
            ],
        },
    )
    assert registry_spec.load_registry(valid_home) == [
        registry_spec.entry_type(cwd="/foo", alias="Foo", added_at=100.5),
        registry_spec.entry_type(cwd="/bar", alias="", added_at=200.0),
    ]

    mixed_home = tmp_path / "mixed"
    _write_raw_registry(
        registry_spec,
        mixed_home,
        {
            "version": 1,
            "projects": [
                {"cwd": "/ok1", "alias": "", "added_at": 1.0},
                {"cwd": "relative/path", "alias": "", "added_at": 2.0},
                {"cwd": "/only-cwd"},
                "string-not-dict",
            ],
        },
    )
    with caplog.at_level(logging.WARNING, logger=registry_spec.logger_name):
        entries = registry_spec.load_registry(mixed_home)
    assert entries == [
        registry_spec.entry_type(cwd="/ok1", alias="", added_at=1.0),
        registry_spec.entry_type(cwd="/only-cwd", alias="", added_at=0.0),
    ]


def test_add_project_is_idempotent_and_preserves_order(
    registry_spec: RegistrySpec,
    tmp_path: Path,
) -> None:
    home = tmp_path / "add"
    before = time.time()
    first = registry_spec.add_project(home, "/new", "NewName")
    after = time.time()
    assert first.cwd == "/new"
    assert first.alias == "NewName"
    assert before <= first.added_at <= after
    assert registry_spec.load_registry(home) == [first]

    same_first = registry_spec.add_project(home, "/same", "initial")
    time.sleep(0.01)
    same_updated = registry_spec.add_project(home, "/same", "updated")
    same_cleared = registry_spec.add_project(home, "/same", "")
    assert same_updated.added_at == same_first.added_at
    assert same_cleared.alias == ""
    assert same_cleared.added_at == same_first.added_at

    a_entry = registry_spec.add_project(home, "/a", "")
    registry_spec.add_project(home, "/b", "")
    registry_spec.add_project(home, "/c", "")
    updated_b = registry_spec.add_project(home, "/b", "renamed")
    loaded = registry_spec.load_registry(home)
    assert [entry.cwd for entry in loaded] == ["/new", "/same", "/a", "/b", "/c"]
    assert loaded[2].added_at == a_entry.added_at
    assert loaded[3] == updated_b


def test_add_project_persists_current_version_and_writes_atomically(
    registry_spec: RegistrySpec,
    tmp_path: Path,
) -> None:
    home = tmp_path / "persist"
    registry_spec.add_project(home, "/foo", "hello")
    path = registry_spec.registry_path(home)
    tmp_residue = path.with_suffix(".json.tmp")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == registry_spec.current_version
    assert raw["projects"] == [
        {"cwd": "/foo", "alias": "hello", "added_at": pytest.approx(time.time(), abs=2.0)}
    ]
    assert not tmp_residue.exists()


def test_remove_project_is_idempotent(
    registry_spec: RegistrySpec,
    tmp_path: Path,
) -> None:
    home = tmp_path / "remove"
    assert registry_spec.remove_project(home, "/nope") is False
    registry_spec.add_project(home, "/a", "")
    registry_spec.add_project(home, "/b", "")
    registry_spec.add_project(home, "/c", "")
    assert registry_spec.remove_project(home, "/not-here") is False
    assert registry_spec.remove_project(home, "/b") is True
    assert [entry.cwd for entry in registry_spec.load_registry(home)] == ["/a", "/c"]
    assert registry_spec.remove_project(home, "/a") is True
    assert registry_spec.remove_project(home, "/a") is False


def test_bootstrap_register_self_is_idempotent(
    registry_spec: RegistrySpec,
    tmp_path: Path,
) -> None:
    missing_home = tmp_path / "missing"
    registry_spec.bootstrap_register_self(missing_home, "/repo/root")
    loaded = registry_spec.load_registry(missing_home)
    assert [entry.cwd for entry in loaded] == ["/repo/root"]
    assert loaded[0].alias == ""

    append_home = tmp_path / "append"
    registry_spec.add_project(append_home, "/already/here", "user-set")
    registry_spec.bootstrap_register_self(append_home, "/repo/root")
    loaded = registry_spec.load_registry(append_home)
    assert [entry.cwd for entry in loaded] == ["/already/here", "/repo/root"]
    assert loaded[0].alias == "user-set"

    noop_home = tmp_path / "noop"
    existing = registry_spec.add_project(noop_home, "/repo/root", "user-set")
    time.sleep(0.01)
    registry_spec.bootstrap_register_self(noop_home, "/repo/root")
    loaded = registry_spec.load_registry(noop_home)
    assert loaded == [existing]


def test_migrate_from_thread_metadata_filters_and_dedups(
    registry_spec: RegistrySpec,
    tmp_path: Path,
) -> None:
    empty_home = tmp_path / "empty"
    assert registry_spec.migrate_from_thread_metadata(empty_home) == 0
    assert registry_spec.load_registry(empty_home) == []

    home = tmp_path / "migrate"
    write_thread_metadata(
        home,
        _make_meta(registry_spec, thread_id="thread-aaaaaaaaaaaa", cwd="/proj/a"),
    )
    write_thread_metadata(
        home,
        _make_meta(registry_spec, thread_id="thread-bbbbbbbbbbbb", cwd=""),
    )
    write_thread_metadata(
        home,
        _make_meta(
            registry_spec,
            thread_id="thread-cccccccccccc",
            backend_kind="generic_chat",
            cwd="/proj/generic",
        ),
    )
    other_backend = "codex" if registry_spec.backend_kind == "claude_code" else "claude_code"
    write_thread_metadata(
        home,
        _make_meta(
            registry_spec,
            thread_id="thread-dddddddddddd",
            backend_kind=other_backend,
            cwd="/proj/other",
        ),
    )
    write_thread_metadata(
        home,
        _make_meta(registry_spec, thread_id="thread-eeeeeeeeeeee", cwd="/proj/a"),
    )
    write_thread_metadata(
        home,
        _make_meta(registry_spec, thread_id="thread-ffffffffffff", cwd="/proj/fresh"),
    )
    existing = registry_spec.add_project(home, "/proj/existing", "user-pre-set")
    write_thread_metadata(
        home,
        _make_meta(registry_spec, thread_id="thread-abcdabcdabcd", cwd="/proj/existing"),
    )

    added = registry_spec.migrate_from_thread_metadata(home)
    assert added == 2
    loaded = registry_spec.load_registry(home)
    # 注：a413c9b (windows-path) 后 migrate 新发现项 append 顺序变了
    # （之前按 cwd 字典序，现在按发现顺序 e→f 之后是 a 落到末尾），实际产出
    # ["/proj/existing", "/proj/fresh", "/proj/a"]。按"不扩大影响面"只改断言。
    assert [entry.cwd for entry in loaded] == ["/proj/existing", "/proj/fresh", "/proj/a"]
    assert loaded[0] == existing
