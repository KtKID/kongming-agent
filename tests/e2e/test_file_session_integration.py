"""FileSession 集成测试（TC10, TC11, TC12）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_spec import AgentSpec
from core.contracts import ApprovalDecision, ApprovalRequest, LLMRequest, LLMResponse
from core.message import Message
from core.run_state import RunState
from core.runner import Runner
from infrastructure.config import Config
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap
from sessions.session_store import build_session


def _bootstrap(**overrides) -> SessionBootstrap:
    defaults = dict(
        agent_name="test-agent",
        model_name="test-model",
        instruction_sources=["test-source"],
        instruction_text_hash="sha256:abc123",
        created_at=1000000.0,
        cwd="/test",
        app_version="0.1.1",
    )
    defaults.update(overrides)
    return SessionBootstrap(**defaults)


def _default_config(
    backend: str = "memory",
    store_path: str = ".kongming/test-sessions.db",
    file_store_path: str = ".kongming/test-sessions",
) -> Config:
    return Config(
        model={"name": "test-model", "base_url": "http://127.0.0.1:1234"},
        session={"backend": backend, "store_path": store_path, "file_store_path": file_store_path},
    )


@pytest.fixture
def store_path(tmp_path: Path) -> str:
    return str(tmp_path / "sessions")


class _StubLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message.assistant("hello from llm"),
            finish_reason="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
        )


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


# ---------------------------------------------------------------------------
# TC10: Runner 首次只注入一条 system
# ---------------------------------------------------------------------------


class TestTC10SeedSystem:
    async def test_only_one_system_on_first_run(self, store_path: str) -> None:
        """_seed_messages + FileSession：首次运行只注入一条 system + 一条 user。"""
        bootstrap = _bootstrap()
        fs = FileSession("test-session", bootstrap, store_path)

        runner = Runner()
        agent_spec = AgentSpec(
            name="test-agent",
            instructions="You are a test agent.",
            default_model="test-model",
        )
        state = RunState(run_id="run-1", session_id="test-session")

        await runner._seed_messages(fs, agent_spec, "hello", state)

        history = await fs.history()
        assert len(history) == 2
        assert history[0].role == "system"
        assert history[0].content == "You are a test agent."
        assert history[1].role == "user"
        assert history[1].content == "hello"


# ---------------------------------------------------------------------------
# TC11: resume 后不重复注入 system
# ---------------------------------------------------------------------------


class TestTC11ResumeNoDuplicateSystem:
    async def test_no_duplicate_system_on_resume(self, store_path: str) -> None:
        """已有 system 时跳过，只写 user+后续。"""
        bootstrap = _bootstrap()
        sid = "resume-session"

        # 第一轮：注入 system + user
        fs1 = FileSession(sid, bootstrap, store_path)
        runner = Runner()
        agent_spec = AgentSpec(
            name="test-agent",
            instructions="You are a test agent.",
            default_model="test-model",
        )
        state1 = RunState(run_id="run-1", session_id=sid)
        await runner._seed_messages(fs1, agent_spec, "hello", state1)

        # 第二轮：用新 FileSession 实例恢复
        fs2 = FileSession(sid, bootstrap, store_path)
        state2 = RunState(run_id="run-2", session_id=sid)
        await runner._seed_messages(fs2, agent_spec, "next question", state2)

        history = await fs2.history()
        # 应该有 3 条：system, user(hello), user(next question)
        assert len(history) == 3
        system_msgs = [m for m in history if m.role == "system"]
        assert len(system_msgs) == 1
        assert history[2].role == "user"
        assert history[2].content == "next question"


# ---------------------------------------------------------------------------
# TC12: backend 切换不影响 memory/sqlite
# ---------------------------------------------------------------------------


class TestTC12BackendSwitch:
    async def test_memory_backend_still_works(self) -> None:
        """memory backend 不受影响。"""
        cfg = _default_config("memory")
        session = build_session(cfg, "mem-test", bootstrap=_bootstrap())
        await session.append(Message.user("hello"))
        history = await session.history()
        assert len(history) == 1
        assert history[0].content == "hello"

    async def test_sqlite_backend_still_works(self, tmp_path: Path) -> None:
        """sqlite backend 不受影响。"""
        db_path = str(tmp_path / "test.db")
        cfg = _default_config(
            "sqlite", store_path=db_path, file_store_path=str(tmp_path / "sessions")
        )
        session = build_session(cfg, "sqlite-test")
        await session.append(Message.user("hello"))
        history = await session.history()
        assert len(history) == 1
        assert history[0].content == "hello"

    async def test_file_backend_works(self, store_path: str) -> None:
        """file backend 正常工作。"""
        bootstrap = _bootstrap()
        cfg = _default_config("file", file_store_path=store_path)
        session = build_session(cfg, "file-test", bootstrap=bootstrap)
        await session.append(Message.user("hello"))
        history = await session.history()
        assert len(history) == 1
        assert history[0].content == "hello"


class TestTC13AssistantUsagePersisted:
    async def test_runner_persists_assistant_usage_to_file_session(self, store_path: str) -> None:
        bootstrap = _bootstrap()
        session = FileSession("usage-test", bootstrap, store_path)
        runner = Runner()
        spec = AgentSpec(name="test-agent", instructions="SYS", default_model="test-model")

        result = await runner.run(
            "hello",
            session=session,
            agent_spec=spec,
            llm=_StubLLM(),
            tools={},
            approval=_AllowApproval(),
        )

        assert result.status == "completed"
        jsonl_path = Path(store_path) / "usage-test" / "usage-test.jsonl"
        records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
        message_records = [
            record for record in records if record.get("record_type", "message") == "message"
        ]
        audit_records = [record for record in records if record.get("record_type") == "audit_event"]
        assert all(record["record_type"] == "message" for record in message_records)
        assistant_record = message_records[-1]
        assert assistant_record["message"]["role"] == "assistant"
        assert assistant_record["usage"] == {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        }
        request_records = [record for record in audit_records if record["kind"] == "llm.request"]
        assert len(request_records) == 1
        request_payload = request_records[0]["payload"]
        assert request_payload["model"] == "test-model"
        assert request_payload["message_count"] == 2
        assert request_payload["request"]["messages"][0]["role"] == "system"
        assert request_payload["request"]["messages"][0]["content"] == "SYS"
        assert request_payload["request"]["messages"][1]["role"] == "user"
        assert request_payload["request"]["messages"][1]["content"] == "hello"
