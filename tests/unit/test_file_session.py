"""FileSession 单元测试（TC1-TC9 + validate）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

import pytest

from context.file_session import FileSession, ValidationResult
from context.session_bootstrap import SessionBootstrap
from core.message import Message


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


@pytest.fixture
def store_path(tmp_path: Path) -> str:
    return str(tmp_path / "sessions")


def _make_session(
    session_id: str = "test-session",
    store_path: str = "",
    bootstrap: SessionBootstrap | None = None,
) -> FileSession:
    return FileSession(
        session_id=session_id,
        bootstrap=bootstrap or _bootstrap(),
        store_path=store_path,
    )


# ---------------------------------------------------------------------------
# TC1: 创建 FileSession 对象时不落盘
# ---------------------------------------------------------------------------


class TestTC1NoDiskOnCreate:
    async def test_not_materialized(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        assert fs._materialized is False

    async def test_last_message_id_is_none(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        assert fs._last_message_id is None

    async def test_no_directory_created(self, store_path: str) -> None:
        _make_session(store_path=store_path)
        assert not os.path.exists(os.path.join(store_path, "test-session"))

    async def test_no_manifest(self, store_path: str) -> None:
        _make_session(store_path=store_path)
        manifest = os.path.join(store_path, "test-session", "manifest.json")
        assert not os.path.exists(manifest)

    async def test_no_jsonl(self, store_path: str) -> None:
        _make_session(store_path=store_path)
        jsonl = os.path.join(store_path, "test-session", "test-session.jsonl")
        assert not os.path.exists(jsonl)


# ---------------------------------------------------------------------------
# TC2: 首次 append 触发 materialize 并直接写入
# ---------------------------------------------------------------------------


class TestTC2FirstAppendMaterialize:
    async def test_directory_created(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        assert os.path.isdir(os.path.join(store_path, "test-session"))

    async def test_manifest_created(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        assert os.path.isfile(os.path.join(store_path, "test-session", "manifest.json"))

    async def test_jsonl_created(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        assert os.path.isfile(os.path.join(store_path, "test-session", "test-session.jsonl"))

    async def test_materialized_true(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        assert fs._materialized is True

    async def test_message_in_first_line(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            record = json.loads(f.readline())
        assert record["message"]["content"] == "hello"
        assert record["message"]["role"] == "user"

    async def test_last_message_id_set(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        assert fs._last_message_id is not None


# ---------------------------------------------------------------------------
# TC3: manifest.json 包含完整 bootstrap 字段（10 个）
# ---------------------------------------------------------------------------


class TestTC3ManifestFields:
    EXPECTED_FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "session_id",
        "created_at",
        "agent_name",
        "model_name",
        "instruction_sources",
        "instruction_text_hash",
        "cwd",
        "app_version",
        "format",
    }

    async def test_all_fields_present(self, store_path: str) -> None:
        bootstrap = _bootstrap()
        fs = _make_session(store_path=store_path, bootstrap=bootstrap)
        await fs.append(Message.user("hello"))
        manifest_path = os.path.join(store_path, "test-session", "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)
        assert self.EXPECTED_FIELDS == set(manifest.keys())

    async def test_values_match_bootstrap(self, store_path: str) -> None:
        bootstrap = _bootstrap()
        fs = _make_session(store_path=store_path, bootstrap=bootstrap)
        await fs.append(Message.user("hello"))
        manifest_path = os.path.join(store_path, "test-session", "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)
        assert manifest["agent_name"] == bootstrap.agent_name
        assert manifest["model_name"] == bootstrap.model_name
        assert manifest["instruction_sources"] == bootstrap.instruction_sources
        assert manifest["instruction_text_hash"] == bootstrap.instruction_text_hash
        assert manifest["created_at"] == bootstrap.created_at
        assert manifest["cwd"] == bootstrap.cwd
        assert manifest["app_version"] == bootstrap.app_version
        assert manifest["session_id"] == "test-session"


# ---------------------------------------------------------------------------
# TC4: 消息记录包含完整字段
# ---------------------------------------------------------------------------


class TestTC4RecordFields:
    ENVELOPE_FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "session_id",
        "model_name",
        "message_id",
        "parent_message_id",
        "created_at",
        "message",
    }
    MESSAGE_FIELDS: ClassVar[set[str]] = {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "metadata",
    }

    async def test_envelope_fields(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            record = json.loads(f.readline())
        assert self.ENVELOPE_FIELDS == set(record.keys())

    async def test_message_sub_fields(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("hello"))
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            record = json.loads(f.readline())
        assert self.MESSAGE_FIELDS == set(record["message"].keys())

    async def test_model_name_value_matches_bootstrap(self, store_path: str) -> None:
        bootstrap = _bootstrap()
        fs = _make_session(store_path=store_path, bootstrap=bootstrap)
        await fs.append(Message.user("hello"))
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            record = json.loads(f.readline())
        assert record["model_name"] == bootstrap.model_name


# ---------------------------------------------------------------------------
# TC5: 消息链 parent_message_id 正确串联
# ---------------------------------------------------------------------------


class TestTC5ParentChain:
    async def test_chain_of_three(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("first"))
        await fs.append(Message.assistant("second"))
        await fs.append(Message.user("third"))

        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            records = [json.loads(line) for line in f if line.strip()]

        assert len(records) == 3
        # 首条 parent 为 null
        assert records[0]["parent_message_id"] is None
        # 后续指向前一条
        assert records[1]["parent_message_id"] == records[0]["message_id"]
        assert records[2]["parent_message_id"] == records[1]["message_id"]


# ---------------------------------------------------------------------------
# TC6: history() 返回 append 顺序一致
# ---------------------------------------------------------------------------


class TestTC6HistoryOrder:
    async def test_order_and_content(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.system("system-prompt"))
        await fs.append(Message.user("hello"))
        await fs.append(Message.assistant("world"))
        await fs.append(Message.user("question"))

        history = await fs.history()
        assert len(history) == 4
        assert history[0].role == "system"
        assert history[0].content == "system-prompt"
        assert history[1].role == "user"
        assert history[1].content == "hello"
        assert history[2].role == "assistant"
        assert history[2].content == "world"
        assert history[3].role == "user"
        assert history[3].content == "question"


# ---------------------------------------------------------------------------
# TC7: 重开同一 session 续写
# ---------------------------------------------------------------------------


class TestTC7Resume:
    async def test_resume_and_continue(self, store_path: str) -> None:
        bootstrap = _bootstrap()
        sid = "resume-test"

        # 第一轮：写入 2 条
        fs1 = _make_session(session_id=sid, store_path=store_path, bootstrap=bootstrap)
        await fs1.append(Message.user("first"))
        await fs1.append(Message.assistant("second"))
        last_id_round1 = fs1._last_message_id

        # 第二轮：新实例续写
        fs2 = _make_session(session_id=sid, store_path=store_path, bootstrap=bootstrap)
        assert fs2._materialized is True
        await fs2.append(Message.user("third"))

        # 新消息 parent 指向最后一条
        jsonl_path = os.path.join(store_path, sid, f"{sid}.jsonl")
        with open(jsonl_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert len(records) == 3
        assert records[2]["parent_message_id"] == last_id_round1


# ---------------------------------------------------------------------------
# TC8: 损坏尾行恢复
# ---------------------------------------------------------------------------


class TestTC8CorruptedTail:
    async def test_skip_corrupted_line(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("msg1"))
        await fs.append(Message.assistant("msg2"))

        # 手工追加损坏行
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path, "a") as f:
            f.write('{"broken json without closing\n')

        # history 跳过损坏行
        history = await fs.history()
        assert len(history) == 2
        assert history[0].content == "msg1"
        assert history[1].content == "msg2"

    async def test_can_append_after_corruption(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("msg1"))

        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path, "a") as f:
            f.write('{"broken\n')

        # 续写
        await fs.append(Message.user("msg3"))
        history = await fs.history()
        assert len(history) == 2
        assert history[0].content == "msg1"
        assert history[1].content == "msg3"


# ---------------------------------------------------------------------------
# TC9: clear() 后重新起链
# ---------------------------------------------------------------------------


class TestTC9Clear:
    async def test_history_empty_after_clear(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("msg1"))
        await fs.append(Message.assistant("msg2"))
        await fs.clear()
        history = await fs.history()
        assert history == []

    async def test_new_chain_after_clear(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("old"))
        await fs.clear()

        # 新消息 parent=null
        await fs.append(Message.user("new"))
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            record = json.loads(f.readline())
        assert record["parent_message_id"] is None
        assert record["message"]["content"] == "new"


# ---------------------------------------------------------------------------
# validate() 测试
# ---------------------------------------------------------------------------


class TestValidate:
    async def test_valid_chain(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("a"))
        await fs.append(Message.assistant("b"))
        await fs.append(Message.user("c"))
        result = fs.validate()
        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == []

    async def test_not_materialized_is_valid(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        result = fs.validate()
        assert result.valid is True

    async def test_detects_duplicate_message_id(self, store_path: str) -> None:
        fs = _make_session(store_path=store_path)
        await fs.append(Message.user("a"))

        # 手工插入一条重复 message_id
        jsonl_path = os.path.join(store_path, "test-session", "test-session.jsonl")
        with open(jsonl_path) as f:
            first_record = json.loads(f.readline())
        dup_record = dict(first_record)
        dup_record["parent_message_id"] = first_record["message_id"]
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(dup_record) + "\n")

        result = fs.validate()
        assert result.valid is False
        assert any("重复" in e for e in result.errors)
