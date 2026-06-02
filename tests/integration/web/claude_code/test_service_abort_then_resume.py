"""集成：``ClaudeCodeService.abort()`` 后下次 ``query()`` 不破坏 client 状态
（interrupt-claude-channel-v0.1 dev-checklist #7，P1）。

#1 改造把 ``abort()`` 拆成两步：

1. ``asyncio.create_task(client.interrupt())`` — fire-and-forget，走 SDK
   ``_send_control_request``（query.py:619 → 407），内部用 ``anyio.Event`` 等
   CLI 子进程响应
2. ``await self._sessions.request_abort(sid)`` — task.cancel 兜底

潜在风险：cc-agent-sdk 在 ``interrupt()`` 后若 ``pending_control_responses``
残留、anyio.Event 没清，同 session 下次 ``client.query(...)`` 可能失败或卡住。

本测试用 mock SDK client 验证：service 层调度上不会破坏 client 状态——同
session_id 第二次 ``query`` 仍然命中 ``_clients[sid]`` 复用同一个 client，
``client.query`` 第二次被调到，整个流程不抛异常。

约束：
- 不调真 SDK / 不起真子进程
- 用 AsyncMock 模拟 ``connect`` / ``query`` / ``interrupt`` / ``disconnect``
- 用 async generator 模拟 ``receive_response``（yield 零条消息即正常结束）
- 每个 case 独立 service 实例（测试隔离）
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from web._shared.session_manager import SessionManager
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer
from web.claude_code.service import ClaudeCodeService


class _FakeWriter:
    """最小 writer stub——收集 send_json，便于断言不抛异常。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_empty_receive_response() -> Any:
    """构造一个 ``receive_response()`` 的返回值——空 async iterator。

    ``service._consume`` 走 ``async for msg in client.receive_response():``，
    yield 零条即立即结束，不触发 normalizer 任何分支，但能验证 ``_consume``
    跑完一次完整生命周期。
    """

    async def _gen() -> AsyncIterator[Any]:
        if False:  # 永不 yield，但保持 async generator 形态
            yield None  # pragma: no cover
        return

    return _gen


def _make_mock_client() -> MagicMock:
    """构造一个 SDK ``ClaudeSDKClient`` 的 mock。

    需要 ``service.query`` / ``service.abort`` / ``service._consume`` 覆盖到的
    所有方法：connect / query / receive_response / interrupt / disconnect。
    """
    client = MagicMock()
    client.connect = AsyncMock()
    client.query = AsyncMock()
    # receive_response 是同步方法，返回 async iterator
    client.receive_response = MagicMock(side_effect=_make_empty_receive_response())
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def _make_service(
    mock_client: MagicMock,
) -> tuple[ClaudeCodeService, SessionManager, list[MagicMock]]:
    """构造一个 service 实例，注入 client_factory 让 service 装配出指定 mock client。

    client_factory 第一次被调时返回 ``mock_client``；后续调用（理论上不应发生，
    因为 _clients[sid] 缓存命中复用）也返回 mock_client，便于调用方断言。

    Returns:
        (svc, sessions, created_clients) — created_clients 是 factory 真正
        返回过的 client 列表，断言"复用"靠 ``len(created_clients) == 1``。
    """
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)

    created_clients: list[MagicMock] = []

    def factory(**_kwargs: Any) -> MagicMock:
        created_clients.append(mock_client)
        return mock_client

    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=factory,
    )
    return svc, sessions, created_clients


async def _drain_pending_tasks() -> None:
    """让 fire-and-forget ``create_task`` 跑完。"""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 场景 1：abort 后立即 resume —— 同 session 下次 query 复用 client 不破坏状态
# ---------------------------------------------------------------------------


