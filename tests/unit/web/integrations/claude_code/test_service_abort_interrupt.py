"""ClaudeCodeService.abort interrupt 行为单测（interrupt-claude-channel-v0.1 #3）。

覆盖 5 个用例，验证 #1 改造后的 ``ClaudeCodeService.abort()`` 契约：

1. 调 ``self._lookup_client_for_abort(session_id)`` 双 key 查 ``ClaudeSDKClient``
2. 找到 client → ``asyncio.create_task(self._safe_interrupt(client))`` fire-and-forget（**不 await**）
3. ``_safe_interrupt`` 内 try/except 跑 ``await client.interrupt()``，异常 logger.exception 吞掉
4. 之后**总是** ``return await self._sessions.request_abort(session_id)``

用例：

- (a) 正常路径：interrupt 被 await + request_abort 被调 + 返回 True
- (b) interrupt raise 时仍走兜底：异常吞掉 + request_abort 仍调 + 返回 True
- (c) ``_clients`` 找不到：create_task 不调 + request_abort 仍调
- (d) 幂等：连续 3 次不抛 + interrupt/request_abort 各调 3 次
- (e) v0.2 rename race：``_clients`` key 是 placeholder + sm._sessions[placeholder].session_id
       == sdk_uuid 时，``abort(sdk_uuid)`` fallback 命中 placeholder 下 client
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from web._shared.session_manager import SessionManager
from web.integrations.claude_code.approval import ApprovalBridge
from web.integrations.claude_code.normalizer import ClaudeNormalizer
from web.integrations.claude_code.service import ClaudeCodeService


class _FakeWriter:
    """最小 writer stub——abort 路径不会用到，仅注册 record 时需要持有引用。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_service() -> tuple[ClaudeCodeService, SessionManager]:
    """构造一个最小可用的 service 实例。

    abort 路径只依赖 ``_clients`` 字典 + ``_sessions`` SessionManager，
    其他依赖（normalizer / approval / client_factory / thread_manager / asset_storage）
    都不会被触发，所以直接拿默认实现就够。
    """
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions)
    return svc, sessions


async def _drain_pending_tasks() -> None:
    """让 fire-and-forget create_task 跑起来。

    ``asyncio.create_task`` 后 task 进入 ready queue，但当前协程不 yield 就不会被调度。
    多 sleep(0) 几次确保后台 task 跑完它的 await（``await client.interrupt()`` 是
    AsyncMock，单 yield 即返回）。
    """
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# (a) 正常路径：interrupt 被 await + request_abort 被调 + 返回 True
# ---------------------------------------------------------------------------


async def test_abort_normal_path_calls_interrupt_and_request_abort() -> None:
    """正常路径：_clients[sid] 命中 client，interrupt 被 fire-and-forget 跑掉，
    request_abort 兜底执行并返回 True。"""
    svc, sessions = _make_service()

    sid = "sid-normal"
    client = MagicMock()
    client.interrupt = AsyncMock()
    svc._clients[sid] = client

    # 注册一个 record，request_abort 才能找到并 set abort_event
    writer = _FakeWriter()
    record = await sessions.register(sid, writer)

    result = await svc.abort(sid)

    # 让 fire-and-forget 后台 task 跑完
    await _drain_pending_tasks()

    assert result is True, "request_abort 命中应返回 True"
    client.interrupt.assert_awaited_once()
    assert record.abort_event.is_set() is True, "request_abort 必须 set abort_event"


# ---------------------------------------------------------------------------
# (b) SDK interrupt raise 时仍走兜底
# ---------------------------------------------------------------------------


async def test_abort_interrupt_raises_but_fallback_still_runs() -> None:
    """interrupt 抛异常时，_safe_interrupt 的 try/except 吞掉；abort 本身不抛；
    request_abort 仍执行并返回 True。"""
    svc, sessions = _make_service()

    sid = "sid-interrupt-fail"
    client = MagicMock()
    client.interrupt = AsyncMock(side_effect=RuntimeError("simulated SDK failure"))
    svc._clients[sid] = client

    writer = _FakeWriter()
    record = await sessions.register(sid, writer)

    # 不应抛
    result = await svc.abort(sid)

    # 让后台 task 跑完（异常已被 _safe_interrupt 吞）
    await _drain_pending_tasks()

    assert result is True, "interrupt 失败不应阻挡 request_abort 兜底"
    client.interrupt.assert_awaited_once()
    assert record.abort_event.is_set() is True


# ---------------------------------------------------------------------------
# (c) _clients 找不到 session_id
# ---------------------------------------------------------------------------


