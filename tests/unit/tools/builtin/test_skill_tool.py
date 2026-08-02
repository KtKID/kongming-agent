"""unit：v0.1.6 skill-system 模块 B —— SkillTool 7 步执行流 + 安全 + 事件。

覆盖目标（≥ 11 用例）：

1. happy path：注册 spec → execute → ok=True + content=body 文本
2. ``$ARGUMENTS`` 替换
3. 三变量同时替换：``$ARGUMENTS`` / ``${KONGMING_SKILL_DIR}`` / ``${KONGMING_SESSION_ID}``
4. ``!command`` 拒绝 → ok=False + ``skill.failed`` 事件 emit + error_kind=SkillSecurityError
5. unknown skill name → ok=False，无 invoked / failed
6. body 读盘 OSError（路径不存在）→ ok=False + ``skill.failed`` emit + error_kind=FileNotFoundError
7. body 非 UTF-8 → ok=False + ``skill.failed`` emit + error_kind=UnicodeDecodeError
8. 变量替换不递归
9. 三事件 emit 顺序 [skill.invoked, skill.completed]，invocation 标识一致
10. sink 异常隔离：3 个 sink，第 1 个 raise，后 2 个仍正常 emit
11. tool.input_schema 完整性
12. ``skill.completed`` payload 含 body_chars + elapsed_ms + name
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from core.contracts import Event, ToolContext
from tests.support.tool_calls import execute_prepared_tool
from tools.builtin.skill_tool import (
    SKILL_TOOL_SCHEMA,
    SkillSecurityError,
    SkillTool,
    assert_no_command_substitution,
    substitute_vars,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeSpec:
    """duck-type :class:`prompting.skills.skill_loader.SkillSpec`：仅 SkillTool 用到的字段。"""

    name: str
    source: Literal["home", "workspace"]
    body_path: Path


class _CollectingSink:
    """收集 emit 到的 Event 列表（用于断言事件序列与 payload）。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _RaisingSink:
    """每次 emit 都抛 RuntimeError —— 用于验证 sink 异常隔离。"""

    async def emit(self, event: Event) -> None:
        raise RuntimeError("sink failure")


def _ctx(
    *,
    run_id: str = "run-1",
    session_id: str = "sess-1",
    turn: int = 1,
    call_id: str = "call-1",
) -> ToolContext:
    return ToolContext(
        run_id=run_id,
        session_id=session_id,
        turn=turn,
        call_id=call_id,
        metadata={},
    )


def _make_spec(
    tmp_path: Path,
    *,
    name: str = "demo",
    source: Literal["home", "workspace"] = "workspace",
    body: str = "hello world\n",
    body_filename: str = "SKILL.md",
) -> _FakeSpec:
    """在 tmp_path 下放一个 skill 目录与 body 文件，返回 _FakeSpec。"""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body_path = skill_dir / body_filename
    body_path.write_text(body, encoding="utf-8")
    return _FakeSpec(name=name, source=source, body_path=body_path)


# ---------------------------------------------------------------------------
# §1 happy path
# ---------------------------------------------------------------------------


async def test_skill_tool_happy_path(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="demo", body="hello world")
    tool = SkillTool(specs={spec.name: spec})

    result = await execute_prepared_tool(tool, {"skill": "demo"}, _ctx())

    assert result.ok is True
    assert result.content == "hello world"
    assert result.error_message is None
    # data 字段含 body_chars / elapsed_ms 等
    assert result.data is not None
    assert result.data["name"] == "demo"
    assert result.data["body_chars"] == len("hello world")


# ---------------------------------------------------------------------------
# §2 $ARGUMENTS 替换
# ---------------------------------------------------------------------------


async def test_skill_tool_arguments_substitution(tmp_path: Path) -> None:
    body = "echo: $ARGUMENTS"
    spec = _make_spec(tmp_path, name="echo", body=body)
    tool = SkillTool(specs={spec.name: spec})

    result = await execute_prepared_tool(tool, {"skill": "echo", "args": "hello"}, _ctx())

    assert result.ok is True
    assert result.content == "echo: hello"


# ---------------------------------------------------------------------------
# §3 三变量同时替换
# ---------------------------------------------------------------------------


async def test_skill_tool_three_variables(tmp_path: Path) -> None:
    body = "args=$ARGUMENTS\ndir=${KONGMING_SKILL_DIR}\nsid=${KONGMING_SESSION_ID}\n"
    spec = _make_spec(tmp_path, name="vars", body=body)
    tool = SkillTool(specs={spec.name: spec})

    ctx = _ctx(session_id="my-session-42")
    result = await execute_prepared_tool(tool, {"skill": "vars", "args": "abc"}, ctx)

    assert result.ok is True
    skill_dir_str = str(spec.body_path.parent)
    assert result.content == (f"args=abc\ndir={skill_dir_str}\nsid=my-session-42\n")


