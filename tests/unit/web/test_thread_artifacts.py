"""Thread Artifact Viewer 后端只读投影测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.errors import KongmingWebError, kongming_error_handler
from hosts.web.routers.thread_artifacts import router
from hosts.web.thread_artifacts.manager import ThreadArtifactManager, encode_artifact_id
from infrastructure.config.models import Config, ModelSelectionConfig


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        "utf-8",
    )


def _seed_thread(tmp_path: Path) -> tuple[Config, str]:
    cfg = _cfg(tmp_path)
    thread_id = "thread-aaaaaaaaaaaa"
    root = Path(cfg.session.file_store_path) / thread_id
    _write_json(root / "manifest.json", {"session_id": thread_id, "run_count": 2})
    _write_json(root / "system_prompt.json", {"record_type": "system_prompt", "content": "SYS"})
    _write_jsonl(
        root / f"{thread_id}.jsonl",
        [
            {
                "message": {
                    "role": "user",
                    "content": "# 标题\n正文第一行\n正文第二行",
                },
                "created_at": 1,
            },
            {"message": {"role": "assistant", "content": "**结论**"}, "created_at": 2},
        ],
    )
    (root / "trace.jsonl").write_text(
        json.dumps({"event": "turn.start", "ts": "2026-06-19T00:00:00Z"}) + "\n" + "{bad json}\n",
        "utf-8",
    )
    _write_json(root / "task_progress.json", {"items": []})
    (root / "agent-workflows").mkdir(parents=True, exist_ok=True)
    return cfg, thread_id


def test_manager_lists_preferred_thread_artifacts(tmp_path: Path) -> None:
    cfg, thread_id = _seed_thread(tmp_path)
    manager = ThreadArtifactManager(config=cfg)

    listing = manager.list_artifacts(thread_id)

    assert [item.path for item in listing.files[:5]] == [
        "manifest.json",
        "system_prompt.json",
        f"{thread_id}.jsonl",
        "trace.jsonl",
        "task_progress.json",
    ]
    assert listing.files[2].kind == "jsonl"
    assert listing.files[2].record_count == 2


def test_manager_reads_json_jsonl_and_directory(tmp_path: Path) -> None:
    cfg, thread_id = _seed_thread(tmp_path)
    manager = ThreadArtifactManager(config=cfg)

    manifest = manager.read_artifact(
        thread_id=thread_id,
        artifact_id=encode_artifact_id("manifest.json"),
    )
    history = manager.read_artifact(
        thread_id=thread_id,
        artifact_id=encode_artifact_id(f"{thread_id}.jsonl"),
    )
    trace = manager.read_artifact(
        thread_id=thread_id,
        artifact_id=encode_artifact_id("trace.jsonl"),
    )
    workflows = manager.read_artifact(
        thread_id=thread_id,
        artifact_id=encode_artifact_id("agent-workflows"),
    )

    assert manifest.content["session_id"] == thread_id
    assert history.content[0]["message"]["content"].startswith("# 标题")
    assert trace.content[1]["__parse_error__"] is True
    assert trace.diagnostics[0].code == "thread_artifact.read_failed"
    assert workflows.kind == "directory"
    assert workflows.content == []


def test_manager_rejects_path_escape(tmp_path: Path) -> None:
    cfg, thread_id = _seed_thread(tmp_path)
    manager = ThreadArtifactManager(config=cfg)

    try:
        manager.read_artifact(thread_id=thread_id, artifact_id=encode_artifact_id("../secret.json"))
    except ValueError as exc:
        assert "invalid artifact path" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def _client(cfg: Config, thread_id: str) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(KongmingWebError, kongming_error_handler)
    app.state.config = cfg
    app.state.thread_manager = SimpleNamespace(list_threads=lambda: [SimpleNamespace(id=thread_id)])
    app.include_router(router)
    return TestClient(app)


def test_router_exposes_thread_artifacts(tmp_path: Path) -> None:
    cfg, thread_id = _seed_thread(tmp_path)
    client = _client(cfg, thread_id)

    listing = client.get(f"/api/threads/{thread_id}/artifacts")
    content = client.get(
        f"/api/threads/{thread_id}/artifacts/{encode_artifact_id('manifest.json')}"
    )

    assert listing.status_code == 200
    assert listing.json()["files"][0]["path"] == "manifest.json"
    assert content.status_code == 200
    assert content.json()["content"]["run_count"] == 2


def test_router_rejects_invalid_thread_and_artifact(tmp_path: Path) -> None:
    cfg, thread_id = _seed_thread(tmp_path)
    client = _client(cfg, thread_id)

    bad_thread = client.get("/api/threads/not-a-thread/artifacts")
    missing_thread = client.get("/api/threads/thread-bbbbbbbbbbbb/artifacts")
    bad_artifact = client.get(
        f"/api/threads/{thread_id}/artifacts/{encode_artifact_id('../secret.json')}"
    )

    assert bad_thread.status_code == 422
    assert missing_thread.status_code == 404
    assert bad_artifact.status_code == 422
