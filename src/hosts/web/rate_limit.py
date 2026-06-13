"""登录限流（v0.1.5 web 鉴权）。

Per-IP 失败计数器；连续 5 次失败锁定 5 分钟。

设计要点：

- **in-memory dict + 单 asyncio.Lock**：v0.1.5 单用户场景，IP 数量小（<1000），
  全局锁开销可忽略。重启即清空（接受语义）。
- **time_provider 依赖注入**：测试可注入 fake clock 快进时间，无需真 sleep。
- **不引外部依赖**：自写 ~80 行替代 ``slowapi``，可测试性好。
- **不在本模块抛 HTTP 错误**：只抛 :class:`RateLimitedError`，由路由层翻译成
  HTTP 429。

锁定语义：

- 第 N+1 次失败时（N = max_failures）锁定，``locked_until = now + lockout_seconds``。
- ``check`` 仅当 ``now < locked_until`` 时抛；过期后第一次 ``check`` 通过 +
  失败计数被惰性清空（见 ``record_failure``）。
- ``record_success`` 把该 IP 条目整个删掉。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass


class RateLimitedError(Exception):
    """登录限流命中：当前 IP 在锁定期内。

    Attributes:
        retry_after_seconds: 距离解锁还有多少秒（向上取整 ≥ 1）。
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class LoginAttempt:
    """单 IP 的失败状态。

    Attributes:
        failed_count: 累计失败次数。
        locked_until: Unix 秒；0 表示未锁定。
    """

    failed_count: int = 0
    locked_until: float = 0.0


class LoginRateLimiter:
    """登录限流器（per-IP）。

    线程安全：所有公开方法都在 :class:`asyncio.Lock` 保护下；同一进程内多
    个 worker / coroutine 调用时数据一致。

    Attributes:
        _max_failures: 触发锁定的失败次数阈值。
        _lockout_seconds: 锁定时长。
        _time_provider: 时钟函数（默认 :func:`time.time`）。
        _attempts: ``ip → LoginAttempt`` 字典。
        _lock: 全局锁。
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        lockout_seconds: int = 300,
        time_provider: Callable[[], float] = time.time,
    ) -> None:
        if max_failures <= 0:
            raise ValueError("max_failures must be > 0")
        if lockout_seconds <= 0:
            raise ValueError("lockout_seconds must be > 0")
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._time_provider = time_provider
        self._attempts: dict[str, LoginAttempt] = {}
        self._lock = asyncio.Lock()

    async def check(self, ip: str) -> None:
        """限流检查；锁定中抛 :class:`RateLimitedError`。

        过期的锁定不会抛 —— 第一次 check 路径上感知到 ``locked_until <= now``
        时不主动清条目（清空交给后续 ``record_failure`` / ``record_success``
        幂等处理）。
        """
        async with self._lock:
            attempt = self._attempts.get(ip)
            if attempt is None:
                return
            now = self._time_provider()
            if attempt.locked_until > now:
                retry = max(1, int(attempt.locked_until - now + 0.999))
                raise RateLimitedError(retry_after_seconds=retry)

    async def record_failure(self, ip: str) -> None:
        """记录一次失败。

        语义：

        - 锁定期已过 → 重置 failed_count = 1（不复用旧计数）
        - 锁定期内 → 计数继续加（理论上调用方应已被 check 挡住，这里防御）
        - 触达阈值 → 设置 locked_until = now + lockout_seconds
        """
        async with self._lock:
            now = self._time_provider()
            attempt = self._attempts.get(ip)
            if attempt is None or (attempt.locked_until > 0 and attempt.locked_until <= now):
                # 新条目 / 锁已过期 → 重新开始计数
                attempt = LoginAttempt(failed_count=0, locked_until=0.0)
                self._attempts[ip] = attempt
            attempt.failed_count += 1
            if attempt.failed_count >= self._max_failures:
                attempt.locked_until = now + self._lockout_seconds

    async def record_success(self, ip: str) -> None:
        """记录一次成功；清除该 IP 的条目。"""
        async with self._lock:
            self._attempts.pop(ip, None)

    # ------------------------------------------------------------------
    # 测试 / 诊断辅助
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, LoginAttempt]:
        """返回当前 attempts dict 的浅拷贝（仅供测试断言用）。"""
        async with self._lock:
            return {
                ip: LoginAttempt(a.failed_count, a.locked_until) for ip, a in self._attempts.items()
            }

    @property
    def max_failures(self) -> int:
        return self._max_failures

    @property
    def lockout_seconds(self) -> int:
        return self._lockout_seconds


__all__ = [
    "LoginAttempt",
    "LoginRateLimiter",
    "RateLimitedError",
]
