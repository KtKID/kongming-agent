from __future__ import annotations

import json
from pathlib import Path

from web.claude_code.keepalive_log import append_keepalive_log


def test_append_keepalive_log_writes_into_workspace_kongming_logs(tmp_path: Path) -> None:
    path = append_keepalive_log(
        tmp_path,
        event="heartbeat_ping_received",
        thread_id="thread-abcdef123456",
        wire_ts=123,
    )

    assert path == tmp_path / "logs" / "claude-keepalive.jsonl"
    assert path.is_file()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "heartbeat_ping_received"
    assert rows[0]["thread_id"] == "thread-abcdef123456"
    assert rows[0]["wire_ts"] == 123
    assert isinstance(rows[0]["ts_ms"], int)
