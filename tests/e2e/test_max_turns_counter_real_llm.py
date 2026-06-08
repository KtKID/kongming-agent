"""e2e：用真实本地 LLM + 自定义 counter tool 验证 max_turns 物理拦截。

unit 测试已经用 stub LLM 验证了 ``state.turn >= max_turns`` 的内部行为，但
那是"runner 自己说自己拦了"。本用例换一个角度：

- 用真本地模型 (``KONGMING_MODEL_BASE_URL`` 默认 ``http://127.0.0.1:1234``)
- 注册一个 :class:`_CounterTool`：每次调用读 ``.kongming/debug/counter.txt``
  里的整数 → +1 → 写回 → 把当前值塞回 LLM
- 让 LLM 持续调这个 tool，目标是把值加到 ``max_turns + 1``
- runner 应该在第 ``max_turns`` 轮把它物理拦下来 → ``MaxTurnsExceededError``

文件值是**关键证据**：``state.turn`` 是 runner 内部计数，文件值是 tool
真的被 LLM 调用了多少次的外部副作用。两个值都等于 ``max_turns`` 才能
证明 LLM 真的跑满了 ``max_turns`` 次才被拦下，不是别的路径短路。

默认 skip。设置 ``KONGMING_E2E_REAL_MODEL=1`` 显式开启。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.agent_spec import AgentSpec
from core.contracts import Session, ToolContext
from core.errors import MaxTurnsExceededError
from core.session import InMemorySession
from infrastructure.config import load_config
from infrastructure.config.paths import get_kongming_home
from runtime_assembly.native_runtime import NativeRuntime
from safety.capability_policy import CapabilityPolicy, CapabilitySet
from tools.runtime.approval import AutoAllowApproval
from tools.runtime.base import BaseBuiltinTool
from tools.runtime.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL_MODEL_YAML = REPO_ROOT / "config" / "setting.yaml"

# 本地小模型对 multi-turn tool-call 训练不足，约调 3 次就会自我满足主动 stop
# （实测 gemma-4-e4b-it 加到 counter=3 必停，再强的 prompt 也压不住）。
# 测试要可靠地触发 runner 的 max_turns 物理拦截，必须让 max_turns 比 LLM 自我
# 停止点更小——这样 LLM 还没"想停"就先撞上限，物理拦截一定发生。
# 这同时也避免单测跑过久（每个 turn 一次本地模型 round-trip）。
_MAX_TURNS_CAP = 3


def _counter_path() -> Path:
    """``.kongming/debug/counter.txt`` 的绝对路径。"""
    return get_kongming_home() / "debug" / "counter.txt"


def _read_counter() -> int:
    p = _counter_path()
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8").strip()
    return int(text) if text else 0


def _write_counter(value: int) -> None:
    p = _counter_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value), encoding="utf-8")


class _CounterTool(BaseBuiltinTool):
    """把传入的整数写到 ``.kongming/debug/counter.txt``，返回 ok。"""

    name = "set_counter"
    description = "Write the given integer value to the counter file."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "value": {"type": "integer", "description": "Integer to store."},
        },
        "required": ["value"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        value = int(args["value"])
        _write_counter(value)
        return "ok", {"value": value}


@pytest.fixture
def clean_counter_file() -> Iterator[Path]:
    """测试前后清空 counter 文件，避免上一次残留值污染本次。"""
    p = _counter_path()
    if p.exists():
        p.unlink()
    yield p
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# 真模型路径（opt-in）
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason=(
        "requires local model service at 127.0.0.1:1234; set KONGMING_E2E_REAL_MODEL=1 to enable"
    ),
)
async def test_real_llm_max_turns_physically_caps_counter_tool(
    clean_counter_file: Path,
) -> None:
    """让真实 LLM 不停调 counter tool，验证 runner 在 max_turns 处把它物理拦下来。

    步骤：
      1. 加载 ``config/setting.yaml`` → 取 ``cfg.runner.max_turns``，硬卡到
         ``_MAX_TURNS_CAP`` 以下（防生产配置写得过大跑爆时间）
      2. 用自定义 :class:`_CounterTool` + 全开 :class:`CapabilityPolicy` +
         :class:`AutoAllowApproval` 装配 NativeRuntime（默认安全链会按
         tool_name="counter_increment" 走 fallback capability，非 file_read 会被
         deny；测试场景需要全开）
      3. instruction 指示 LLM 反复调 tool 把 counter 加到 ``max_turns + 1``
         （目标必须 > max_turns 才能保证不会因 LLM 主动 stop 而走 completed
         路径，那样就不触发上限了）
      4. 期望：``status=failed`` / ``error`` 是 :class:`MaxTurnsExceededError` /
         ``turn_count == capped_max_turns`` / 文件值 == ``capped_max_turns``
    """
    cfg = load_config(LOCAL_MODEL_YAML)
    capped = min(cfg.runner.max_turns, _MAX_TURNS_CAP)
    target = capped + 1  # 目标必须严格 > max_turns，否则 LLM 提前 stop 不触发上限

    counter_tool = _CounterTool()
    registry = ToolRegistry([counter_tool])

    instructions = (
        "Use the set_counter tool to count from 1 up to "
        f"{target}. Call set_counter with value=1, then value=2, then value=3, "
        f"and so on, until value={target}."
    )

    agent_spec = AgentSpec(
        name="counter-runner",
        instructions=instructions,
        default_model=cfg.model.name,
        tool_names=("set_counter",),
        max_turns=capped,
        reasoning_effort=cfg.model.reasoning_effort,
    )

    # 强制用 InMemorySession 绕开 file backend：避免固定 session_id 在多次跑之间
    # 累积旧 history（cfg.session.backend=file 默认会持久化到 .kongming/sessions/）
    def _in_memory_session_factory(sid: str) -> Session:
        return InMemorySession(session_id=sid)

    runtime = NativeRuntime.build(
        cfg,
        tools=registry,
        enabled_tool_names=["set_counter"],
        agent_spec=agent_spec,
        # 全开 capability：set_counter 不在默认白名单，会被 fallback deny
        capability_policy=CapabilityPolicy(CapabilitySet()),
        # auto_allow：跳过 interactive 审批
        approval=AutoAllowApproval(),
        session_factory=_in_memory_session_factory,
    )

    try:
        result = await runtime.run(
            f"Count from 1 to {target} using set_counter.",
            session_id=f"max-turns-counter-{uuid.uuid4().hex[:8]}",
        )
    finally:
        await runtime.aclose()

    final_counter = _read_counter()

    assert result.status == "failed", (
        f"expected runner to fail at max_turns, got status={result.status}; "
        f"final_counter={final_counter}, turn_count={result.turn_count}"
    )
    assert isinstance(result.error, MaxTurnsExceededError), (
        f"expected MaxTurnsExceededError, got {type(result.error).__name__}: {result.error}"
    )
    assert result.turn_count == capped, (
        f"turn_count should equal capped max_turns={capped}, got {result.turn_count}"
    )
    # 文件值是外部副作用证据：LLM 真的执行了 capped 次 tool_call
    assert final_counter == capped, (
        f"counter file value should equal capped max_turns={capped}, "
        f"got {final_counter} (means LLM did NOT actually execute the tool that "
        f"many times — runner may have been short-circuited by a different path)"
    )
