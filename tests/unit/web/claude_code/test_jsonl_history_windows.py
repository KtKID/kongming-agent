from pathlib import Path

from web.claude_code.jsonl_history import encode_cwd, jsonl_path_for


def test_encode_cwd_replaces_windows_drive_and_backslashes() -> None:
    assert encode_cwd(r"E:\xgt\proj\agent-proj\kongming-agent") == (
        "E--xgt-proj-agent-proj-kongming-agent"
    )


def test_jsonl_path_for_windows_cwd_matches_sdk_layout(tmp_path: Path) -> None:
    path = jsonl_path_for(
        r"E:\xgt\proj\agent-proj\kongming-agent",
        "sid-win",
        claude_home=tmp_path,
    )
    assert path == (
        tmp_path / "projects" / "E--xgt-proj-agent-proj-kongming-agent" / "sid-win.jsonl"
    )
