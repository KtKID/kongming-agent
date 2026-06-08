"""audit.py 单测：JSONL 追加 + schema 完整 + 并发 O_APPEND 安全。"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from web.approvals.auto.audit import AuditLogger


@pytest.fixture()
def logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


class TestAuditBasic:
    def test_file_created(self, logger: AuditLogger) -> None:
        assert logger.path.exists()

    def test_log_request_writes_one_line(self, logger: AuditLogger) -> None:
        logger.log_request(
            channel="claude_code",
            thread_id="t1",
            cwd="/proj",
            tool_name="Bash",
            arguments={"command": "ls"},
            matched_rule=None,
            all_enabled_rules=["bash_sudo", "bash_rm_recursive"],
            timeout_ms=10000,
        )
        lines = logger.path.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["channel"] == "claude_code"
        assert rec["thread_id"] == "t1"
        assert rec["cwd"] == "/proj"
        assert rec["tool_name"] == "Bash"
        assert rec["arguments"] == {"command": "ls"}
        assert rec["rule_evaluation"]["matched"] is None
        assert rec["rule_evaluation"]["all_rules"] == ["bash_sudo", "bash_rm_recursive"]
        assert rec["decision"]["outcome"] == "pending"
        assert rec["decision"]["decided_at_ms"] is None
        assert rec["decision"]["timeout_ms"] == 10000

    def test_log_decision_writes_all_fields(self, logger: AuditLogger) -> None:
        logger.log_decision(
            channel="claude_code",
            thread_id="t1",
            cwd="/proj",
            tool_name="Bash",
            arguments={"command": "rm -rf /"},
            matched_rule="bash_rm_recursive",
            all_enabled_rules=["bash_rm_recursive"],
            outcome="denied",
            decided_by="user",
            timeout_ms=10000,
        )
        rec = logger.read_all()[0]
        assert rec["decision"]["outcome"] == "denied"
        assert rec["decision"]["decided_by"] == "user"
        assert rec["decision"]["decided_at_ms"] is not None
        assert rec["rule_evaluation"]["matched"] == "bash_rm_recursive"

    def test_outcomes_all_supported(self, logger: AuditLogger) -> None:
        for outcome, decided_by in [
            ("denied", "user"),
            ("allowed_manual", "user"),
            ("allowed_auto_timeout", "timeout"),
            ("blocked_then_manual", "user"),
        ]:
            logger.log_decision(
                channel="claude_code",
                thread_id="t",
                cwd="/p",
                tool_name="Bash",
                arguments={},
                matched_rule=None,
                all_enabled_rules=[],
                outcome=outcome,
                decided_by=decided_by,
                timeout_ms=10000,
            )
        records = logger.read_all()
        assert [r["decision"]["outcome"] for r in records] == [
            "denied",
            "allowed_manual",
            "allowed_auto_timeout",
            "blocked_then_manual",
        ]

    def test_invalid_outcome_is_marked(self, logger: AuditLogger) -> None:
        # 不抛——审计宁可记录脏数据也不阻断
        logger.log_decision(
            channel="claude_code",
            thread_id="t",
            cwd="/p",
            tool_name="Bash",
            arguments={},
            matched_rule=None,
            all_enabled_rules=[],
            outcome="bogus_outcome",
            decided_by="user",
            timeout_ms=10000,
        )
        rec = logger.read_all()[0]
        assert rec["decision"]["outcome"].startswith("unknown:")

    def test_channel_field_present_for_multi_channel(self, logger: AuditLogger) -> None:
        """v1 只跑 claude_code，但 channel 字段必须存在（为 generic_chat 留位）。"""
        logger.log_request(
            channel="generic_chat",
            thread_id="t",
            cwd="/p",
            tool_name="Bash",
            arguments={},
            matched_rule=None,
            all_enabled_rules=[],
            timeout_ms=10000,
        )
        rec = logger.read_all()[0]
        assert "channel" in rec
        assert rec["channel"] == "generic_chat"


class TestAuditConcurrent:
    def test_concurrent_append_no_interleave(self, logger: AuditLogger) -> None:
        """50 个线程各写 20 行；总行数必须 = 1000，每行必须 valid JSON。"""
        N_THREADS = 50
        N_LINES_EACH = 20

        def writer(tid: int) -> None:
            for i in range(N_LINES_EACH):
                logger.log_decision(
                    channel="claude_code",
                    thread_id=f"thread-{tid}",
                    cwd=f"/p/{tid}",
                    tool_name="Bash",
                    arguments={"command": f"echo t{tid}-i{i}"},
                    matched_rule=None,
                    all_enabled_rules=[],
                    outcome="allowed_manual",
                    decided_by="user",
                    timeout_ms=10000,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            list(pool.map(writer, range(N_THREADS)))

        lines = logger.path.read_text().splitlines()
        assert len(lines) == N_THREADS * N_LINES_EACH

        # 每行必须是 valid JSON
        parsed = [json.loads(line) for line in lines]
        thread_ids = sorted(rec["thread_id"] for rec in parsed)
        # 每个 thread-id 应出现 N_LINES_EACH 次
        from collections import Counter

        counts = Counter(thread_ids)
        for tid in range(N_THREADS):
            assert counts[f"thread-{tid}"] == N_LINES_EACH

    def test_read_all_skips_corrupted_lines(self, logger: AuditLogger) -> None:
        logger.log_decision(
            channel="claude_code",
            thread_id="t",
            cwd="/p",
            tool_name="Bash",
            arguments={},
            matched_rule=None,
            all_enabled_rules=[],
            outcome="denied",
            decided_by="user",
            timeout_ms=10000,
        )
        # 手动追加损坏行
        with logger.path.open("a", encoding="utf-8") as f:
            f.write("not a valid json\n")
        logger.log_decision(
            channel="claude_code",
            thread_id="t",
            cwd="/p",
            tool_name="Bash",
            arguments={},
            matched_rule=None,
            all_enabled_rules=[],
            outcome="allowed_manual",
            decided_by="user",
            timeout_ms=10000,
        )
        records = logger.read_all()
        assert len(records) == 2  # 损坏的那行被跳过
