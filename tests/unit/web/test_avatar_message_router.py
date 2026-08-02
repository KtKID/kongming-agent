"""Avatar REST Router 合同测试。

本脚本验证 `/api/avatar/v1/*` 真实 HTTP 合同，作用是固定 Web cookie 调试路径、
XSpace device token scope 矩阵、错误码和 chat disabled 响应。关键流程是用
真实 `create_app`、Auth/CSRF middleware、SQLite repository 和 TestClient 走完整
Router 链路。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.avatar import AvatarMessageInput
from hosts.web.xspace_mobile.models import MobileDeviceDescriptor
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import CSRF_HEADERS, FakeTM, _make_cfg


class _RouterFakeBridge:
    """Router 测试 bridge，记录 run_once 入参。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[dict[str, object]] = []

    async def run_once(
        self,
        text: str,
        *,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, object]] | None = None,
        references: list[dict[str, object]] | None = None,
    ) -> None:
        """记录 Avatar REST chat 触发的 run_once。"""
        self.calls.append(
            {
                "text": text,
                "reasoning_effort": reasoning_effort,
                "attachments": attachments,
            }
        )


class _RouterFakeCell:
    """Router 测试 cell，提供 bridge/current_run_task/touch。"""

    def __init__(self, thread_id: str) -> None:
        """初始化 fake cell。"""
        self.thread_id = thread_id
        self.bridge = _RouterFakeBridge()
        self.current_run_task = None
        self.touch_count = 0

    def touch(self) -> None:
        """记录 cell 活跃刷新。"""
        self.touch_count += 1


class _AvatarChatFakeTM(FakeTM):
    """支持 Avatar REST chat 的 FakeThreadManager。"""

    def __init__(self) -> None:
        """初始化 fake cells 和刷新记录。"""
        super().__init__()
        self.cells: dict[str, _RouterFakeCell] = {}
        self.refresh_calls: list[str] = []
        self.pending_avatar_inputs: dict[str, list[dict[str, object]]] = {}

    async def boot_or_attach(self, thread_id: str) -> _RouterFakeCell:
        """返回或创建 fake cell。"""
        if thread_id not in self._threads:
            raise KeyError(thread_id)
        cell = self.cells.get(thread_id)
        if cell is None:
            cell = _RouterFakeCell(thread_id)
            self.cells[thread_id] = cell
        return cell

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """记录 runtime refresh 并返回成功。"""
        self.refresh_calls.append(thread_id)
        return True

    async def submit_avatar_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, object]] | None = None,
        avatar_run_id: str | None = None,
    ) -> object:
        """复刻 ThreadManager Avatar 输入入口，active run 时进入队列。"""
        cell = await self.boot_or_attach(thread_id)
        payload: dict[str, object] = {
            "text": text,
            "request_id": request_id,
            "reasoning_effort": reasoning_effort,
            "attachments": attachments,
            "avatar_run_id": avatar_run_id,
        }
        if cell.current_run_task is not None:
            self.pending_avatar_inputs.setdefault(thread_id, []).append(payload)
            return object()
        await cell.bridge.run_once(
            text,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
        )
        return object()


class _FakeApprovalInboxBroadcaster:
    """记录 Avatar approval resolve 路由到 inbox broadcaster 的调用。"""

    def __init__(self, *, ok: bool = True) -> None:
        """初始化 resolve 结果和调用记录。"""
        self.ok = ok
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.remember_rule = {
            "expression": "run_shell(git status:*)",
            "displayText": "记住 /workspace 中的 git status",
            "scopeCwd": "/workspace",
        }

    async def resolve(
        self,
        thread_id: str,
        request_id: str,
        decision: dict[str, object],
    ) -> bool:
        """记录 ApprovalInboxBroadcaster.resolve 入参。"""
        self.calls.append((thread_id, request_id, decision))
        return self.ok

    def remember_rule_for(
        self,
        thread_id: str,
        request_id: str,
    ) -> dict[str, object]:
        """返回路由测试使用的冻结 remember 候选。"""
        assert thread_id
        assert request_id
        return dict(self.remember_rule)


