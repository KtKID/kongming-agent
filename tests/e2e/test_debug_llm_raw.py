"""opt-in e2e：用 httpx DEBUG 日志捕获 LLM provider 原始 HTTP body。

项目当前的 ``llm.response`` 事件只落盘 ``finish_reason`` / ``has_tool_calls`` /
``usage`` 三个字段，看不到 provider 返回的完整 JSON（含厂商扩展字段如
``logprobs`` / ``reasoning_content`` / snowflake id 等）。

这份文件用**不改生产代码**的方式应急：打开 httpx + httpcore 的 DEBUG logger，
把一次真实 LLM 请求的完整 request/response 字节流写到一个持久化 log 文件，用户
事后可以 grep 看。

## 用法

默认跳过（不会在 `make test` / `make test-e2e` 里跑）。opt-in 开启：

.. code-block:: bash

    KONGMING_E2E_DEBUG_DUMP_RAW=1 uv run pytest tests/e2e/test_debug_llm_raw.py -v -s

**会真的发请求到 `config/setting.yaml` 配置的 LLM 服务**（本地 LM Studio /
Ollama / 远端 glm-5.1 / OpenAI / …），请确保服务可达且 API key 已通过 `.env`
或 env 注入。

## 看日志

跑完之后：

.. code-block:: bash

    ls -lt .kongming/debug/debug-llm-raw-*.log | head
    # 看完整 response body（httpcore 的 response_body.data 是字节流）
    grep -A 2 'response_body.data' .kongming/debug/debug-llm-raw-*.log | less
    # 看每轮 HTTP 请求大纲
    grep 'HTTP Request\|HTTP/1.1' .kongming/debug/debug-llm-raw-*.log

## 为什么两条测试

- 纯文本场景（"hi" 打招呼）：捕获最简单的 assistant 响应，看清 provider
  返回的 choices[0].message.content 以及 id / usage 等元数据字段
- tool_call 场景（让模型调 read_file）：捕获 tool_calls 数组的原始形态，
  看清厂商是怎么编码 function.name / function.arguments / tool_call.id 的
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from infrastructure.config import load_config
from runtime_assembly.session_engine import SessionEngine
from tools import AutoAllowApproval, ReadFileTool

_DUMP_ENV = "KONGMING_E2E_DEBUG_DUMP_RAW"
_LOG_DIR = Path(".kongming") / "debug"


def _raw_dump_snapshot(dump_dir: Path) -> set[Path]:
    """快照当前 dump 目录里已有的 raw-llm-*.json 文件，用来 diff 新增文件。"""
    if not dump_dir.exists():
        return set()
    return {p for p in dump_dir.iterdir() if p.name.startswith("raw-llm-") and p.suffix == ".json"}


pytestmark = pytest.mark.skipif(
    os.getenv(_DUMP_ENV) != "1",
    reason=(
        f"opt-in debug dump; set {_DUMP_ENV}=1 to enable "
        "(will make a REAL LLM call and write full HTTP body to .kongming/debug/)"
    ),
)


@pytest.fixture
def httpx_debug_dump_file(request: pytest.FixtureRequest) -> Iterator[Path]:
    """把 httpx / httpcore 的 DEBUG 日志写到 `.kongming/debug/<test_name>.log`。

    fixture 负责：

    - 开 logger 级别到 DEBUG（httpx + httpcore）
    - 挂一个独立 FileHandler，避免污染其它 logging 配置
    - 测试结束时 restore 原级别、摘掉 handler、关文件
    - 返回 log 文件路径给测试做 assertion 和 stdout 提示

    **注意**：httpcore 的子 logger（``httpcore.http11`` / ``httpcore.connection``）
    事件会 propagate 到 parent ``httpcore``；所以只给 ``httpcore`` 挂一次
    handler 就能收到全部子事件。之前给每个子 logger 都挂导致重复写入。
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 用测试名做后缀，同一 pytest 进程跑多条测试不会互相覆盖
    test_name = request.node.name.replace("/", "_").replace("::", "-")
    log_file = _LOG_DIR / f"debug-llm-raw-{test_name}.log"

    handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s %(levelname)s] %(message)s"))

    # 只挂 parent logger（httpx / httpcore），靠 propagate 继承给子 logger。
    logger_names = ["httpx", "httpcore"]
    loggers = [logging.getLogger(name) for name in logger_names]
    previous_levels = {lg.name: lg.level for lg in loggers}

    for lg in loggers:
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)

    try:
        yield log_file
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
            lg.setLevel(previous_levels[lg.name])
        handler.close()


