from __future__ import annotations

from commands.parser import parse_input


def test_parse_plain_text():
    parsed = parse_input("hello world")
    assert parsed.kind == "text"
    assert parsed.command_name is None


def test_parse_review_command():
    parsed = parse_input("/review auth")
    assert parsed.kind == "command"
    assert parsed.command_name == "review"
    assert parsed.args_text == "auth"


def test_parse_absolute_path_as_text():
    parsed = parse_input("/tmp/project")
    assert parsed.kind == "text"


def test_parse_invalid_command_name_as_text():
    parsed = parse_input("/Review")
    assert parsed.kind == "text"
