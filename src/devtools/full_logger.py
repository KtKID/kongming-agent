"""FullLogger —— 开发期前后端通信全量日志（full-log-v0.1 阶段 1）。

设计真源：``docs/full-log-v0.1/README.md``。

## 核心不变量

- ``FullLogger.log()`` **永不阻塞调用方**：队列用 ``asyncio.Queue.put_nowait``，
  满则 ``popleft`` 丢最旧 + 一次性 stderr warning。
- ``enabled=False`` 时 ``log()`` / ``start()`` / ``aclose()`` 都是 no-op，
  对 event loop 零影响。
- 写入只走单条 writer task，避免 JSONL 行交错；行内 ``ensure_ascii=False`` 保中文可读。
- writer task 异常崩溃时，emit stderr warning 并自动重启（启动新 task 替换）；
  不让 logger 静默故障。
- 写盘失败（路径不可写）时，emit stderr warning 并把 ``enabled`` 降级为 ``False``，
  不抛异常，让业务侧无感降级。
- 时间戳走 UTC+8 固定偏移（CLAUDE.md 规则），ISO8601 毫秒精度
  形如 ``2026-05-26T22:33:21.123+08:00``。

## 模块级单例

通过 ``init_full_logger(config)`` 装配；``get_full_logger()`` 在任何地方拿到当前实例。
未 init 时 ``get_full_logger()`` 返回一个 ``enabled=False`` 的哑实例，
避免使用方崩溃。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

# CLAUDE.md 明确规则：UTC+8 固定偏移，不用系统 TZ / astimezone()。
CN_TZ = timezone(timedelta(hours=8))

LogDir = Literal["c2s", "s2c", "http_req", "http_resp"]


class _FullLogConfigLike(Protocol):
    """鸭子类型协议 —— ``WebFullLogConfig`` 实例或任意有相同字段的对象都行。

    阶段 1 ``WebFullLogConfig`` 还没并到 ``infrastructure.config``（并行任务在做），
    用 Protocol 避免硬依赖。
    """

    enabled: bool
    path: str
    queue_size: int
    rotate_daily: bool


def _now_cn_iso() -> str:
    """返回 UTC+8 时区的 ISO8601 时间戳，毫秒精度。

    形如 ``2026-05-26T22:33:21.123+08:00``。**必须**用固定偏移 ``CN_TZ``，
    不用 ``datetime.now()`` 默认行为（依赖系统 TZ 不可靠，CLAUDE.md 规则）。
    """
    return datetime.now(CN_TZ).isoformat(timespec="milliseconds")


def _json_default(o: Any) -> Any:
    """``json.dumps`` 兜底序列化器，防止 payload 含奇怪对象时 dump 抛异常。"""
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, (bytes, bytearray)):
        try:
            return o.decode("utf-8")
        except UnicodeDecodeError:
            return o.hex()
    return repr(o)


class FullLogger:
    """异步全量通信日志 Manager。

    与 ``JsonlTraceSink`` 同一种写盘模式（``asyncio.to_thread`` + ensure_ascii=False），
    但行内格式不同 —— ``FullLogger`` 写 ``{ts, dir, channel, thread_id, client_id, payload}``，
    payload 是组装后的 frame（``frame.model_dump()``）或 REST 元数据，不是
    ``Event`` 内核结构。

    ## 生命周期

    1. ``__init__``：纯参数登记，不做任何 I/O，不启动 task。
    2. ``start()``：启动后台 writer task；幂等（多次调用 = 一次）；``enabled=False`` 时 no-op。
    3. ``log()``：把记录入队；队满丢最旧 + 一次性 warning；永不抛 / 永不阻塞。
    4. ``aclose()``：发送 sentinel → writer 消费完剩余队列 → task 退出。

    ## 自愈

    writer task 异常时（如 ``json.dumps`` 撞到无法序列化的怪物对象、磁盘满），
    捕获后 stderr warning + 启动新 task 替换；不让 logger 静默死掉。
    持续写盘失败（``OSError``）时降级为 ``enabled=False``，停止入队，业务无感。

    ## rotate_daily（阶段 2 #23 TODO）

    阶段 1 接收 ``rotate_daily`` 参数但不实现真正切日逻辑，
    阶段 2 再加按 UTC+8 日历切到 ``full_log.<YYYY-MM-DD>.jsonl`` 的能力。
    """

    # sentinel 对象，aclose() 时塞进队列让 writer 收到后退出
    _CLOSE_SENTINEL: object = object()

    def __init__(
        self,
        *,
        enabled: bool,
        path: str,
        queue_size: int = 10000,
        rotate_daily: bool = True,
    ) -> None:
        self._enabled = bool(enabled)
        # 相对 cwd 解析，启动时 mkdir 父目录。
        self._path: Path = Path(path).resolve()
        self._queue_size = max(1, int(queue_size))
        # TODO 阶段 2 #23：实现按 UTC+8 日历切日 ``full_log.<YYYY-MM-DD>.jsonl``。
        self._rotate_daily = bool(rotate_daily)
        # asyncio.Queue 在没有运行 loop 时不能构造（3.10+），延迟到 start() 里建。
        self._queue: asyncio.Queue[Any] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._overflow_warned = False
        self._write_failure_warned = False

    # ------------------------------------------------------------------
    # public properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """当前是否启用。运行期可能因写盘持续失败被降级为 ``False``。"""
        return self._enabled

    @property
    def path(self) -> Path:
        """JSONL 落盘路径（已 resolve 成绝对路径）。"""
        return self._path

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动后台 writer task。

        - ``enabled=False`` 或已 ``aclose()``：no-op。
        - 多次调用幂等（已起跑的 task 还活着就直接返回）。
        - 启动前 ``mkdir -p`` 父目录；mkdir 失败 → stderr warning + 降级 ``enabled=False``。
        """
        if not self._enabled or self._closed:
            return
        if self._started and self._writer_task is not None and not self._writer_task.done():
            return

        # 准备文件系统：父目录创建失败就直接降级。
        try:
            await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            self._handle_write_failure(f"mkdir parent failed: {exc!r}")
            return

        if self._queue is None:
            # asyncio.Queue 不直接支持 popleft 丢最旧，所以用 deque 自己实现，
            # 配合 asyncio.Event 通知 writer。
            self._queue = asyncio.Queue(maxsize=self._queue_size)

        self._started = True
        self._writer_task = asyncio.create_task(self._writer_loop(), name="full-logger-writer")

    async def log(
        self,
        dir: LogDir,
        channel: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        """把一条记录入队。**永不阻塞调用方**，**永不抛**。

        - ``enabled=False`` / 未 start：早返。
        - 队满：popleft 丢最旧 + 一次性 stderr warning，新记录入队。
        - 入队失败（不应该发生，保险起见捕获）：stderr warning，不抛。
        """
        if not self._enabled or self._queue is None or self._closed:
            return

        record: dict[str, Any] = {
            "ts": _now_cn_iso(),
            "dir": dir,
            "channel": channel,
            "thread_id": thread_id,
            "client_id": client_id,
            "payload": payload,
        }
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            # 丢最旧 + 一次 warning + 重试入队（不会再满，因为刚 pop 出一个）。
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                # ValueError: task_done() 比 get_nowait() 多调一次，忽略。
                pass
            if not self._overflow_warned:
                print(
                    f"[full_logger] queue full (size={self._queue_size}), "
                    "dropping oldest records; this warning only shows once",
                    file=sys.stderr,
                )
                self._overflow_warned = True
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover
                self._queue.put_nowait(record)
        except Exception as exc:  # pragma: no cover
            print(f"[full_logger] enqueue failed: {exc!r}", file=sys.stderr)

    async def aclose(self) -> None:
        """flush 剩余队列 + 停止 writer task。

        - 未 start 或已 close：no-op。
        - 给 writer 发送 sentinel，等它消费完整个队列后退出。
        - **5s 超时兜底**（P1 #1）：极端场景 writer task 因 loop 关闭等原因
          无法自愈 + 队列已满时，``put(sentinel)`` 会无限阻塞——5s 后强制退出，
          跟项目其他 ``aclose`` 系列（``ThreadManager.aclose_all`` 等）兜底语义一致。
        """
        if self._closed:
            return
        self._closed = True
        if not self._started or self._queue is None or self._writer_task is None:
            return

        try:
            await asyncio.wait_for(self._aclose_inner(), timeout=5.0)
        except TimeoutError:
            print(
                "[full_logger] aclose timed out after 5s; force exit "
                "(records in queue may be lost)",
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover
            print(f"[full_logger] aclose unexpected error: {exc!r}", file=sys.stderr)

    async def _aclose_inner(self) -> None:
        """aclose 内部步骤：put sentinel → await writer task drain。

        被 :meth:`aclose` 包在 ``asyncio.wait_for(..., timeout=5.0)`` 里调，
        超时由外层处理。本方法只走"正常 happy path"。
        """
        # 类型守护：调用方（aclose）已确认 _started=True 且 _queue/_writer_task 非 None。
        assert self._queue is not None
        assert self._writer_task is not None

        # 发送 sentinel：writer 拿到后 flush 完剩余记录，看到 sentinel 退出。
        try:
            await self._queue.put(self._CLOSE_SENTINEL)
        except Exception as exc:  # pragma: no cover
            print(f"[full_logger] aclose put sentinel failed: {exc!r}", file=sys.stderr)
            return

        try:
            await self._writer_task
        except Exception as exc:  # pragma: no cover
            print(f"[full_logger] writer task ended with error: {exc!r}", file=sys.stderr)

    # ------------------------------------------------------------------
    # internal: writer task
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """后台 writer 主循环。

        循环消费队列：每条记录 ``json.dumps`` → ``asyncio.to_thread`` 追加写盘。
        sentinel 触发退出。任何异常都重启自身（除了 ``CancelledError``）。
        """
        assert self._queue is not None
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is self._CLOSE_SENTINEL:
                        # close 信号；drain 完队列里 sentinel 之前剩余的不需要，因为 put 是 FIFO，
                        # sentinel 之前的早被消费了。直接退出。
                        return
                    await self._write_one(item)
                finally:
                    with contextlib.suppress(ValueError):  # pragma: no cover
                        self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # writer 自愈：报错 + 启动新 task 替换自己。
            print(
                f"[full_logger] writer task crashed: {exc!r}; restarting",
                file=sys.stderr,
            )
            # 启动新 task 替换 self._writer_task；旧 task（就是当前 coroutine）即将退出。
            # RuntimeError: event loop 已关 —— 没法重启，认了。
            with contextlib.suppress(RuntimeError):  # pragma: no cover
                self._writer_task = asyncio.create_task(
                    self._writer_loop(), name="full-logger-writer"
                )

    async def _write_one(self, record: dict[str, Any]) -> None:
        """序列化 + 落盘一条记录。失败时降级，不抛。

        P1 #3 早返：``_handle_write_failure`` 把 ``_enabled=False`` 后，
        队列里残留的记录不再尝试写盘（每条早返，避免反复触发 OSError 浪费 CPU）。
        writer 仍会消费完队列直到看到 sentinel，但每条记录都是 ~0 成本。
        """
        if not self._enabled:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=_json_default)
        except Exception as exc:
            # 序列化失败：emit warning，跳过这条；不影响后续记录。
            print(
                f"[full_logger] serialize failed (record dropped): {exc!r}",
                file=sys.stderr,
            )
            return

        try:
            await asyncio.to_thread(self._append_line_sync, line)
        except OSError as exc:
            self._handle_write_failure(f"write failed: {exc!r}")

    def _append_line_sync(self, line: str) -> None:
        """同步追加一行；通过 ``asyncio.to_thread`` 调起，不阻塞 event loop。"""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    def _handle_write_failure(self, reason: str) -> None:
        """持续写盘失败 → emit warning（只一次）+ 降级为 ``enabled=False``。"""
        if not self._write_failure_warned:
            print(
                f"[full_logger] disabling logger due to write failure: {reason}",
                file=sys.stderr,
            )
            self._write_failure_warned = True
        self._enabled = False


# =====================================================================
# 模块级单例
# =====================================================================

# 用 module-private 变量持单例。CLAUDE.md "拒绝散落"：所有访问统一走
# ``init_full_logger`` / ``get_full_logger``，不让外部模块直接摸这个变量。
_logger_instance: FullLogger | None = None


def init_full_logger(config: _FullLogConfigLike) -> FullLogger:
    """用配置创建并注册单例 ``FullLogger``。

    - ``config`` 是 ``WebFullLogConfig`` 或鸭子类型只要含
      ``enabled / path / queue_size / rotate_daily`` 字段都行。
    - 重复调用会**替换**当前单例（不报错），但调用方应自己确保不会乱替换。
    - 返回新建的 logger 实例，**不会**自动 ``await start()``——
      调用方按生命周期合适时机起跑。
    """
    global _logger_instance
    _logger_instance = FullLogger(
        enabled=config.enabled,
        path=config.path,
        queue_size=getattr(config, "queue_size", 10000),
        rotate_daily=getattr(config, "rotate_daily", True),
    )
    return _logger_instance


def get_full_logger() -> FullLogger:
    """返回当前单例；未 init 时返回 ``enabled=False`` 哑实例（不抛）。

    哑实例的 ``log()`` / ``start()`` / ``aclose()`` 都是 no-op，
    业务侧可以无脑 ``await get_full_logger().log(...)`` 而不必判空。
    """
    global _logger_instance
    if _logger_instance is None:
        # 哑实例：path 走个内存盘式的占位（永远不会真去写）；reset 单例不污染下次 init。
        _logger_instance = FullLogger(
            enabled=False,
            path=".kongming/logs/full_log.jsonl",  # 仅占位，enabled=False 不会写到这里
            queue_size=10000,
            rotate_daily=True,
        )
    return _logger_instance


def _reset_for_tests() -> None:
    """仅供单测调用，清空单例确保测试隔离。"""
    global _logger_instance
    _logger_instance = None


__all__ = [
    "FullLogger",
    "LogDir",
    "get_full_logger",
    "init_full_logger",
]
