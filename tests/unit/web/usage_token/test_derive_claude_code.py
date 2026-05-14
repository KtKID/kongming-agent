"""_derive_claude_code 派生器单测。

覆盖场景：

- 文件不存在 / 不可读 → None
- 空文件 / 没有 assistant entry → None
- 单条 assistant entry → cumulative == last_snapshot
- 多条 assistant entry → cumulative 是累加，last_snapshot 是最后一条
- 行级损坏（一行 JSON 坏掉）→ skip 损坏行，其余正常累加
- assistant entry 缺 usage 字段 → skip 该行（不影响其他行）
- 取 message.model 作为 last_model_name；最后一条没有时取倒数第二条的有效值
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# tests/ 目录不受 importlinter 约束，可以直接 import 内部模块以验证派生器行为
from web.usage_token._derive_claude_code import (
    ClaudeCodeDerived,
    derive_from_jsonl,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """写一个 jsonl 文件（行尾自动加 \\n）。"""
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _assistant(usage: dict, model: str = "claude-opus-4") -> dict:
    """构造一条 type=assistant 行（最小字段）。"""
    return {
        "type": "assistant",
        "sessionId": "deadbeef-1234",
        "uuid": "u1",
        "timestamp": "2026-05-14T00:00:00Z",
        "message": {
            "model": model,
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "usage": usage,
        },
    }


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    result = derive_from_jsonl(tmp_path / "nonexistent.jsonl")
    assert result is None


def test_returns_none_for_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert derive_from_jsonl(p) is None


def test_returns_none_when_no_assistant_entry(tmp_path: Path) -> None:
    p = tmp_path / "no-assistant.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "user", "sessionId": "s1", "message": {"content": "hi"}},
            {"type": "system", "subtype": "init", "sessionId": "s1"},
        ],
    )
    assert derive_from_jsonl(p) is None


def test_single_assistant_cumulative_equals_last_snapshot(tmp_path: Path) -> None:
    p = tmp_path / "single.jsonl"
    _write_jsonl(
        p,
        [
            _assistant(
                {
                    "input_tokens": 10,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 5,
                },
            ),
        ],
    )
    result = derive_from_jsonl(p)
    assert isinstance(result, ClaudeCodeDerived)
    # cumulative
    assert result.cumulative.input_tokens == 10
    assert result.cumulative.output_tokens == 50
    assert result.cumulative.cache_read_input_tokens == 100
    assert result.cumulative.cache_creation_input_tokens == 5
    # last_snapshot
    assert result.last_snapshot.channel == "anthropic"
    assert result.last_snapshot.input_tokens == 10
    assert result.last_snapshot.output_tokens == 50
    assert result.last_snapshot.extras == {
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 5,
    }
    # context_usage = input + cache_read + cache_creation
    assert result.last_snapshot.context_usage == 10 + 100 + 5
    # turn / run_id 占位
    assert result.last_snapshot.turn == 0
    assert result.last_snapshot.run_id == ""
    # model
    assert result.last_model_name == "claude-opus-4"


def test_multi_assistant_cumulative_accumulates_last_is_final(tmp_path: Path) -> None:
    p = tmp_path / "multi.jsonl"
    _write_jsonl(
        p,
        [
            _assistant(
                {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
                model="claude-opus-4",
            ),
            _assistant(
                {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                },
                model="claude-sonnet-4-5",
            ),
            _assistant(
                {
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 400,
                },
                model="claude-sonnet-4-5",
            ),
        ],
    )
    result = derive_from_jsonl(p)
    assert result is not None
    # cumulative：1+10+100, 2+20+200, 3+30+300, 4+40+400
    assert result.cumulative.input_tokens == 111
    assert result.cumulative.output_tokens == 222
    assert result.cumulative.cache_read_input_tokens == 333
    assert result.cumulative.cache_creation_input_tokens == 444
    # last_snapshot 是最后一条
    assert result.last_snapshot.input_tokens == 100
    assert result.last_snapshot.output_tokens == 200
    assert result.last_snapshot.context_usage == 100 + 300 + 400
    # model 取最后一条
    assert result.last_model_name == "claude-sonnet-4-5"


def test_skips_malformed_line_continues_accumulating(tmp_path: Path) -> None:
    p = tmp_path / "malformed.jsonl"
    valid = _assistant({"input_tokens": 5, "output_tokens": 7})
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(valid) + "\n")
        fh.write("{this is not valid json\n")  # 坏行
        fh.write(json.dumps(valid) + "\n")
    result = derive_from_jsonl(p)
    assert result is not None
    # 两条 valid，损坏行 skip
    assert result.cumulative.input_tokens == 10
    assert result.cumulative.output_tokens == 14


def test_skips_assistant_without_usage(tmp_path: Path) -> None:
    p = tmp_path / "no-usage.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "sessionId": "s1",
                "message": {"model": "claude-opus-4", "content": []},
                # 缺 usage
            },
            _assistant({"input_tokens": 7, "output_tokens": 3}),
        ],
    )
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.cumulative.input_tokens == 7
    assert result.cumulative.output_tokens == 3


def test_last_model_falls_back_to_earlier_when_final_missing(tmp_path: Path) -> None:
    """最后一条 message 没 model 字段时，应保留最近一次见到的非空 model。"""
    p = tmp_path / "model-fallback.jsonl"
    _write_jsonl(
        p,
        [
            _assistant({"input_tokens": 1, "output_tokens": 2}, model="claude-opus-4"),
            {
                "type": "assistant",
                "sessionId": "s1",
                "message": {
                    # 缺 model
                    "usage": {"input_tokens": 5, "output_tokens": 10},
                },
            },
        ],
    )
    result = derive_from_jsonl(p)
    assert result is not None
    # last_snapshot 应该来自最后一条（无 model 也算 last，cumulative 累加正确）
    assert result.cumulative.input_tokens == 6
    assert result.cumulative.output_tokens == 12
    assert result.last_snapshot.input_tokens == 5
    assert result.last_snapshot.output_tokens == 10
    # last_model 是倒数第二条（最后一条没 model 字段，保留之前的）
    assert result.last_model_name == "claude-opus-4"


def test_empty_usage_dict_treated_as_missing(tmp_path: Path) -> None:
    """``usage`` 是空 dict 时跟缺失等价（避免 0 行干扰累加）。"""
    p = tmp_path / "empty-usage.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "sessionId": "s1",
                "message": {"model": "x", "usage": {}},
            },
        ],
    )
    assert derive_from_jsonl(p) is None


def test_streaming_read_handles_large_files(tmp_path: Path) -> None:
    """流式按行读 —— 1000 条 entry 不一次性 readall。"""
    p = tmp_path / "large.jsonl"
    entries = [_assistant({"input_tokens": 1, "output_tokens": 1}) for _ in range(1000)]
    _write_jsonl(p, entries)
    result = derive_from_jsonl(p)
    assert result is not None
    assert result.cumulative.input_tokens == 1000
    assert result.cumulative.output_tokens == 1000


def test_unreadable_file_returns_none(tmp_path: Path) -> None:
    """权限错误（mode 0o000）应该被 OSError 兜底返回 None。"""
    p = tmp_path / "no-perm.jsonl"
    p.write_text("[]", encoding="utf-8")
    p.chmod(0o000)
    try:
        result = derive_from_jsonl(p)
        # macOS / Linux 下 chmod 000 + 当前用户非 root → 不可读 → None
        # 注意：CI 偶尔 root，权限丢失测试可能 skip
        if result is not None:
            pytest.skip("running as root; cannot test permission denial")
    finally:
        p.chmod(0o644)  # 还回去让 tmp_path 清理不抱怨
