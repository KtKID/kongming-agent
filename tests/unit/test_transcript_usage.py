"""TranscriptUsage 解析单测。"""

from __future__ import annotations

import json
from pathlib import Path

from web.claude_code.transcript_usage import TranscriptUsage, parse_transcript_usage

# ---------- helpers ----------


def _write_jsonl(path: Path, entries: list[dict | str]) -> Path:
    """写入 JSONL 文件。entries 中 dict 会序列化，str 原样写入（模拟损坏行）。"""
    lines: list[str] = []
    for e in entries:
        if isinstance(e, dict):
            lines.append(json.dumps(e, ensure_ascii=False))
        else:
            lines.append(e)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _assistant_entry(usage: dict) -> dict:
    return {"type": "assistant", "message": {"usage": usage}}


def _user_entry() -> dict:
    return {"type": "user", "message": {"content": "hello"}}


def _system_entry() -> dict:
    return {"type": "system", "message": {"content": "system prompt"}}


# ---------- tests ----------


class TestParseTranscriptUsage:
    """覆盖 7 个场景。"""

    def test_normal_multiple_assistant(self, tmp_path: Path) -> None:
        """多行 assistant 带 usage → 累计正确。"""
        f = _write_jsonl(
            tmp_path / "t.jsonl",
            [
                _assistant_entry(
                    {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                    }
                ),
                _assistant_entry(
                    {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 15,
                    }
                ),
            ],
        )
        result = parse_transcript_usage(f)
        assert result == TranscriptUsage(
            input_tokens=300,
            output_tokens=130,
            cache_read_tokens=30,
            cache_creation_tokens=20,
        )

    def test_empty_file(self, tmp_path: Path) -> None:
        """空文件 → 全 0。"""
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        assert parse_transcript_usage(f) == TranscriptUsage()

    def test_file_not_exists(self, tmp_path: Path) -> None:
        """文件不存在 → 全 0，不抛异常。"""
        assert parse_transcript_usage(tmp_path / "no_such_file.jsonl") == TranscriptUsage()

    def test_corrupted_lines_skipped(self, tmp_path: Path) -> None:
        """损坏行（非 JSON）→ 跳过，其他行正常累计。"""
        f = _write_jsonl(
            tmp_path / "t.jsonl",
            [
                "NOT VALID JSON {{{",
                _assistant_entry({"input_tokens": 42, "output_tokens": 7}),
                "{broken",
            ],
        )
        result = parse_transcript_usage(f)
        assert result.input_tokens == 42
        assert result.output_tokens == 7

    def test_assistant_without_usage(self, tmp_path: Path) -> None:
        """assistant 条目无 usage 字段 → 不累加。"""
        f = _write_jsonl(
            tmp_path / "t.jsonl",
            [
                {"type": "assistant", "message": {"content": "hi"}},
                _assistant_entry({"input_tokens": 10}),
            ],
        )
        result = parse_transcript_usage(f)
        assert result.input_tokens == 10
        assert result.output_tokens == 0

    def test_negative_values_treated_as_zero(self, tmp_path: Path) -> None:
        """usage 中有负数 → 按 0 处理。"""
        f = _write_jsonl(
            tmp_path / "t.jsonl",
            [
                _assistant_entry(
                    {
                        "input_tokens": -100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": -1,
                        "cache_creation_input_tokens": 0,
                    }
                ),
            ],
        )
        result = parse_transcript_usage(f)
        assert result.input_tokens == 0
        assert result.output_tokens == 50
        assert result.cache_read_tokens == 0
        assert result.cache_creation_tokens == 0

    def test_mixed_entry_types(self, tmp_path: Path) -> None:
        """混合条目（user / assistant / system）→ 只累加 assistant。"""
        f = _write_jsonl(
            tmp_path / "t.jsonl",
            [
                _user_entry(),
                _assistant_entry({"input_tokens": 10, "output_tokens": 5}),
                _system_entry(),
                _assistant_entry({"input_tokens": 20, "output_tokens": 15}),
                _user_entry(),
            ],
        )
        result = parse_transcript_usage(f)
        assert result == TranscriptUsage(
            input_tokens=30,
            output_tokens=20,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
