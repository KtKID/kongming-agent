from __future__ import annotations

import json
from pathlib import Path

from core.message import Message, ToolCall
from infrastructure.tracing import PromptDebugDumpSink


def test_prompt_debug_dump_writes_prompt_snapshot_json(tmp_path) -> None:
    sink = PromptDebugDumpSink(output_dir=tmp_path)

    path = sink.dump(
        session_id="session/with unsafe chars",
        run_id="run_abc",
        turn=2,
        model="stub-model",
        instruction_origins=["agent_spec", "memory"],
        history_before_assemble=[Message.user("hi")],
        assembled_messages=[
            Message.system("SYS"),
            Message.user("hi"),
            Message.assistant(tool_calls=[ToolCall("call-1", "read_file", {"path": "a"})]),
        ],
        metadata={"added_system": True, "instruction_sources": [""]},
        added_system_prompt="SYS",
    )

    assert "prompt-debug-session-with-unsafe-chars-run_abc-turn-2-" in path

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["session_id"] == "session/with unsafe chars"
    assert payload["run_id"] == "run_abc"
    assert payload["turn"] == 2
    assert payload["model"] == "stub-model"
    assert payload["instruction_origins"] == ["agent_spec", "memory"]
    assert payload["added_system_prompt"] == "SYS"
    assert payload["system_prompts"] == ["SYS"]
    assert payload["history_before_assemble"] == [{"role": "user", "content": "hi"}]
    assert payload["metadata"]["instruction_sources"] == [""]
    assert payload["assembled_messages"][2]["tool_calls"] == [
        {"call_id": "call-1", "tool_name": "read_file", "arguments": {"path": "a"}}
    ]


def test_default_debug_dir_uses_kongming_home(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.chdir(workspace)
    sink = PromptDebugDumpSink()

    path = Path(
        sink.dump(
            session_id="sid",
            run_id="run-1",
            turn=1,
            model="stub-model",
            instruction_origins=[],
            history_before_assemble=[],
            assembled_messages=[Message.system("SYS")],
            metadata={},
            added_system_prompt="SYS",
        )
    )

    assert path.parent == (home / "debug").resolve()