# ---------------------------------------------------------------------------
# §4 !command 拒绝
# ---------------------------------------------------------------------------


async def test_skill_tool_rejects_command_substitution(tmp_path: Path) -> None:
    body = "step 1\n!command rm -rf /\n"
    spec = _make_spec(tmp_path, name="bad", body=body)
    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    result = await execute_prepared_tool(tool, {"skill": "bad"}, _ctx())

    assert result.ok is False
    assert result.error_message is not None
    assert "!command" in result.error_message
    # 应 emit skill.invoked + skill.failed
    kinds = [e.kind for e in sink.events]
    assert kinds == ["skill.invoked", "skill.failed"]
    failed = sink.events[1]
    assert failed.payload["error_kind"] == "SkillSecurityError"
    assert failed.payload["name"] == "bad"
    assert "elapsed_ms" in failed.payload


# ---------------------------------------------------------------------------
# §5 unknown skill name —— ok=False，无 invoked / failed
# ---------------------------------------------------------------------------


async def test_skill_tool_unknown_name(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="known", body="x")
    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    result = await execute_prepared_tool(tool, {"skill": "nonexistent"}, _ctx())

    assert result.ok is False
    assert result.error_message is not None
    assert "not found" in result.error_message
    # 不 emit invoked / failed
    assert sink.events == []


# ---------------------------------------------------------------------------
# §6 body 读盘 OSError（路径不存在）
# ---------------------------------------------------------------------------


async def test_skill_tool_oserror(tmp_path: Path) -> None:
    """spec.body_path 指向不存在文件 → execute → ok=False + skill.failed emit。"""
    skill_dir = tmp_path / "missing"
    skill_dir.mkdir()
    missing_path = skill_dir / "SKILL.md"  # 不真正写入
    spec = _FakeSpec(name="missing", source="workspace", body_path=missing_path)

    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    result = await execute_prepared_tool(tool, {"skill": "missing"}, _ctx())

    assert result.ok is False
    assert result.error_message is not None
    assert "skill body read failed" in result.error_message
    kinds = [e.kind for e in sink.events]
    assert kinds == ["skill.invoked", "skill.failed"]
    failed = sink.events[1]
    # error_kind 是 OSError 子类（FileNotFoundError）的真实类名
    assert failed.payload["error_kind"] in {"FileNotFoundError", "OSError"}


# ---------------------------------------------------------------------------
# §7 body 非 UTF-8 → UnicodeDecodeError
# ---------------------------------------------------------------------------


async def test_skill_tool_unicode_decode_error(tmp_path: Path) -> None:
    skill_dir = tmp_path / "binary"
    skill_dir.mkdir()
    body_path = skill_dir / "SKILL.md"
    # 写入非 UTF-8 字节序列
    body_path.write_bytes(b"\xff\xfe\xfd\x00invalid")
    spec = _FakeSpec(name="binary", source="workspace", body_path=body_path)

    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    result = await execute_prepared_tool(tool, {"skill": "binary"}, _ctx())

    assert result.ok is False
    kinds = [e.kind for e in sink.events]
    assert kinds == ["skill.invoked", "skill.failed"]
    failed = sink.events[1]
    assert failed.payload["error_kind"] == "UnicodeDecodeError"


# ---------------------------------------------------------------------------
# §8 变量替换不递归
# ---------------------------------------------------------------------------


def test_substitute_vars_no_recursion_skill_dir() -> None:
    """``$ARGUMENTS`` 内容含 ``${KONGMING_SKILL_DIR}`` 不被二次展开（避免参数注入）。"""
    body = "[$ARGUMENTS]"
    out = substitute_vars(
        body,
        args="${KONGMING_SKILL_DIR}",
        skill_dir="/should/not/expand",
        session_id="sid",
    )
    assert out == "[${KONGMING_SKILL_DIR}]"
    assert "/should/not/expand" not in out


def test_substitute_vars_no_recursion_session_id() -> None:
    """``$ARGUMENTS`` 内容含 ``${KONGMING_SESSION_ID}`` 不被二次展开。"""
    body = "[$ARGUMENTS]"
    out = substitute_vars(
        body,
        args="${KONGMING_SESSION_ID}",
        skill_dir="/dir",
        session_id="my-sid",
    )
    assert out == "[${KONGMING_SESSION_ID}]"
    assert "my-sid" not in out


def test_assert_no_command_substitution_passes_clean_body() -> None:
    """干净 body（含 ``!`` 但不含 ``!command`` 字面）不抛。"""
    assert_no_command_substitution("This is great! Use cmd.")
    assert_no_command_substitution("")
    assert_no_command_substitution("step 1\nstep 2\n")


