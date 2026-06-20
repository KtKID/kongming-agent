"""Avatar WebSocket channel 门户。

本脚本实现 AvatarChannelManager，把 `/ws/avatar/v1/threads/{thread_id}` 接入
现有 generic_chat WebSocket 分发链路。关键流程是校验 thread、boot cell、
推送 thread.history、注册网络连接、复用普通 C2S frame 解析与 dispatch。
关键函数职责：register_avatar_channel_routes 注册路由，AvatarChannelManager
处理单条连接生命周期。
"""

from __future__ import annotations

from fastapi import FastAPI, WebSocket

from hosts.web.websocket.routes import handle_thread_ws_channel


class AvatarChannelManager:
    """Avatar WS channel Manager。

    职责：管理 Avatar 独立 WS channel 的建连、history 下发和连接清理。
    关键输入：FastAPI WebSocket 与 thread_id。
    关键输出：连接期间复用普通 generic_chat S2C/C2S 帧。
    """

    async def handle(self, websocket: WebSocket, thread_id: str) -> None:
        """处理一条 Avatar WS 连接。

        关键输入：WebSocket 和 thread_id。
        关键输出：接受连接后进入普通 frame receive loop。
        """
        await handle_thread_ws_channel(
            websocket,
            thread_id,
            network_channel="avatar",
            require_cookie=False,
            allowed_backend_kind="generic_chat",
            include_evolution_replay=False,
        )


def register_avatar_channel_routes(app: FastAPI) -> None:
    """注册 Avatar WS channel 路由。

    关键输入：FastAPI app。
    关键输出：新增 `/ws/avatar/v1/threads/{thread_id}` endpoint。
    """
    manager = AvatarChannelManager()

    @app.websocket("/ws/avatar/v1/threads/{thread_id}")
    async def avatar_thread_ws(websocket: WebSocket, thread_id: str) -> None:
        await manager.handle(websocket, thread_id)


__all__ = ["AvatarChannelManager", "register_avatar_channel_routes"]