async def test_dump_plain_chat_hi(
    httpx_debug_dump_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """场景 1：最简单的 "hi" 打招呼。

    同时开两条 dump 通道：
    - 方案 A：httpx DEBUG log → `.kongming/debug/debug-llm-raw-<test>.log`
      （连接/TLS/headers 流水，看不到 body）
    - 方案 B：KONGMING_TRACE_RAW_LLM=1 → `.kongming/debug/raw-llm-<ts>.json`
      （完整 request + response body 结构化 JSON，**能看到 provider 返回全貌**）

    断言放宽到"run 完成 + 两份 dump 都非空"，不依赖模型返回特定文本。
    """
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")
    dump_dir = _LOG_DIR  # .kongming/debug
    before = _raw_dump_snapshot(dump_dir)

    cfg = load_config()
    runtime = SessionEngine.build(
        cfg,
        approval=AutoAllowApproval(),
        tools={},
        enabled_tool_names=[],
    )

    result = await runtime.run("hi", session_id="debug-plain-chat")

    assert result.status == "completed", (
        f"LLM 请求未完成：status={result.status} error={result.error}"
    )

    # 方案 A：httpx DEBUG 日志
    content = httpx_debug_dump_file.read_text(encoding="utf-8")
    assert content.strip(), "httpx DEBUG 日志为空 —— fixture 未正确挂 handler？"
    assert "HTTP" in content or "request" in content.lower(), "日志文件未包含 HTTP 交互记录"

    # 方案 B：raw-llm-*.json 文件
    after = _raw_dump_snapshot(dump_dir)
    new_raw_dumps = sorted(after - before)
    assert new_raw_dumps, "KONGMING_TRACE_RAW_LLM=1 应该产出至少一个 raw-llm-*.json 文件"

    # 给用户的提示（pytest -s 才能看见）
    print(f"\n📄 方案 A httpx 日志：{httpx_debug_dump_file.resolve()}")
    print(f"📄 方案 B raw JSON：{new_raw_dumps[-1].resolve()}")
    print("   方案 B 是你真正想看的东西——打开就是完整 request+response JSON")


async def test_dump_tool_call_roundtrip(
    httpx_debug_dump_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景 2：给模型一个 ``read_file`` 工具并诱导它调用。

    同时开两条 dump 通道（同 test_dump_plain_chat_hi）；重点看方案 B 的
    raw-llm-*.json —— 里面能看到 provider 返回的 ``tool_calls[].id`` /
    ``function.arguments`` / ``finish_reason`` 的原始形态。
    """
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")
    dump_dir = _LOG_DIR
    before = _raw_dump_snapshot(dump_dir)

    # 造一个可读的测试文件
    sample = tmp_path / "debug-readme.txt"
    sample.write_text(
        "This is a test file for debug dump.\nContent: HELLO-WORLD-DEBUG-123.\n",
        encoding="utf-8",
    )

    cfg = load_config()
    registry: dict[str, object] = {"read_file": ReadFileTool()}
    runtime = SessionEngine.build(
        cfg,
        approval=AutoAllowApproval(),
        tools=registry,  # type: ignore[arg-type]  # dict 结构上满足 ToolLookup
        enabled_tool_names=["read_file"],
    )

    prompt = (
        f"Use the `read_file` tool to read the file at '{sample}' and "
        "then tell me the exact value after 'Content:' in the file."
    )
    result = await runtime.run(prompt, session_id="debug-tool-call")

    assert result.status == "completed", (
        f"LLM 请求未完成：status={result.status} error={result.error}"
    )

    content = httpx_debug_dump_file.read_text(encoding="utf-8")
    assert content.strip(), "httpx DEBUG 日志为空"

    after = _raw_dump_snapshot(dump_dir)
    new_raw_dumps = sorted(after - before)
    assert new_raw_dumps, "应至少生成一个方案 B raw-llm-*.json"

    # 软断言：如果模型调了工具，日志里应该出现 'tool_calls' 字样；没调也 OK
    tool_call_seen = "tool_calls" in content or "read_file" in content

    print(f"\n📄 方案 A httpx 日志：{httpx_debug_dump_file.resolve()}")
    print(f"📄 方案 B raw JSON：{new_raw_dumps[-1].resolve()}")
    print(f"   tool_call 是否被触发：{'是' if tool_call_seen else '否（模型仅文本回复）'}")
    print(
        f"   看 tool_calls 结构（方案 B，结构化）："
        f"jq '.response.body.choices[0].message.tool_calls' {new_raw_dumps[-1]}"
    )
