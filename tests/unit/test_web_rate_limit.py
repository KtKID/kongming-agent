"""LoginRateLimiter 单测（v0.1.5）。

覆盖：

1. 5 失败触发锁定 + check 抛 RateLimitedError
2. 锁定到期后 check 通过 + record_failure 重置计数
3. 不同 IP 独立计数
4. record_success 清条目
5. time_provider 注入快进时间
"""

from __future__ import annotations

import pytest

from web.rate_limit import LoginRateLimiter, RateLimitedError


class FakeClock:
    """可注入的假时钟。"""

    def __init__(self, t0: float = 1000.0) -> None:
        self.now = t0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_first_failures_below_threshold_pass() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(
        max_failures=5,
        lockout_seconds=300,
        time_provider=clock,
    )

    for _ in range(4):
        await rl.check("1.2.3.4")
        await rl.record_failure("1.2.3.4")

    # 第 5 次 check 仍通过（还未触发锁定）
    await rl.check("1.2.3.4")


async def test_fifth_failure_locks_ip() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(
        max_failures=5,
        lockout_seconds=300,
        time_provider=clock,
    )

    for _ in range(5):
        await rl.record_failure("1.2.3.4")

    with pytest.raises(RateLimitedError) as exc_info:
        await rl.check("1.2.3.4")
    assert exc_info.value.retry_after_seconds <= 300
    assert exc_info.value.retry_after_seconds >= 1


async def test_lockout_expires_after_window() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(
        max_failures=3,
        lockout_seconds=60,
        time_provider=clock,
    )

    for _ in range(3):
        await rl.record_failure("1.2.3.4")

    # 锁定期内
    with pytest.raises(RateLimitedError):
        await rl.check("1.2.3.4")

    # 快进 60 + 1 秒
    clock.advance(61)

    # 锁过期 → check 不抛
    await rl.check("1.2.3.4")


async def test_record_failure_after_lock_expiry_resets_counter() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(
        max_failures=3,
        lockout_seconds=60,
        time_provider=clock,
    )

    for _ in range(3):
        await rl.record_failure("ip-a")

    # 第一次锁
    with pytest.raises(RateLimitedError):
        await rl.check("ip-a")

    clock.advance(61)
    # 锁过期，下一次 record_failure 应当从 1 开始计
    await rl.record_failure("ip-a")
    snap = await rl.snapshot()
    assert snap["ip-a"].failed_count == 1
    assert snap["ip-a"].locked_until == 0


async def test_different_ips_independent() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(
        max_failures=3,
        lockout_seconds=60,
        time_provider=clock,
    )

    for _ in range(3):
        await rl.record_failure("ip-a")

    # ip-a 锁了
    with pytest.raises(RateLimitedError):
        await rl.check("ip-a")
    # ip-b 不受影响
    await rl.check("ip-b")


async def test_record_success_clears_entry() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(time_provider=clock)

    await rl.record_failure("ip-a")
    await rl.record_failure("ip-a")
    await rl.record_success("ip-a")

    snap = await rl.snapshot()
    assert "ip-a" not in snap


async def test_invalid_ctor_args() -> None:
    with pytest.raises(ValueError):
        LoginRateLimiter(max_failures=0)
    with pytest.raises(ValueError):
        LoginRateLimiter(lockout_seconds=0)
