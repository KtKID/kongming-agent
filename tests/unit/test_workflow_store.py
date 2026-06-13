"""WorkflowStore JSON 持久化单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from hosts.web.workflow.models import (
    EdgeTrigger,
    EventSource,
    RunStatus,
    StageStatus,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowEvent,
    WorkflowNode,
    WorkflowRun,
    new_id,
)
from hosts.web.workflow.store import (
    _DEFINITIONS_FILENAME,
    _EVENTS_FILENAME,
    _RUNS_FILENAME,
    SCHEMA_VERSION,
    WorkflowStore,
)

# ── helpers ────────────────────────────────────────────────


def _make_definition(
    *,
    wf_id: str | None = None,
    name: str = "test-wf",
    n_nodes: int = 2,
) -> WorkflowDefinition:
    wf_id = wf_id or new_id()
    nodes = [WorkflowNode(id=f"n{i}", template_id=f"tpl{i}", position=i) for i in range(n_nodes)]
    edges = (
        [
            WorkflowEdge(
                id=new_id(),
                from_node_id=nodes[0].id,
                to_node_id=nodes[1].id,
                trigger=EdgeTrigger.ON_COMPLETED,
            )
        ]
        if n_nodes >= 2
        else []
    )
    return WorkflowDefinition(
        id=wf_id,
        name=name,
        description="desc",
        nodes=nodes,
        edges=edges,
    )


def _make_run(
    *,
    run_id: str | None = None,
    workflow_id: str = "wf-1",
    task_id: str = "task-1",
) -> WorkflowRun:
    return WorkflowRun(
        id=run_id or new_id(),
        workflow_id=workflow_id,
        task_id=task_id,
        spec_id="spec-1",
        node_states={"n0": StageStatus.DONE, "n1": StageStatus.NOT_STARTED},
        status=RunStatus.ACTIVE,
    )


def _make_event(
    *,
    run_id: str = "run-1",
    node_id: str = "n0",
    old: StageStatus = StageStatus.NOT_STARTED,
    new: StageStatus = StageStatus.DONE,
    source: EventSource = EventSource.SCANNER,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=new_id(),
        run_id=run_id,
        node_id=node_id,
        old_status=old,
        new_status=new,
        source=source,
    )


# ── 1. bootstrap ──────────────────────────────────────────


class TestBootstrap:
    def test_creates_dir_and_files(self, tmp_path: Path) -> None:
        """构造时目录不存在 → 自动创建 + 3 个空文件。"""
        home = tmp_path / "workflow" / "nested"
        assert not home.exists()

        WorkflowStore(home)

        assert home.is_dir()
        assert (home / _DEFINITIONS_FILENAME).exists()
        assert (home / _RUNS_FILENAME).exists()
        assert (home / _EVENTS_FILENAME).exists()

    def test_bootstrap_json_files_are_valid(self, tmp_path: Path) -> None:
        """bootstrap 写出的 JSON 文件是合法 JSON，且 items 为空列表。"""
        store = WorkflowStore(tmp_path / "wf")
        for fname in (_DEFINITIONS_FILENAME, _RUNS_FILENAME):
            path = store._home / fname
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["items"] == []
            assert payload["schema_version"] == SCHEMA_VERSION

    def test_bootstrap_is_idempotent(self, tmp_path: Path) -> None:
        """对已有目录再构造一次不应报错。"""
        home = tmp_path / "wf"
        WorkflowStore(home)
        store2 = WorkflowStore(home)  # 不抛异常
        assert store2.load_definitions() == []


# ── 2. definitions CRUD ───────────────────────────────────


class TestDefinitionsCRUD:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        d = _make_definition(n_nodes=3)
        store.save_definitions([d])

        loaded = store.load_definitions()
        assert len(loaded) == 1
        got = loaded[0]
        assert got.id == d.id
        assert got.name == d.name
        assert got.description == d.description
        assert len(got.nodes) == 3
        assert len(got.edges) == 1
        assert got.edges[0].trigger == EdgeTrigger.ON_COMPLETED

    def test_save_multiple_definitions(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        defs = [_make_definition(name=f"wf-{i}") for i in range(3)]
        store.save_definitions(defs)

        loaded = store.load_definitions()
        assert len(loaded) == 3
        assert {d.name for d in loaded} == {"wf-0", "wf-1", "wf-2"}

    def test_save_overwrites_previous(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.save_definitions([_make_definition(name="old")])
        store.save_definitions([_make_definition(name="new")])

        loaded = store.load_definitions()
        assert len(loaded) == 1
        assert loaded[0].name == "new"

    def test_empty_definitions_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.save_definitions([])
        assert store.load_definitions() == []

    def test_node_fields_preserved(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        d = _make_definition(n_nodes=1)
        store.save_definitions([d])

        got = store.load_definitions()[0]
        assert got.nodes[0].id == d.nodes[0].id
        assert got.nodes[0].template_id == d.nodes[0].template_id
        assert got.nodes[0].position == d.nodes[0].position


# ── 3. runs CRUD ──────────────────────────────────────────


class TestRunsCRUD:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        r = _make_run()
        store.save_runs([r])

        loaded = store.load_runs()
        assert len(loaded) == 1
        got = loaded[0]
        assert got.id == r.id
        assert got.workflow_id == r.workflow_id
        assert got.task_id == r.task_id
        assert got.spec_id == r.spec_id
        assert got.status == RunStatus.ACTIVE
        assert got.node_states == {"n0": StageStatus.DONE, "n1": StageStatus.NOT_STARTED}

    def test_save_multiple_runs(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        runs = [_make_run(run_id=f"r{i}") for i in range(4)]
        store.save_runs(runs)

        loaded = store.load_runs()
        assert len(loaded) == 4

    def test_completed_run_status(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        r = _make_run()
        r.status = RunStatus.COMPLETED
        store.save_runs([r])

        got = store.load_runs()[0]
        assert got.status == RunStatus.COMPLETED


# ── 4. events append + load ───────────────────────────────


class TestEvents:
    def test_append_and_load_all(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        e1 = _make_event(run_id="r1", node_id="n0")
        e2 = _make_event(run_id="r1", node_id="n1")
        e3 = _make_event(run_id="r2", node_id="n0")

        store.append_event(e1)
        store.append_event(e2)
        store.append_event(e3)

        all_events = store.load_events()
        assert len(all_events) == 3

    def test_load_filtered_by_run_id(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.append_event(_make_event(run_id="r1", node_id="n0"))
        store.append_event(_make_event(run_id="r2", node_id="n0"))
        store.append_event(_make_event(run_id="r1", node_id="n1"))

        r1_events = store.load_events(run_id="r1")
        assert len(r1_events) == 2
        assert all(e.run_id == "r1" for e in r1_events)

        r2_events = store.load_events(run_id="r2")
        assert len(r2_events) == 1

    def test_load_nonexistent_run_id_returns_empty(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.append_event(_make_event(run_id="r1"))
        assert store.load_events(run_id="no-such") == []

    def test_load_empty_events(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        assert store.load_events() == []

    def test_event_fields_preserved(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        e = _make_event(
            run_id="r1",
            node_id="n5",
            old=StageStatus.NOT_STARTED,
            new=StageStatus.DONE,
            source=EventSource.MANUAL,
        )
        store.append_event(e)

        got = store.load_events()[0]
        assert got.event_id == e.event_id
        assert got.run_id == "r1"
        assert got.node_id == "n5"
        assert got.old_status == StageStatus.NOT_STARTED
        assert got.new_status == StageStatus.DONE
        assert got.source == EventSource.MANUAL


# ── 5. atomic write ──────────────────────────────────────


class TestAtomicWrite:
    def test_save_produces_valid_json(self, tmp_path: Path) -> None:
        """save 完成后文件内容是合法 JSON。"""
        store = WorkflowStore(tmp_path / "wf")
        store.save_definitions([_make_definition()])
        store.save_runs([_make_run()])

        for fname in (_DEFINITIONS_FILENAME, _RUNS_FILENAME):
            path = store._home / fname
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)  # 不抛 JSONDecodeError
            assert isinstance(payload, dict)

    def test_no_temp_file_left_after_save(self, tmp_path: Path) -> None:
        """原子写入完成后不应残留 .tmp 文件。"""
        store = WorkflowStore(tmp_path / "wf")
        store.save_definitions([_make_definition()])

        tmp_files = list(store._home.glob(".wf_*.tmp"))
        assert tmp_files == []


# ── 6. schema_version ────────────────────────────────────


class TestSchemaVersion:
    def test_definitions_contain_schema_version(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.save_definitions([_make_definition()])

        raw = json.loads((store._home / _DEFINITIONS_FILENAME).read_text(encoding="utf-8"))
        assert raw["schema_version"] == 1

    def test_runs_contain_schema_version(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        store.save_runs([_make_run()])

        raw = json.loads((store._home / _RUNS_FILENAME).read_text(encoding="utf-8"))
        assert raw["schema_version"] == 1

    def test_bootstrap_files_contain_schema_version(self, tmp_path: Path) -> None:
        WorkflowStore(tmp_path / "wf")
        for fname in (_DEFINITIONS_FILENAME, _RUNS_FILENAME):
            raw = json.loads((tmp_path / "wf" / fname).read_text(encoding="utf-8"))
            assert raw["schema_version"] == 1


# ── 7. Enum 序列化 / 反序列化 round-trip ─────────────────


class TestEnumRoundTrip:
    def test_edge_trigger_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        d = _make_definition()
        d.edges[0].trigger = EdgeTrigger.ON_FAILED
        store.save_definitions([d])

        got = store.load_definitions()[0]
        assert got.edges[0].trigger == EdgeTrigger.ON_FAILED
        assert isinstance(got.edges[0].trigger, EdgeTrigger)

    def test_stage_status_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        r = _make_run()
        r.node_states = {"x": StageStatus.DONE, "y": StageStatus.NOT_STARTED}
        store.save_runs([r])

        got = store.load_runs()[0]
        assert got.node_states["x"] == StageStatus.DONE
        assert got.node_states["y"] == StageStatus.NOT_STARTED
        assert isinstance(got.node_states["x"], StageStatus)

    def test_run_status_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        for status in RunStatus:
            r = _make_run(run_id=f"r-{status.value}")
            r.status = status
            store.save_runs([r])
            got = store.load_runs()[0]
            assert got.status == status
            assert isinstance(got.status, RunStatus)

    def test_event_source_roundtrip(self, tmp_path: Path) -> None:
        store = WorkflowStore(tmp_path / "wf")
        for src in EventSource:
            e = _make_event(source=src)
            store.append_event(e)

        events = store.load_events()
        sources = {e.source for e in events}
        assert sources == set(EventSource)
        assert all(isinstance(e.source, EventSource) for e in events)

    def test_enum_stored_as_string_value_in_json(self, tmp_path: Path) -> None:
        """JSON 里存的是字符串值（如 "done"），不是类名。"""
        store = WorkflowStore(tmp_path / "wf")
        r = _make_run()
        store.save_runs([r])

        raw = json.loads((store._home / _RUNS_FILENAME).read_text(encoding="utf-8"))
        item = raw["items"][0]
        assert item["status"] == "active"
        assert item["node_states"]["n0"] == "done"
        assert item["node_states"]["n1"] == "not_started"
