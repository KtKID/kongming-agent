"""司天 generic_chat source 的配置、采集和归并合同测试。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sitian.config import SiTianConfig, SiTianScannerConfig, SiTianSourceConfig, SiTianSourceKind
from sitian.models import SiTianObservation
from sitian.scanners import SiTianScanSource
from sitian.service import SiTianRunOnce
from sitian.store import SiTianRecordsStore
from sitian.suggestions import SiTianMaterializeState

_OBSERVED_AT = "2026-08-02T10:00:00Z"
_OBSERVED_TS = datetime(2026, 8, 2, 10, 0, tzinfo=UTC).timestamp()


def _write_metadata(
    home: Path,
    *,
    thread_id: str,
    name: str,
    backend_kind: str = "generic_chat",
    thread_kind: str = "chat",
    message_count: int = 2,
    cwd: str = "",
) -> Path:
    """写入最小 metadata.json，返回其路径。"""

    path = home / "web" / "threads" / thread_id / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": thread_id,
                "name": name,
                "preset_id": "test",
                "backend_kind": backend_kind,
                "thread_kind": thread_kind,
                "source_kind": "",
                "source_id": "",
                "claude_thread_id": "",
                "codex_thread_id": "",
                "cwd": cwd,
                "created_at": _OBSERVED_TS - 60,
                "updated_at": _OBSERVED_TS - 30,
                "message_count": message_count,
                "is_pinned": False,
                "is_archived": False,
                "forked_from_id": None,
                "forked_from_history_index": None,
                "schema_version": 13,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_session(
    home: Path,
    *,
    thread_id: str,
    cwd: str,
    entries: list[dict[str, Any]],
    mtime: float = _OBSERVED_TS - 30,
) -> tuple[Path, Path]:
    """写入最小 FileSession manifest 和 JSONL，返回二者路径。"""

    session_dir = home / "sessions" / thread_id
    session_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = session_dir / f"{thread_id}.jsonl"
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": thread_id,
                "cwd": cwd,
                "format": jsonl_path.name,
            }
        ),
        encoding="utf-8",
    )
    jsonl_path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    os.utime(jsonl_path, (mtime, mtime))
    return manifest_path, jsonl_path


def _message(role: str, content: str | None) -> dict[str, Any]:
    """构造 FileSession message JSONL 记录。"""

    return {
        "record_type": "message",
        "message": {"role": role, "content": content},
    }


def _scan(
    home: Path, *, scanner_config: SiTianScannerConfig | None = None
) -> tuple[SiTianObservation, ...]:
    """执行 generic_chat scanner 并返回 observations。"""

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="generic-chat",
                kind=SiTianSourceKind.GENERIC_CHAT,
                path=str(home),
                top_n=10,
            ),
            observed_at=_OBSERVED_AT,
            scanner_config=scanner_config,
        )
    )
    return batch.observations


@pytest.mark.unit
def test_source_kind_serializes_generic_chat_and_rejects_unknown() -> None:
    """五个 source kind 保持 YAML/JSON 字符串并拒绝未知值。"""

    source = SiTianSourceConfig(
        id="generic-chat",
        kind=SiTianSourceKind.GENERIC_CHAT,
        path="/tmp/kongming",
    )

    assert source.model_dump(mode="json")["kind"] == "generic_chat"
    assert source.kind == "generic_chat"
    with pytest.raises(ValidationError):
        SiTianSourceConfig(id="unknown", kind="bad_kind", path="/tmp/kongming")


@pytest.mark.unit
def test_generic_chat_scanner_uses_contract_truth_sources(tmp_path: Path) -> None:
    """标题来自 metadata，cwd 来自 manifest，正文来自 JSONL。"""

    home = tmp_path / ".kongming"
    thread_id = "thread-0123456789ab"
    metadata_path = _write_metadata(
        home,
        thread_id=thread_id,
        name="Title From Metadata",
        cwd="/metadata-wrong",
    )
    manifest_path, jsonl_path = _write_session(
        home,
        thread_id=thread_id,
        cwd="/manifest-right",
        entries=[
            _message("user", "Body From Session"),
            _message("assistant", "Assistant From Session"),
        ],
    )

    observations = _scan(home)
    project = next(obs for obs in observations if obs.entity_type == "project")
    thread = next(obs for obs in observations if obs.entity_type == "thread")

    assert project.entity_key == "/manifest-right"
    assert project.payload["cwd"] == "/manifest-right"
    assert project.payload["recentSessions"][0]["recentUserMessages"] == ["Body From Session"]
    assert thread.entity_key == thread_id
    assert thread.payload["title"] == "Title From Metadata"
    assert thread.payload["cwd"] == "/manifest-right"
    assert thread.payload["recentUserMessages"] == ["Body From Session"]
    assert thread.payload["recentAssistantMessages"] == ["Assistant From Session"]
    assert thread.evidence_refs == (str(metadata_path), str(manifest_path), str(jsonl_path))


@pytest.mark.unit
def test_generic_chat_scanner_filters_backend_kind_thread_kind_and_window(tmp_path: Path) -> None:
    """仅保留时间窗内的 generic_chat/chat 会话。"""

    home = tmp_path / ".kongming"
    cases = [
        ("thread-111111111111", "generic_chat", "chat", _OBSERVED_TS - 60, "include"),
        ("thread-222222222222", "generic_chat", "scheduled_task", _OBSERVED_TS - 60, "scheduled"),
        ("thread-333333333333", "claude_code", "chat", _OBSERVED_TS - 60, "claude"),
        ("thread-444444444444", "codex", "chat", _OBSERVED_TS - 60, "codex"),
        ("thread-555555555555", "generic_chat", "chat", _OBSERVED_TS - 3 * 86400, "stale"),
    ]
    for thread_id, backend_kind, thread_kind, mtime, body in cases:
        _write_metadata(
            home,
            thread_id=thread_id,
            name=body,
            backend_kind=backend_kind,
            thread_kind=thread_kind,
        )
        _write_session(
            home,
            thread_id=thread_id,
            cwd=f"/project/{body}",
            entries=[_message("user", body)],
            mtime=mtime,
        )

    observations = _scan(home, scanner_config=SiTianScannerConfig(recent_session_window_days=2))

    threads = [obs for obs in observations if obs.entity_type == "thread"]
    assert [obs.entity_key for obs in threads] == ["thread-111111111111"]
    assert threads[0].payload["recentUserMessages"] == ["include"]


@pytest.mark.unit
def test_generic_chat_source_missing_home_fails(tmp_path: Path) -> None:
    """配置根目录缺失时扫描失败，交由 RunOnce 记录 source error。"""

    with pytest.raises(FileNotFoundError, match="Kongming home"):
        _scan(tmp_path / "missing")


@pytest.mark.unit
def test_generic_chat_skips_corrupted_threads_and_records_missing_root_failure(
    tmp_path: Path,
) -> None:
    """局部损坏不阻断健康 thread，缺失根目录写入 source runtime error。"""

    home = tmp_path / ".kongming"
    valid_id = "thread-666666666666"
    _write_metadata(home, thread_id=valid_id, name="healthy")
    _write_session(
        home,
        thread_id=valid_id,
        cwd="/project/healthy",
        entries=[_message("user", "healthy body")],
    )
    corrupt_metadata = home / "web" / "threads" / "thread-777777777777" / "metadata.json"
    corrupt_metadata.parent.mkdir(parents=True, exist_ok=True)
    corrupt_metadata.write_text("{corrupt", encoding="utf-8")
    _write_metadata(home, thread_id="thread-888888888888", name="missing-session")

    observations = _scan(home)
    assert [obs.entity_key for obs in observations if obs.entity_type == "thread"] == [valid_id]

    missing_source = SiTianSourceConfig(
        id="missing-generic-chat",
        kind=SiTianSourceKind.GENERIC_CHAT,
        path=str(tmp_path / "missing"),
    )
    store = SiTianRecordsStore(tmp_path / "records")
    result = asyncio.run(
        SiTianRunOnce(
            SiTianConfig(sources=[missing_source]),
            store=store,
            now=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
        )
    )

    assert result.failed_sources == {
        "missing-generic-chat": f"Kongming home does not exist: {tmp_path / 'missing'}"
    }
    runtime_states = asyncio.run(store.load_runtime_states())
    assert runtime_states[0].status == "error"
    assert runtime_states[0].last_error == result.failed_sources["missing-generic-chat"]


@pytest.mark.unit
def test_generic_chat_materializes_one_work_item_per_cwd() -> None:
    """同一 source 的多个项目在建议层独立归并。"""

    source = SiTianSourceConfig(
        id="generic-chat",
        kind=SiTianSourceKind.GENERIC_CHAT,
        path="/tmp/kongming",
    )
    config = SiTianConfig(sources=[source])
    observations = (
        _observation("project", "/project/a", {"cwd": "/project/a", "displayName": "a"}, "old"),
        _observation("project", "/project/a", {"cwd": "/project/a", "displayName": "a-new"}, "new"),
        _observation("thread", "thread-a", {"cwd": "/project/a", "threadId": "thread-a"}, "new"),
        _observation("project", "/project/b", {"cwd": "/project/b", "displayName": "b"}, "new"),
        _observation("thread", "thread-b", {"cwd": "/project/b", "threadId": "thread-b"}, "new"),
    )

    _, work_items, _, _ = SiTianMaterializeState(
        config,
        runtime_states=(),
        observations=observations,
        updated_at="2026-08-02T10:01:00Z",
    )

    by_path = {item.project_paths[0]: item for item in work_items}
    assert set(by_path) == {"/project/a", "/project/b"}
    assert by_path["/project/a"].title == "a-new"
    assert by_path["/project/a"].thread_ids == ("thread-a",)
    assert by_path["/project/b"].thread_ids == ("thread-b",)


def _observation(
    entity_type: str,
    entity_key: str,
    payload: dict[str, Any],
    observed_suffix: str,
) -> SiTianObservation:
    """构造 generic_chat observation，供归并真源测试使用。"""

    return SiTianObservation(
        id=f"obs-{entity_type}-{entity_key}-{observed_suffix}",
        source_id="generic-chat",
        source_kind="generic_chat",
        observed_at=f"2026-08-02T10:00:0{0 if observed_suffix == 'old' else 1}Z",
        entity_type=entity_type,
        entity_key=entity_key,
        payload=payload,
        evidence_refs=(),
    )
