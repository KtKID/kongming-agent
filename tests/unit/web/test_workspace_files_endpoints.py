from __future__ import annotations

from pathlib import Path

from tests.unit.web.test_workspace_context_endpoint import FakeTM, _login_client
from web.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.thread_metadata import ThreadMetadata

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_tm(workspace_root: Path) -> FakeTM:
    return FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000003",
                name="Claude",
                preset_id="",
                backend_kind="claude_code",
                claude_thread_id="sdk-1",
                cwd=str(workspace_root),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )


def test_workspace_tree_lists_children(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "README.md").write_text("hello", encoding="utf-8")
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        resp = client.get(
            "/api/threads/thread-000000000003/workspace-tree",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == ""
        assert body["entries"][0]["name"] == "src"
        assert body["entries"][0]["kind"] == "dir"
        assert body["entries"][1]["name"] == "README.md"
        assert body["entries"][1]["kind"] == "file"
    finally:
        client.__exit__(None, None, None)


def test_workspace_file_read_and_write(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("old", encoding="utf-8")
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        read_resp = client.get(
            "/api/threads/thread-000000000003/workspace-file",
            params={"path": "notes.txt"},
        )
        assert read_resp.status_code == 200
        assert read_resp.json()["content"] == "old"

        write_resp = client.put(
            "/api/threads/thread-000000000003/workspace-file",
            json={"path": "notes.txt", "content": "new content"},
            headers=CSRF_HEADERS,
        )
        assert write_resp.status_code == 200
        assert write_resp.json()["content"] == "new content"
        assert target.read_text(encoding="utf-8") == "new content"
    finally:
        client.__exit__(None, None, None)


def test_workspace_file_rejects_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        resp = client.get(
            "/api/threads/thread-000000000003/workspace-file",
            params={"path": "../secret.txt"},
        )
        assert resp.status_code == 400
        assert "escapes root" in resp.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_workspace_file_marks_nul_binary_as_non_text(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "blob.bin").write_bytes(b"\x00abc")
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        resp = client.get(
            "/api/threads/thread-000000000003/workspace-file",
            params={"path": "blob.bin"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "blob.bin"
        assert body["name"] == "blob.bin"
        assert body["content"] == ""
        assert body["size_bytes"] == 4
        assert body["is_text"] is False
        assert body["too_large"] is False
        assert body["encoding"] == "utf-8"
    finally:
        client.__exit__(None, None, None)


def test_workspace_tree_falls_back_to_server_cwd_when_thread_cwd_empty(
    tmp_path: Path,
) -> None:
    """thread.cwd 空时 workspace-tree 端点 fallback 到 server 启动目录。

    task #2：``require_workspace_root(..., fallback_to=app.state.workspace_root)``
    让纯聊天 thread 也能正常列文件，不再 400 ``WorkspaceError``。
    """
    workspace = tmp_path / "server_proj"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")
    tm = FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000004",
                name="Generic",
                preset_id="preset-a",
                backend_kind="generic_chat",
                claude_thread_id="",
                cwd="",  # 故意留空，触发 fallback
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )
    client = _login_client(tmp_path, tm)
    try:
        client.app.state.workspace_root = workspace  # type: ignore[attr-defined]
        resp = client.get("/api/threads/thread-000000000004/workspace-tree")
        assert resp.status_code == 200
        body = resp.json()
        names = [entry["name"] for entry in body["entries"]]
        assert "hello.txt" in names
    finally:
        client.__exit__(None, None, None)


def test_workspace_file_marks_invalid_utf8_as_non_text(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "broken.txt").write_bytes(b"\xff\xfehello")
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        resp = client.get(
            "/api/threads/thread-000000000003/workspace-file",
            params={"path": "broken.txt"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "broken.txt"
        assert body["name"] == "broken.txt"
        assert body["content"] == ""
        assert body["size_bytes"] == 7
        assert body["is_text"] is False
        assert body["too_large"] is False
        assert body["encoding"] == "utf-8"
    finally:
        client.__exit__(None, None, None)
