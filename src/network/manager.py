"""NetworkManager v0.1 实现。

职责（spec ``docs/web-network-layer-v0.1/02-module-breakdown.md``）：

- 维护 ``conn_id → ConnectionInfo`` 映射 + ``conn_id → Heartbeat`` 映射
- 提供 ``register / unregister`` 处理连接生命周期
- 提供 ``handle_inbound`` 拦截 ping 帧走 heartbeat、其余帧返回 ``False``
  让上层路由业务 handler
- 提供 ``send`` 给业务侧统一发送出口

v0.1 仅接入 Claude 频道；其他频道（``generic`` / ``codex`` / ``thread-status`` /
``cron`` / ``scheduler`` / ``shell``）在 v0.2 才接入，**本版本不实现路由分支**。

并发安全：所有读写 ``_connections`` / ``_heartbeats`` 的入口都在 ``_lock``
保护下；``Heartbeat`` 实例 per-connection 独立，互不串扰（spec 02
``Heartbeat 设计`` 强调）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .heartbeat import Heartbeat, HeartbeatConfig, HeartbeatHooks
from .tools import make_connection_id, safe_send_json

# ----------------------------------------------------------------------------
# Heartbeat 诊断日志（独立文件，仅本包写）
#
# 写入路径：``.kongming/logs/heartbeat/heartbeat.log``（由 web/app.py lifespan
# 调 :func:`configure_heartbeat_log` 注入路径；不依赖业务模块）。
#
# 日志格式：``<ISO ts> [LEVEL] <event> conn_id=... [extra=...]``
# 用途：诊断"前端发了 ping 吗 / 后端识别了吗 / 回了 pong 吗 / 何时 register/unregister"。
# 关闭：删除 configure 调用 + 重启 server 即可（runtime 旁路设计，不影响功能）。
# ----------------------------------------------------------------------------

_heartbeat_logger = logging.getLogger("kongming.network.heartbeat")
_heartbeat_logger.setLevel(logging.DEBUG)
_heartbeat_logger.propagate = False  # 不污染 root logger / 不双写到主日志
_heartbeat_log_initialized = False


def configure_heartbeat_log(log_dir: Path) -> None:
    """初始化心跳诊断 FileHandler。

    幂等：多次调用静默忽略。失败（如目录无写权限）不抛——诊断日志缺失不应
    打挂正常服务。

    :param log_dir: 日志目录绝对路径，缺失会自动 ``mkdir(parents=True)``
    """
    global _heartbeat_log_initialized
    if _heartbeat_log_initialized:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "heartbeat.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        _heartbeat_logger.addHandler(handler)
        _heartbeat_log_initialized = True
        _heartbeat_logger.info("heartbeat diag log initialized at %s", log_file)
    except Exception as exc:
        # 用 stderr 兜底告知，但不抛——主服务不能被诊断日志失败连累
        import sys

        print(
            f"[network.manager] heartbeat log init failed: {exc!r}",
            file=sys.stderr,
        )


@dataclass
class ConnectionInfo:
    """一条已注册连接的元信息。

    :ivar conn_id: 全局唯一连接 ID（v0.1 用 ``uuid.uuid4().hex``）
    :ivar channel: 频道名（v0.1 仅 ``"claude"``）
    :ivar thread_id: 线程 ID；全局型频道传 ``None``
    :ivar websocket: 底层 FastAPI ``WebSocket`` 实例
    :ivar registered_at_ms: 注册时的服务端 wall-clock 毫秒时间戳
    """

    conn_id: str
    channel: str
    thread_id: str | None
    websocket: Any
    registered_at_ms: int


class _BoundHooks:
    """``HeartbeatHooks`` 的具体绑定实现（per-connection 实例）。

    本类按 ``HeartbeatHooks`` Protocol 实现两个回调：

    - ``is_open()`` —— 通过 ``websocket.client_state`` 或 ``application_state``
      推断是否仍可写；不同 starlette 版本字段不一致，这里采用宽容判定：
      只要能拿到 ``send_json`` 属性就当 open（真正的写失败会在
      ``safe_send_json`` 内 swallow 成 ``False``）
    - ``send_pong()`` —— 通过 ``safe_send_json`` 写 pong 帧；失败时不抛
      （NetworkManager 的 ``handle_inbound`` 会用同一 ws 再写一次，
      失败时调用方决定是否触发 unregister）

    本类不持有 ``conn_id`` 或 ``NetworkManager`` 引用，纯函数式 hooks
    设计——把 ws 闭包进来，让 Heartbeat 调 ``send_pong`` 时不需要查
    manager state，减少跨锁竞争。
    """

    def __init__(self, websocket: Any) -> None:
        self._ws: Any = websocket

    def is_open(self) -> bool:
        return hasattr(self._ws, "send_json")

    async def send_pong(self, frame: dict[str, Any]) -> None:
        # 走 safe_send_json 吞写失败异常；返回值这里不消费——manager
        # send/handle_inbound 路径会单独判定 unregister 时机。
        await safe_send_json(self._ws, frame)


class NetworkManager:
    """连接注册 + channel 路由 + lifecycle 的单例管理器。

    使用流程：

    1. 进程启动时调一次 :meth:`configure` 把 ``HeartbeatConfig`` 注入
       （来自 ``cfg.web.ws_heartbeat_*`` 真源）
    2. WS endpoint handshake 成功后调 :meth:`register` 拿 ``conn_id``
    3. 主循环里每条 inbound 帧先调 :meth:`handle_inbound`，
       若返回 ``True`` 说明已被本类拦下当心跳处理，跳过业务路由
    4. 业务侧发送走 :meth:`send`
    5. WS disconnect 时调 :meth:`unregister`
    """

    def __init__(self) -> None:
        self._connections: dict[str, ConnectionInfo] = {}
        self._heartbeats: dict[str, Heartbeat] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._config: HeartbeatConfig | None = None

    def configure(self, config: HeartbeatConfig) -> None:
        """注入 :class:`HeartbeatConfig`。

        必须在第一次 :meth:`register` 之前调用一次，否则 ``register`` 会抛
        ``RuntimeError``。允许多次调用覆盖（v0.1 不支持热更新已注册连接
        的 config，只影响后续新连接）。

        :param config: 来自 ``cfg.web.ws_heartbeat_*`` 三件套
        """
        self._config = config

    async def register(
        self,
        channel: str,
        websocket: Any,
        thread_id: str | None = None,
    ) -> str:
        """注册一条新连接，返回 ``conn_id``。

        :param channel: 频道名（v0.1 仅 ``"claude"``）
        :param websocket: 已 ``accept`` 的 FastAPI ``WebSocket``
        :param thread_id: 线程 ID；全局频道传 ``None``
        :returns: 新建的 ``conn_id``（uuid4 hex）
        :raises RuntimeError: 未调 :meth:`configure` 就 register
        """
        if self._config is None:
            raise RuntimeError("call configure() first")

        conn_id = make_connection_id()
        info = ConnectionInfo(
            conn_id=conn_id,
            channel=channel,
            thread_id=thread_id,
            websocket=websocket,
            registered_at_ms=int(time.time() * 1000),
        )
        hooks: HeartbeatHooks = _BoundHooks(websocket)
        heartbeat = Heartbeat(self._config, hooks)
        async with self._lock:
            self._connections[conn_id] = info
            self._heartbeats[conn_id] = heartbeat
        _heartbeat_logger.info(
            "register channel=%s conn_id=%s thread_id=%s total_conns=%d",
            channel,
            conn_id,
            thread_id,
            len(self._connections),
        )
        return conn_id

    async def unregister(self, conn_id: str) -> None:
        """注销一条连接：移除 ``_connections`` / ``_heartbeats`` 双映射。

        幂等：未知 ``conn_id`` 静默返回，不抛异常。
        """
        async with self._lock:
            had = conn_id in self._connections
            self._connections.pop(conn_id, None)
            self._heartbeats.pop(conn_id, None)
        _heartbeat_logger.info(
            "unregister conn_id=%s had=%s remaining=%d",
            conn_id,
            had,
            len(self._connections),
        )

    async def handle_inbound(self, conn_id: str, frame: dict[str, Any]) -> bool:
        """处理一条入站帧；若为心跳则拦下回 pong 并返回 ``True``。

        :param conn_id: 来源连接 ID
        :param frame: 已经被上层解析为 ``dict`` 的入站帧
        :returns: ``True`` 表示心跳帧已被本类处理（调用方应跳过业务路由）；
            ``False`` 表示非心跳帧，调用方按业务 schema 继续处理
        """
        # 读 heartbeat 句柄走锁；调用 handle_inbound_frame / send_json 时
        # 不持锁（避免长持有锁阻塞其他 register/unregister）。
        async with self._lock:
            heartbeat = self._heartbeats.get(conn_id)
            info = self._connections.get(conn_id)
        if heartbeat is None or info is None:
            _heartbeat_logger.warning(
                "handle_inbound conn_id=%s NOT_REGISTERED frame_kind=%s",
                conn_id,
                frame.get("kind") if isinstance(frame, dict) else "<not-dict>",
            )
            return False

        response = await heartbeat.handle_inbound_frame(frame)
        if response is None:
            # 非心跳帧不打日志（避免业务帧刷屏）
            return False

        # pong 写盘失败由 safe_send_json 吞掉；本路径不主动 unregister
        # （让 endpoint 主循环的 WebSocketDisconnect 走 finally 做收口，
        # 避免与正在 disconnect 的连接竞争）。
        send_ok = await safe_send_json(info.websocket, response)
        _heartbeat_logger.info(
            "ping->pong conn_id=%s client_ts=%s server_ts=%s send_ok=%s",
            conn_id,
            frame.get("ts") if isinstance(frame, dict) else None,
            response.get("timestamp_ms"),
            send_ok,
        )
        return True

    async def send(self, conn_id: str, frame: dict[str, Any]) -> bool:
        """向指定连接发送一条帧；连接不存在或写失败时清理映射。

        :param conn_id: 目标连接 ID
        :param frame: 序列化前的 dict 帧
        :returns: 写盘是否成功；失败时已自动 unregister
        """
        async with self._lock:
            info = self._connections.get(conn_id)
        if info is None:
            return False
        ok = await safe_send_json(info.websocket, frame)
        if not ok:
            await self.unregister(conn_id)
        return ok


_singleton: NetworkManager | None = None


def get_network_manager() -> NetworkManager:
    """模块级 lazy 单例工厂。

    首次调用时构造 ``NetworkManager``；后续调用直接复用。无锁实现：
    Python 模块导入有 GIL 串行化语义，单例首次构造在事件循环启动前
    完成；事件循环内的并发访问只读 ``_singleton`` 引用，不会撕裂。

    :returns: 进程级唯一的 ``NetworkManager`` 实例
    """
    global _singleton
    if _singleton is None:
        _singleton = NetworkManager()
    return _singleton


def reset_network_manager_for_test() -> None:
    """**仅供测试使用**：重置模块级单例。

    生产代码不应调用本函数；测试用例需要在 ``register`` 前重置以隔离不同
    test 之间的 manager state。
    """
    global _singleton
    _singleton = None
