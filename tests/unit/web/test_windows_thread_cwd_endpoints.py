from pathlib import Path

from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _login_client


def test_create_thread_accepts_windows_absolute_cwd(tmp_path: Path) -> None:
    tm = FakeTM()
    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads",
            json={
                "name": "windows thread",
                "preset_id": "p1",
                "cwd": r"E:\xgt\proj\agent-proj\kongming-agent",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["cwd"] == r"E:\xgt\proj\agent-proj\kongming-agent"
    finally:
        client.__exit__(None, None, None)


def test_import_claude_session_accepts_windows_absolute_cwd(tmp_path: Path) -> None:
    tm = FakeTM()
    bind_calls: list[tuple[str, str, str]] = []

    async def _fake_bind(thread_id: str, claude_thread_id: str, cwd: str):
        bind_calls.append((thread_id, claude_thread_id, cwd))
        old = tm._threads[thread_id]
        new_meta = old.model_copy(
            update={
                "claude_thread_id": claude_thread_id,
                "cwd": cwd,
                "updated_at": old.updated_at + 1.0,
            }
        )
        tm._threads[thread_id] = new_meta
        return new_meta

    tm.bind_claude_thread = _fake_bind  # type: ignore[method-assign]

    client = _login_client(tmp_path, tm)
    try:
        resp = client.post(
            "/api/threads/import-claude-session",
            json={
                "claude_thread_id": "sid-win",
                "cwd": r"E:\xgt\proj\agent-proj\kongming-agent",
                "name": "windows session",
            },
            headers=CSRF_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] is True
        assert body["thread"]["cwd"] == r"E:\xgt\proj\agent-proj\kongming-agent"
        assert bind_calls[0][2] == r"E:\xgt\proj\agent-proj\kongming-agent"
    finally:
        client.__exit__(None, None, None)