async def test_abort_no_client_skips_interrupt_but_still_calls_request_abort() -> None:
    """_clients 为空时不调 create_task / interrupt，但 request_abort 仍走完。"""
    svc, sessions = _make_service()

    sid = "sid-no-client"
    # _clients 显式保持空
    assert svc._clients == {}

    # 仍要 register 让 request_abort 能命中
    writer = _FakeWriter()
    record = await sessions.register(sid, writer)

    # 用 asyncio.create_task 探针：拦截调用看是否被触发
    real_create_task = asyncio.create_task
    create_task_calls: list[Any] = []

    def spy_create_task(coro: Any, **kwargs: Any) -> Any:
        # 标记被 service.abort 主动创建的 task（name 含 claude-interrupt 前缀）
        name = kwargs.get("name", "")
        if isinstance(name, str) and name.startswith("claude-interrupt"):
            create_task_calls.append(name)
        return real_create_task(coro, **kwargs)

    # monkeypatch 在 service 模块级别引用上（service.py 里直接 asyncio.create_task）
    import web.integrations.claude_code.service as service_mod

    original = service_mod.asyncio.create_task
    service_mod.asyncio.create_task = spy_create_task  # type: ignore[assignment]
    try:
        result = await svc.abort(sid)
        await _drain_pending_tasks()
    finally:
        service_mod.asyncio.create_task = original  # type: ignore[assignment]

    assert svc._clients == {}, "_clients 不应被 modify"
    assert create_task_calls == [], "无 client 时不应 create_task 跑 interrupt"
    assert result is True
    assert record.abort_event.is_set() is True


# ---------------------------------------------------------------------------
# (d) 幂等：连续 3 次 abort
# ---------------------------------------------------------------------------


async def test_abort_idempotent_three_calls() -> None:
    """连续 3 次 abort：不抛 + interrupt 调 3 次（fire-and-forget 每次新建 task）+
    request_abort 调 3 次 + 最终状态一致。"""
    svc, sessions = _make_service()

    sid = "sid-idempotent"
    client = MagicMock()
    client.interrupt = AsyncMock()
    svc._clients[sid] = client

    writer = _FakeWriter()
    record = await sessions.register(sid, writer)

    results = []
    for _ in range(3):
        results.append(await svc.abort(sid))

    await _drain_pending_tasks()

    assert results == [True, True, True], "3 次 abort 都应返回 True"
    assert client.interrupt.await_count == 3, (
        "interrupt 应被 await 3 次（fire-and-forget 每次新建）"
    )
    assert record.abort_event.is_set() is True
    # SessionManager 状态仍正常：record 还在表里（abort 不 unregister）
    assert sessions.is_active(sid) is True


# ---------------------------------------------------------------------------
# (e) v0.2 rename race：_clients key 是 placeholder + record.session_id 是 sdk_uuid
# ---------------------------------------------------------------------------


async def test_abort_fallback_lookup_when_clients_key_is_placeholder() -> None:
    """v0.2 rename race：_clients 字典 key 还停在 placeholder（因为 rename 只动
    SessionManager._sessions 没同步 _clients），前端发的 sdk_uuid 在 _clients
    直接 get 落空；_lookup_client_for_abort 的 fallback 必须能反查到。

    构造：
    - _clients = {"placeholder-xxx": mock_client}
    - SessionManager._sessions 里 placeholder-xxx 这条记录的 session_id 字段
      已被 rename 改成 "real-sdk-uuid"（注意：rename 后 _sessions 的 key 也是
      "real-sdk-uuid"，placeholder 那条 key 会被删——这是 SessionManager.rename
      的真实行为。所以这里手动构造 race 场景：record.session_id 是 sdk_uuid，
      但 _sessions 字典 key 仍是 placeholder（模拟 _clients 与 _sessions 不同步
      的中间态，或测试 _lookup_client_for_abort 的 fallback 算法本身）。

    调用方传入 sdk_uuid → _clients.get(sdk_uuid) 落空 → 走 fallback 遍历
    _clients items，反查 _sessions[placeholder].session_id == sdk_uuid → 命中。
    """
    svc, sessions = _make_service()

    placeholder_key = "pending-12345"
    sdk_uuid = "real-sdk-uuid"

    mock_client = MagicMock()
    mock_client.interrupt = AsyncMock()
    svc._clients[placeholder_key] = mock_client

    # 手动构造 _sessions：key = placeholder，但 record.session_id = sdk_uuid
    writer = _FakeWriter()
    record = await sessions.register(placeholder_key, writer)
    # 直接改 record.session_id 模拟 race 中间态（绕过 rename，因为 rename 会
    # 同时改 _sessions 字典 key）
    record.session_id = sdk_uuid

    # 前端发 abort 用 sdk_uuid
    result = await svc.abort(sdk_uuid)
    await _drain_pending_tasks()

    # fallback 应命中 placeholder 下的 client
    mock_client.interrupt.assert_awaited_once()
    # 注意：request_abort(sdk_uuid) 在 _sessions 字典里查不到（key 还是 placeholder），
    # 所以兜底返回 False。这是预期：当前实现 _lookup_client_for_abort 能 fallback
    # 但 request_abort 是 SessionManager 自己的方法、用字典 key 直接查不做 fallback。
    # 测试要验证的核心契约是"fallback 反查 client 命中"，request_abort 返回值是
    # 副产品。
    assert result is False, (
        "request_abort 用 sdk_uuid 直接查 _sessions key 落空，返 False（符合 SessionManager.request_abort 的实现）"
    )
