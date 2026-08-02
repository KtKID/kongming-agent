"""scheduler v0.1 — 时间与窗口纯函数（无副作用，无第三方依赖）。

本模块对应 ``docs/agent-cron-module-v0.1/05-hermes-feature-tradeoffs.md`` §2
``ONESHOT_GRACE_SECONDS`` 与文档末尾 ``grace_seconds_for_period`` 算法，参考
``other/hermes-agent/cron/jobs.py:252`` 的 ``_compute_grace_seconds``。

模块职责：
- ``grace_seconds_for_period``: missed-run 容忍窗口
- ``is_within_oneshot_grace``: one-shot ``run_at`` 是否仍在 120s 宽限内
- ``is_stale_recurring``: recurring 任务是否已过窗口需快进
- ``parse_iso`` / ``to_iso`` / ``utc_now``: ISO8601 与 timezone-aware ``datetime`` 互转
- ``compute_first_run_at``: trigger → 第一次触发时刻 ISO8601（v0.2 P1）

除 ``compute_first_run_at`` 在 cron 分支引入 ``croniter`` 之外，所有函数均为
纯函数，不调 ``print`` / ``logging``，不做 IO。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scheduler.domain import (
    GRACE_MAX_SECONDS,
    GRACE_MIN_SECONDS,
    ONESHOT_GRACE_SECONDS,
    ScheduleTrigger,
    TriggerType,
)


def grace_seconds_for_period(period_seconds: int) -> int:
    """missed-run 容忍窗口：grace = clamp(period/2, GRACE_MIN, GRACE_MAX)。

    Hermes 算法（``_compute_grace_seconds``）效果：daily=2h、hourly=30m、10min=5m。
    任务越频繁、对"延迟跑"越敏感，grace 越小，快进越激进。

    ``period_seconds <= 0`` 时退化为 :data:`GRACE_MIN_SECONDS`。
    """
    if period_seconds <= 0:
        return GRACE_MIN_SECONDS
    half = period_seconds // 2
    if half < GRACE_MIN_SECONDS:
        return GRACE_MIN_SECONDS
    if half > GRACE_MAX_SECONDS:
        return GRACE_MAX_SECONDS
    return half


def is_within_oneshot_grace(run_at: datetime, now: datetime) -> bool:
    """one-shot ``run_at`` 比 ``now`` 早 ≤ ``ONESHOT_GRACE_SECONDS`` 仍可触发。

    ``run_at`` 在未来同样视为 due（``>= now - 0``），调用方再判断 ``< now``
    决定是否触发实际逻辑。
    """
    return run_at >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS)


def is_stale_recurring(scheduled_for: datetime, now: datetime, period_seconds: int) -> bool:
    """recurring 任务的 ``next_run_at`` 偏移超过 grace 视为 stale，应快进跳过。"""
    return (now - scheduled_for).total_seconds() > grace_seconds_for_period(period_seconds)


def parse_iso(value: str) -> datetime:
    """解析 ISO8601 字符串到 timezone-aware :class:`datetime`。

    兼容 ``Z`` 后缀（替换为 ``+00:00``）；无 tz 时假定为本地时区。
    """
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def to_iso(dt: datetime) -> str:
    """timezone-aware :class:`datetime` → ISO8601。无 tz 时先 ``astimezone()`` 补本地。"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def utc_now() -> datetime:
    """当前 UTC 时间（timezone-aware）。"""
    return datetime.now(UTC)


def compute_first_run_at(trigger: ScheduleTrigger, *, now: datetime | None = None) -> str:
    """给定 trigger 计算第一次触发的 ISO8601 时刻字符串。

    - ``ONCE``: 直接返回 ``trigger.expr``（已是 ISO8601 时间戳）
    - ``INTERVAL``: ``now + int(expr)`` 分钟（最小 1 分钟）
    - ``SECONDS``: ``now + int(expr)`` 秒（最小 1 秒）
    - ``CRON``: 用 :mod:`croniter` 算下一个触发时刻，**按 ``trigger.timezone``
      解释**；非法 IANA 名退回到 base 自身的时区（即 UTC）。5 字段 vs 6 字段
      cron 自动识别。

    本函数是 schedule_tool / store / runtime_factory 三处共享 first-run 算法
    的真源，避免漂移。

    Args:
        trigger: ScheduleTrigger 实例。
        now: 起点时刻；``None`` 时取 :func:`utc_now`。

    Returns:
        ISO8601 字符串（带时区）。

    Raises:
        ValueError: trigger 字段不合法（``expr`` 解析失败 / cron 表达式非法 /
            未知 ``trigger_type``）。
    """
    if now is None:
        now = utc_now()

    trigger_type = trigger.trigger_type

    if trigger_type is TriggerType.ONCE:
        # ONCE 的 expr 已是 ISO 时间戳；非法 ISO 让上游/dataclass 处理
        return trigger.expr

    return compute_next_run_at(trigger, after=now)


def compute_next_run_at(trigger: ScheduleTrigger, *, after: datetime) -> str:
    """给定 recurring trigger 和已消费触发点，计算下一匹配的 ISO8601 时刻。

    ``after`` 是逻辑时间锚点：正常 cron fire 传原 ``next_run_at``，让每日
    ``0 22 * * *`` 始终落在 22:00；stale 快进传当前时间，直接得到下一次未来
    匹配。手动试运行不调用本函数，因此不会改变正式 cron cursor。
    """
    trigger_type = trigger.trigger_type

    if trigger_type is TriggerType.ONCE:
        raise ValueError("ONCE trigger 没有下一次触发时刻")

    if trigger_type is TriggerType.SECONDS:
        try:
            seconds = max(int(trigger.expr.strip()), 1)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"SECONDS expr 不是正整数: {trigger.expr!r}") from exc
        return to_iso(after + timedelta(seconds=seconds))

    if trigger_type is TriggerType.INTERVAL:
        try:
            minutes = max(int(trigger.expr.strip()), 1)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"INTERVAL expr 不是正整数: {trigger.expr!r}") from exc
        return to_iso(after + timedelta(minutes=minutes))

    if trigger_type is TriggerType.CRON:
        from croniter import croniter  # type: ignore[import-untyped, unused-ignore]

        # 按 trigger.timezone 解释 cron 表达式；非法 IANA 名退回 UTC（base 自身 tz）
        tz: ZoneInfo | None
        try:
            tz = ZoneInfo(trigger.timezone) if trigger.timezone else None
        except (ZoneInfoNotFoundError, ValueError):
            tz = None

        base = after.astimezone(tz) if tz is not None else after
        expr = trigger.expr.strip()
        fields_count = len(expr.split())
        try:
            if fields_count == 6:
                it = croniter(expr, base, second_at_beginning=True)
            else:
                it = croniter(expr, base)
            next_dt = it.get_next(datetime)
        except Exception as exc:
            raise ValueError(f"cron 表达式非法 {trigger.expr!r}: {exc}") from exc

        # 防御：croniter 在某些版本下返回 naive datetime；补回基准时区
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=tz or base.tzinfo or UTC)
        return to_iso(next_dt)

    raise ValueError(f"unknown trigger_type: {trigger_type!r}")


__all__ = [
    "compute_first_run_at",
    "compute_next_run_at",
    "grace_seconds_for_period",
    "is_stale_recurring",
    "is_within_oneshot_grace",
    "parse_iso",
    "to_iso",
    "utc_now",
]