def _authed_client(
    tmp_path: Path,
    cfg: Config | None = None,
    thread_manager: FakeTM | None = None,
) -> TestClient:
    """创建已登录 Web TestClient。

    关键输入：pytest 临时目录和可选 Config。
    关键输出：带 Web session cookie 的 TestClient。
    """
    _seed_password(tmp_path, "pwd")
    app = create_app(cfg or _make_cfg(), thread_manager or FakeTM(), home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return client


def _anonymous_client(app: FastAPI) -> TestClient:
    """创建共享同一 app.state 的匿名 TestClient。

    关键输入：已经由登录 client 启动 lifespan 的 FastAPI app。
    关键输出：无 Web session cookie 的 TestClient。
    """
    return TestClient(app)


def _issue_device_token(client: TestClient, *, device_id: str, scopes: list[str]) -> str:
    """签发 XSpace mobile device token。

    关键输入：已启动 app 的 TestClient、设备 ID 和 scope 列表。
    关键输出：只用于测试请求的 Bearer token 明文。
    """
    result = client.app.state.xspace_mobile_token_service.issue_device_token(
        device=MobileDeviceDescriptor(
            device_id=device_id,
            label=device_id,
            platform="android",
            app_version="0.1.0",
        ),
        scopes=scopes,
    )
    return result.device_token


def _register_via_manager(client: TestClient, title: str = "Background event") -> str:
    """直接通过 app.state.avatar_manager 注册测试消息。

    关键输入：已启动 app 的 TestClient 和标题。
    关键输出：注册后的 messageId。
    """
    message = client.app.state.avatar_manager.register_message(
        AvatarMessageInput(
            source="task",
            title=title,
            body="ready for avatar",
            thread_id="thread-router-test",
            dedupe_key=title,
        )
    )
    return message.message_id


def test_debug_register_then_list_and_ack_with_testclient(tmp_path: Path) -> None:
    """验证 Web cookie 调试注册、list 和单条 ack 真实路由链路。"""
    authed = _authed_client(tmp_path)
    try:
        registered = authed.post(
            "/api/avatar/v1/messages",
            json={
                "source": "approval",
                "title": "Needs approval",
                "body": "A shell command is waiting",
                "level": "warning",
                "priority": 90,
                "threadId": "thread-router-test",
                "runId": "run-router-test",
                "requestId": "req-router-test",
                "dedupeKey": "approval:req-router-test",
                "metadata": {"surface": "pytest"},
            },
            headers=CSRF_HEADERS,
        )
        assert registered.status_code == 200, registered.text
        body = registered.json()
        assert body["source"] == "approval"
        assert body["threadId"] == "thread-router-test"
        assert body["metadata"] == {"surface": "pytest"}

        listed = authed.get("/api/avatar/v1/messages")
        assert listed.status_code == 200, listed.text
        assert [item["messageId"] for item in listed.json()["items"]] == [body["messageId"]]

        acked = authed.post(
            f"/api/avatar/v1/messages/{body['messageId']}/ack",
            json={"status": "consumed", "consumerId": "xspace-avatar"},
            headers=CSRF_HEADERS,
        )
        assert acked.status_code == 200, acked.text
        assert acked.json()["status"] == "consumed"
        assert acked.json()["consumedAt"] is not None
    finally:
        authed.__exit__(None, None, None)


def test_avatar_v1_bringup_auth_passes_for_list_and_ack(tmp_path: Path) -> None:
    """验证 Avatar v1 联调期 list 和 ack 直接放行鉴权。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        message_id = _register_via_manager(authed)
        read_token = _issue_device_token(
            authed,
            device_id="avatar-read",
            scopes=["avatar.read"],
        )
        thread_read_token = _issue_device_token(
            authed,
            device_id="avatar-thread-read",
            scopes=["thread.read"],
        )
        wrong_token = _issue_device_token(
            authed,
            device_id="avatar-wrong",
            scopes=["webview"],
        )
        ack_token = _issue_device_token(
            authed,
            device_id="avatar-ack",
            scopes=["avatar.ack"],
        )

        for token in (read_token, thread_read_token):
            response = anonymous.get(
                "/api/avatar/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text
            assert [item["messageId"] for item in response.json()["items"]] == [message_id]

        no_auth_response = anonymous.get("/api/avatar/v1/messages")
        assert no_auth_response.status_code == 200, no_auth_response.text
        assert [item["messageId"] for item in no_auth_response.json()["items"]] == [message_id]

        pass_through_ack = anonymous.post(
            f"/api/avatar/v1/messages/{message_id}/ack",
            json={"status": "consumed"},
            headers={"Authorization": f"Bearer {wrong_token}"},
        )
        assert pass_through_ack.status_code == 200, pass_through_ack.text
        assert pass_through_ack.json()["status"] == "consumed"

        allowed_ack = anonymous.post(
            f"/api/avatar/v1/messages/{message_id}/ack",
            json={"status": "consumed"},
            headers={"Authorization": f"Bearer {ack_token}"},
        )
        assert allowed_ack.status_code == 200, allowed_ack.text
        assert allowed_ack.json()["status"] == "consumed"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_batch_ack_returns_per_item_errors(tmp_path: Path) -> None:
    """验证批量 ack 返回逐项成功和缺失错误。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        message_id = _register_via_manager(authed, title="Batch event")
        response = anonymous.post(
            "/api/avatar/v1/messages/ack",
            json={
                "messageIds": [message_id, "missing"],
                "status": "consumed",
                "consumerId": "xspace-avatar",
            },
        )

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results[0]["ok"] is True
        assert results[0]["message"]["messageId"] == message_id
        assert results[1] == {
            "messageId": "missing",
            "ok": False,
            "message": None,
            "error": "avatar_message_not_found",
        }
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_routes_three_actions(tmp_path: Path) -> None:
    """验证 Avatar resolve endpoint 将三态动作映射到 inbox broadcaster。"""
    authed = _authed_client(tmp_path)
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        actions = ["accept_once", "accept_for_session", "reject"]
        for idx, action in enumerate(actions):
            request_id = f"approval-req-{idx}"
            response = authed.post(
                f"/api/avatar/v1/approvals/{request_id}/resolve",
                json={
                    "threadId": "thread-router-test",
                    "callId": request_id,
                    "requestId": request_id,
                    "action": action,
                    "clientId": "xspace-avatar",
                },
                headers=CSRF_HEADERS,
            )
            assert response.status_code == 200, response.text
            assert response.json() == {
                "ok": True,
                "requestId": request_id,
                "action": action,
            }

        assert manager.calls == [
            (
                "thread-router-test",
                "approval-req-0",
                {"allow": True, "remember": False},
            ),
            (
                "thread-router-test",
                "approval-req-1",
                {
                    "allow": True,
                    "remember": True,
                    "rememberRule": manager.remember_rule,
                },
            ),
            (
                "thread-router-test",
                "approval-req-2",
                {"allow": False, "remember": False},
            ),
        ]
    finally:
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_requires_web_session(tmp_path: Path) -> None:
    """验证匿名 Avatar approval resolve 被全局 AuthMiddleware 拦截。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = anonymous.post(
            "/api/avatar/v1/approvals/approval-req/resolve",
            json={
                "threadId": "thread-router-test",
                "callId": "approval-req",
                "requestId": "approval-req",
                "action": "accept_once",
                "clientId": "xspace-avatar",
            },
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 401, response.text
        assert manager.calls == []
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_requires_csrf_header(tmp_path: Path) -> None:
    """验证登录态 Avatar approval resolve 缺 CSRF header 时被拦截。"""
    authed = _authed_client(tmp_path)
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = authed.post(
            "/api/avatar/v1/approvals/approval-req/resolve",
            json={
                "threadId": "thread-router-test",
                "callId": "approval-req",
                "requestId": "approval-req",
                "action": "accept_once",
                "clientId": "xspace-avatar",
            },
        )

        assert response.status_code == 403, response.text
        assert manager.calls == []
    finally:
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_rejects_bearer_only(tmp_path: Path) -> None:
    """验证 Bearer token 不能单独写 Avatar approval resolve。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    token = _issue_device_token(
        authed,
        device_id="avatar-approval-resolve",
        scopes=["avatar.chat"],
    )
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = anonymous.post(
            "/api/avatar/v1/approvals/approval-req/resolve",
            json={
                "threadId": "thread-router-test",
                "callId": "approval-req",
                "requestId": "approval-req",
                "action": "accept_once",
                "clientId": "xspace-avatar",
            },
            headers={
                **CSRF_HEADERS,
                "Authorization": f"Bearer {token}",
            },
        )

        assert response.status_code == 401, response.text
        assert manager.calls == []
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_returns_not_found_for_missing_pending(
    tmp_path: Path,
) -> None:
    """验证 pending 缺失返回稳定 Avatar 错误。"""
    authed = _authed_client(tmp_path)
    manager = _FakeApprovalInboxBroadcaster(ok=False)
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = authed.post(
            "/api/avatar/v1/approvals/missing-req/resolve",
            json={
                "threadId": "thread-router-test",
                "callId": "missing-req",
                "requestId": "missing-req",
                "action": "reject",
                "clientId": "xspace-avatar",
            },
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "avatar_approval_not_found"
        assert manager.calls == [
            (
                "thread-router-test",
                "missing-req",
                {"allow": False, "remember": False},
            )
        ]
    finally:
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_rejects_mismatched_request_id(tmp_path: Path) -> None:
    """验证 path requestId 和 body 字段不一致时返回稳定错误。"""
    authed = _authed_client(tmp_path)
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = authed.post(
            "/api/avatar/v1/approvals/path-req/resolve",
            json={
                "threadId": "thread-router-test",
                "callId": "path-req",
                "requestId": "body-req",
                "action": "accept_once",
                "clientId": "xspace-avatar",
            },
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "avatar_invalid_request"
        assert manager.calls == []
    finally:
        authed.__exit__(None, None, None)


def test_avatar_approval_resolve_requires_thread_id(tmp_path: Path) -> None:
    """验证 Avatar approval resolve 必须携带 threadId。"""
    authed = _authed_client(tmp_path)
    manager = _FakeApprovalInboxBroadcaster()
    authed.app.state.approval_inbox_broadcaster = manager
    try:
        response = authed.post(
            "/api/avatar/v1/approvals/path-req/resolve",
            json={
                "callId": "path-req",
                "requestId": "path-req",
                "action": "accept_once",
                "clientId": "xspace-avatar",
            },
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "avatar_invalid_request"
        assert manager.calls == []
    finally:
        authed.__exit__(None, None, None)


def test_avatar_router_keeps_safety_import_out_of_route_boundary() -> None:
    """验证 Avatar router 不直接依赖 safety 包。"""
    router_source = Path("src/hosts/web/routers/avatar.py").read_text(encoding="utf-8")
    assert "safety." not in router_source


def test_avatar_capabilities_and_chat_accepted(tmp_path: Path) -> None:
    """验证 capabilities 和 REST chat accepted 合同。"""
    fake_tm = _AvatarChatFakeTM()
    authed = _authed_client(tmp_path, thread_manager=fake_tm)
    anonymous = _anonymous_client(authed.app)
    try:
        capabilities = anonymous.get("/api/avatar/v1/capabilities")
        assert capabilities.status_code == 200
        capabilities_body = capabilities.json()
        assert capabilities_body["avatarChat"] is True
        assert capabilities_body["avatarRealtimeChat"] is True
        assert capabilities_body["chatTransports"] == {
            "websocket": "/ws/avatar/v1/threads/{threadId}",
            "rest": "/api/avatar/v1/chat",
        }
        assert capabilities_body["messageRegistry"] is True

        read_token = _issue_device_token(
            authed,
            device_id="avatar-capabilities",
            scopes=["avatar.read"],
        )
        readable_capabilities = anonymous.get(
            "/api/avatar/v1/capabilities",
            headers={"Authorization": f"Bearer {read_token}"},
        )
        assert readable_capabilities.status_code == 200
        assert readable_capabilities.json()["avatarChat"] is True

        response = anonymous.post(
            "/api/avatar/v1/chat",
            json={
                "threadId": None,
                "presetId": "local-default",
                "cwd": "/tmp/avatar",
                "message": {
                    "text": "hello avatar",
                    "reasoningEffort": "medium",
                    "attachments": None,
                    "metadata": {"surface": "pytest"},
                },
                "client": {
                    "deviceId": "xspace-desktop-main",
                    "clientMessageId": "client-msg-1",
                    "capabilities": {"realtime": True},
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] is True
        assert body["transport"] == "websocket"
        assert body["websocketUrl"] == f"/ws/avatar/v1/threads/{body['threadId']}"
        assert body["runId"].startswith(f"avatar-{body['threadId']}-")
        assert body["serverTime"]

        import time

        time.sleep(0.1)
        assert fake_tm.cells[body["threadId"]].bridge.calls == [
            {
                "text": "hello avatar",
                "reasoning_effort": "medium",
                "attachments": None,
            }
        ]

        existing = anonymous.post(
            "/api/avatar/v1/chat",
            json={
                "threadId": body["threadId"],
                "presetId": None,
                "cwd": None,
                "message": {
                    "text": "continue avatar",
                    "reasoningEffort": "low",
                    "attachments": None,
                },
                "client": {
                    "deviceId": "xspace-desktop-main",
                    "clientMessageId": "client-msg-2",
                },
            },
        )
        assert existing.status_code == 200, existing.text
        assert existing.json()["threadId"] == body["threadId"]
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_rest_chat_queues_when_thread_has_active_run(tmp_path: Path) -> None:
    """验证 Avatar REST chat 复用 ThreadManager active-run gate。"""
    fake_tm = _AvatarChatFakeTM()
    authed = _authed_client(tmp_path, thread_manager=fake_tm)
    anonymous = _anonymous_client(authed.app)
    try:
        meta = authed.portal.call(fake_tm.create_thread, "running", "local-default")
        cell = authed.portal.call(fake_tm.boot_or_attach, meta.id)
        original_task = object()
        cell.current_run_task = original_task

        response = anonymous.post(
            "/api/avatar/v1/chat",
            json={
                "threadId": meta.id,
                "presetId": None,
                "cwd": None,
                "message": {
                    "text": "avatar while web run is active",
                    "reasoningEffort": "low",
                    "attachments": None,
                },
                "client": {
                    "deviceId": "xspace-desktop-main",
                    "clientMessageId": "client-msg-active-run",
                },
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["threadId"] == meta.id
        assert cell.current_run_task is original_task
        assert cell.bridge.calls == []
        assert fake_tm.pending_avatar_inputs[meta.id][0]["text"] == (
            "avatar while web run is active"
        )
        assert (
            fake_tm.pending_avatar_inputs[meta.id][0]["avatar_run_id"] == response.json()["runId"]
        )
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_register_allows_pass_through_auth_during_v1_bringup(tmp_path: Path) -> None:
    """验证 Avatar v1 联调期 debug register 直接放行鉴权。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        response = anonymous.post(
            "/api/avatar/v1/messages",
            json={"source": "debug", "title": "Pass-through register"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["source"] == "debug"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_ack_rejects_empty_consumer_id(tmp_path: Path) -> None:
    """验证单条和批量 ack 的空 consumerId 在请求校验阶段被拒绝。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        message_id = _register_via_manager(authed, title="Consumer boundary")
        ack_token = _issue_device_token(
            authed,
            device_id="avatar-empty-consumer",
            scopes=["avatar.ack"],
        )

        single = anonymous.post(
            f"/api/avatar/v1/messages/{message_id}/ack",
            json={"status": "consumed", "consumerId": ""},
            headers={"Authorization": f"Bearer {ack_token}"},
        )
        assert single.status_code == 422

        batch = anonymous.post(
            "/api/avatar/v1/messages/ack",
            json={"messageIds": [message_id], "status": "consumed", "consumerId": ""},
            headers={"Authorization": f"Bearer {ack_token}"},
        )
        assert batch.status_code == 422
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_list_rejects_too_many_filter_values(tmp_path: Path) -> None:
    """验证超大过滤数组返回稳定 avatar_invalid_filter。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        read_token = _issue_device_token(
            authed,
            device_id="avatar-large-filter",
            scopes=["avatar.read"],
        )
        params = [("source", f"source-{idx}") for idx in range(51)]
        response = anonymous.get(
            "/api/avatar/v1/messages",
            params=params,
            headers={"Authorization": f"Bearer {read_token}"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "avatar_invalid_filter"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_list_rejects_huge_cursor(tmp_path: Path) -> None:
    """验证超出 SQLite integer 范围的 cursor 返回稳定错误。"""
    authed = _authed_client(tmp_path)
    anonymous = _anonymous_client(authed.app)
    try:
        read_token = _issue_device_token(
            authed,
            device_id="avatar-huge-cursor",
            scopes=["avatar.read"],
        )
        response = anonymous.get(
            "/api/avatar/v1/messages",
            params={"cursor": str(2**63)},
            headers={"Authorization": f"Bearer {read_token}"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "avatar_invalid_cursor"
    finally:
        anonymous.close()
        authed.__exit__(None, None, None)


def test_avatar_register_rejects_empty_dedupe_key(tmp_path: Path) -> None:
    """验证空 dedupeKey 在请求校验阶段被拒绝。"""
    authed = _authed_client(tmp_path)
    try:
        response = authed.post(
            "/api/avatar/v1/messages",
            json={"source": "debug", "title": "Empty dedupe", "dedupeKey": ""},
            headers=CSRF_HEADERS,
        )

        assert response.status_code == 422
    finally:
        authed.__exit__(None, None, None)
