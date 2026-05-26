from __future__ import annotations

import json

from observability.network_log import log_network_event, log_network_exception


def test_log_network_event_writes_jsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = log_network_event(
        "web.test",
        "send_failed",
        level="WARNING",
        message="socket send failed",
        thread_id="thread-abc",
    )

    assert path == tmp_path / "log" / "network" / "network-events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["component"] == "web.test"
    assert rows[0]["action"] == "send_failed"
    assert rows[0]["thread_id"] == "thread-abc"


def test_log_network_exception_writes_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        path = log_network_exception("web.test", "close_failed", exc)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["error_type"] == "RuntimeError"
    assert rows[0]["error"] == "boom"
    assert "RuntimeError: boom" in rows[0]["traceback"]