async def test_abort_then_resume_reuses_same_client() -> None:
    """abort 后立即起第二次 query 同 session_id：

    1. 第一次 query —— factory 调 1 次造 client，client.query 调 1 次
    2. service.abort(sid) —— interrupt 被 fire-and-forget 跑掉
    3. 第二次 query 同 sid —— factory **不再调**（_clients 缓存命中），
       client.query 调到第 2 次，整个调用链不抛异常
    4. _clients[sid] 仍然指向同一个 client（"复用"契约）
    """
    mock_client = _make_mock_client()
    svc, _sessions, created_clients = _make_service(mock_client)

    sid = "thread-resume-001"
    writer = _FakeWriter()

    # --- 1. 第一次 query ---
    await svc.query(
        command="hello",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )

    assert len(created_clients) == 1, "首次 query 必须经 factory 造一次 client"
    assert mock_client.connect.await_count == 1, "首次 query 必须调 connect"
    assert mock_client.query.await_count == 1, "首次 query 必须把 prompt 发给 client"

    # --- 2. abort ---
    aborted = await svc.abort(sid)
    await _drain_pending_tasks()

    # abort 时 register 已 unregister（query 走完 finally），所以 request_abort
    # 返回 False（_sessions 表里查不到）；这是正常的：测试核心是 client 状态
    # 一致性，不是 request_abort 返回值。但 interrupt 必须被 await 一次。
    assert aborted is False, "query() 走完 finally 已 unregister，request_abort 查不到 → False"
    assert mock_client.interrupt.await_count == 1, "abort 必须 fire-and-forget interrupt 一次"

    # --- 3. 第二次 query 同 sid ---
    # 关键断言：不抛异常 + client 被复用 + query 第二次被发
    await svc.query(
        command="follow-up",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )

    # --- 4. 复用契约 ---
    assert len(created_clients) == 1, "第二次 query 必须复用 _clients[sid]，不应再造新 client"
    assert mock_client.connect.await_count == 1, "复用 client 不应再调 connect（仅首次）"
    assert mock_client.query.await_count == 2, "第二次 query 应再调一次 client.query"
    # client 仍在缓存里——abort 不清缓存
    assert svc._clients.get(sid) is mock_client


# ---------------------------------------------------------------------------
# 场景 2：abort 多次（幂等）后 resume —— 仍能正常起第二次 query
# ---------------------------------------------------------------------------


async def test_abort_multiple_times_then_resume() -> None:
    """连续 3 次 abort（幂等）后再起第二次 query：

    1. 第一次 query 完成
    2. abort × 3 —— 每次都不抛，interrupt 累计调 3 次
    3. 第二次 query 同 sid —— 仍然复用 client、query 调到第 2 次、不抛
    """
    mock_client = _make_mock_client()
    svc, _sessions, created_clients = _make_service(mock_client)

    sid = "thread-resume-002"
    writer = _FakeWriter()

    # --- 1. 第一次 query ---
    await svc.query(
        command="first",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )
    assert mock_client.query.await_count == 1

    # --- 2. abort × 3 ---
    results = []
    for _ in range(3):
        results.append(await svc.abort(sid))
    await _drain_pending_tasks()

    # 3 次都不抛，且每次都 fire-and-forget 调一次 interrupt
    assert mock_client.interrupt.await_count == 3, (
        "幂等 abort：每次都新建 fire-and-forget task 调 interrupt"
    )
    # request_abort 全部落空（unregister 后）—— 不影响契约
    assert results == [False, False, False]

    # --- 3. 第二次 query —— 复用 client + 不抛 ---
    await svc.query(
        command="second-after-many-aborts",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )

    assert len(created_clients) == 1, "多次 abort 后不应丢失 client 缓存"
    assert mock_client.query.await_count == 2, "第二次 query 应能正常发 prompt"
    assert svc._clients.get(sid) is mock_client


# ---------------------------------------------------------------------------
# 场景 3：abort 期间 interrupt 抛异常，下次 query 仍然正常（吞掉契约）
# ---------------------------------------------------------------------------


async def test_abort_with_interrupt_failure_does_not_break_resume() -> None:
    """``client.interrupt`` 抛异常时 ``_safe_interrupt`` 吞掉；下次 query 仍能正常起。

    回归保护 #1 改造的关键契约：interrupt 失败不能污染 client 状态、不能让
    下次 query 卡住或抛。
    """
    mock_client = _make_mock_client()
    mock_client.interrupt = AsyncMock(side_effect=RuntimeError("simulated SDK fail"))
    svc, _sessions, created_clients = _make_service(mock_client)

    sid = "thread-resume-003"
    writer = _FakeWriter()

    # 第一次 query
    await svc.query(
        command="first",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )

    # abort —— interrupt 会抛但被吞
    await svc.abort(sid)
    await _drain_pending_tasks()
    assert mock_client.interrupt.await_count == 1

    # 第二次 query —— 不抛
    await svc.query(
        command="second",
        options={"sessionId": sid},
        writer=writer,
        register_id_override=sid,
    )
    assert mock_client.query.await_count == 2, "interrupt 失败不应阻挡下次 query"
    assert len(created_clients) == 1, "client 缓存仍应保留"