def test_assert_no_command_substitution_raises() -> None:
    with pytest.raises(SkillSecurityError) as exc_info:
        assert_no_command_substitution("hello !command bad")
    assert "!command" in str(exc_info.value)
    assert exc_info.value.details.get("forbidden_token") == "!command"


# ---------------------------------------------------------------------------
# §9 成功路径事件顺序
# ---------------------------------------------------------------------------


async def test_skill_tool_emits_invoked_completed_in_order(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="demo", body="ok")
    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    ctx = _ctx(run_id="run-A", turn=3)
    result = await execute_prepared_tool(tool, {"skill": "demo"}, ctx)

    assert result.ok is True
    kinds = [e.kind for e in sink.events]
    assert kinds == ["skill.invoked", "skill.completed"]
    invoked, completed = sink.events
    # 同一次调用的标识一致：name / source / run_id / turn
    assert invoked.payload["name"] == completed.payload["name"] == "demo"
    assert invoked.payload["source"] == completed.payload["source"] == "workspace"
    assert invoked.run_id == completed.run_id == "run-A"
    assert invoked.turn == completed.turn == 3


# ---------------------------------------------------------------------------
# §10 sink 异常隔离 —— 3 个 sink，第 1 个 raise，后 2 个仍正常 emit
# ---------------------------------------------------------------------------


async def test_skill_tool_sink_exception_isolated(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="demo", body="ok")
    raising = _RaisingSink()
    sink_b = _CollectingSink()
    sink_c = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[raising, sink_b, sink_c])

    # 不抛
    result = await execute_prepared_tool(tool, {"skill": "demo"}, _ctx())

    assert result.ok is True
    # 后两个 sink 收到完整事件序列
    assert [e.kind for e in sink_b.events] == ["skill.invoked", "skill.completed"]
    assert [e.kind for e in sink_c.events] == ["skill.invoked", "skill.completed"]


# ---------------------------------------------------------------------------
# §11 tool.input_schema 完整性
# ---------------------------------------------------------------------------


def test_skill_tool_schema_completeness(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="demo", body="x")
    tool = SkillTool(specs={spec.name: spec})

    schema: dict[str, Any] = tool.input_schema
    assert schema is SKILL_TOOL_SCHEMA  # 同一引用
    assert schema["type"] == "object"
    assert "properties" in schema
    properties = schema["properties"]
    assert "skill" in properties
    assert properties["skill"]["type"] == "string"
    assert "args" in properties
    assert properties["args"]["type"] == "string"
    assert schema["required"] == ["skill"]
    # tool 必须实现 Tool Protocol 的 name / description 字段
    assert tool.name == "skill"
    assert isinstance(tool.description, str)
    assert tool.description != ""


# ---------------------------------------------------------------------------
# §12 skill.completed payload 字段
# ---------------------------------------------------------------------------


async def test_skill_tool_completed_payload_fields(tmp_path: Path) -> None:
    body = "the quick brown fox"
    spec = _make_spec(tmp_path, name="demo", body=body)
    sink = _CollectingSink()
    tool = SkillTool(specs={spec.name: spec}, event_sinks=[sink])

    result = await execute_prepared_tool(tool, {"skill": "demo"}, _ctx())
    assert result.ok is True

    completed_events = [e for e in sink.events if e.kind == "skill.completed"]
    assert len(completed_events) == 1
    payload = completed_events[0].payload

    assert payload["name"] == "demo"
    assert payload["source"] == "workspace"
    # body_chars 是 int >= 0，等于实际替换后字符数
    body_chars = payload["body_chars"]
    assert isinstance(body_chars, int)
    assert body_chars == len(body)
    # elapsed_ms 是 int >= 0
    elapsed_ms = payload["elapsed_ms"]
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0


# ---------------------------------------------------------------------------
# §13 args 缺省 / None / 非 string 处理
# ---------------------------------------------------------------------------


async def test_skill_tool_args_default_empty_string(tmp_path: Path) -> None:
    """params 不传 args → $ARGUMENTS 替换为空串。"""
    spec = _make_spec(tmp_path, name="demo", body="x=$ARGUMENTS;")
    tool = SkillTool(specs={spec.name: spec})

    result = await execute_prepared_tool(tool, {"skill": "demo"}, _ctx())
    assert result.ok is True
    assert result.content == "x=;"


async def test_skill_tool_rejects_non_string_args(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, name="demo", body="x")
    tool = SkillTool(specs={spec.name: spec})

    result = await execute_prepared_tool(tool, {"skill": "demo", "args": 123}, _ctx())
    assert result.ok is False
    assert result.error_message is not None
    assert "args" in result.error_message
