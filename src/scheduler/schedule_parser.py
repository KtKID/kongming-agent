"""scheduler v0.2 — 调度表达式解析器。

把 LLM/用户输入的字符串解析成 ScheduleTrigger。

支持格式（按以下顺序匹配，先严后宽）：
1. ISO8601 时间戳：含 'T' 或 4 位年份开头 → ONCE
2. 'every Ns/m/h/d' 周期：every 10s → SECONDS / every 30m → INTERVAL
3. 'Ns/m/h/d' 一次性：10s → 计算 run_at = now + N → ONCE（落 ISO 时间戳）
4. cron 表达式：5 字段 → CRON / 6 字段（含秒）→ CRON（expr 原样保留）
5. 都不匹配 → ValueError("unrecognized schedule: ...")
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from croniter import croniter  # type: ignore[import-untyped, unused-ignore]

from scheduler.domain import ScheduleTrigger, TriggerType
from scheduler.timing import to_iso, utc_now

_DURATION_RE = re.compile(
    r"^(?P<num>\d+)\s*"
    r"(?P<unit>s|sec|secs|second|seconds"
    r"|m|min|mins|minute|minutes"
    r"|h|hr|hrs|hour|hours"
    r"|d|day|days)$",
    re.IGNORECASE,
)
_EVERY_RE = re.compile(r"^every\s+(.+)$", re.IGNORECASE)
_ISO_HINT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")


def parse_schedule(text: str, *, default_tz: str = "UTC") -> ScheduleTrigger:
    """解析调度字符串成 ScheduleTrigger。

    所有解析路径都尝试 strip + 大小写兼容（duration / every / cron）。

    边界：
    - 解析顺序固定，先 ISO → 再 every → 再 duration → 再 cron → 不识别
    - 不调真 LLM；不做 IO；只读 utc_now()
    """
    if not text or not text.strip():
        raise ValueError("schedule must be non-empty string")
    s = text.strip()

    # 1) ISO8601 时间戳（含 'T' 或 'YYYY-MM-DD' 开头）
    if _ISO_HINT_RE.match(s):
        try:
            normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid ISO8601 timestamp: {s!r}: {exc}") from exc
        return ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr=to_iso(dt),
            timezone=default_tz,
        )

    # 2) every Ns/m/h/d 周期
    every_match = _EVERY_RE.match(s)
    if every_match:
        return _parse_every_duration(every_match.group(1).strip(), default_tz)

    # 3) Ns/m/h/d 一次性 duration
    duration_match = _DURATION_RE.match(s)
    if duration_match:
        seconds = _duration_to_seconds(duration_match)
        run_at = utc_now() + timedelta(seconds=seconds)
        return ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr=to_iso(run_at),
            timezone=default_tz,
        )

    # 4) cron 表达式：5 字段 / 6 字段（首字段是秒）
    fields = s.split()
    if len(fields) in (5, 6):
        try:
            if len(fields) == 5:
                croniter(s)
            else:
                croniter(s, second_at_beginning=True)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron expression {s!r}: {exc}") from exc
        return ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr=s,
            timezone=default_tz,
        )

    raise ValueError(
        f"unrecognized schedule {text!r}: expected ISO8601 / 'every Ns/m/h/d' / "
        f"'Ns/m/h/d' / 5 or 6-field cron"
    )


def _parse_every_duration(duration: str, default_tz: str) -> ScheduleTrigger:
    """every 子句的 duration 解析：秒级走 SECONDS，分钟以上走 INTERVAL。"""
    m = _DURATION_RE.match(duration)
    if not m:
        raise ValueError(f"invalid 'every' duration: {duration!r}")
    seconds = _duration_to_seconds(m)
    if seconds <= 0:
        raise ValueError(f"'every' duration must be positive, got {duration!r}")
    if seconds < 60:
        # 秒级：走 SECONDS 类型
        return ScheduleTrigger(
            trigger_type=TriggerType.SECONDS,
            expr=str(seconds),
            timezone=default_tz,
        )
    # 分钟以上：走 INTERVAL 类型，expr 是分钟数
    minutes = seconds // 60
    return ScheduleTrigger(
        trigger_type=TriggerType.INTERVAL,
        expr=str(minutes),
        timezone=default_tz,
    )


def _duration_to_seconds(m: re.Match[str]) -> int:
    """把 duration regex match 折算成秒。

    单位首字母分支：
    - 's' / 'sec' / 'second' / 'seconds' → 秒
    - 'm' / 'min' / 'minute' / 'minutes' → 分钟
    - 'h' / 'hr' / 'hour' / 'hours' → 小时
    - 'd' / 'day' / 'days' → 天

    cron 的 month 不在这里处理（regex 不匹配 'mo' / 'month'）。
    """
    num = int(m.group("num"))
    unit = m.group("unit").lower()
    if unit.startswith("s"):
        return num
    if unit.startswith("m"):
        return num * 60
    if unit.startswith("h"):
        return num * 3600
    if unit.startswith("d"):
        return num * 86400
    raise ValueError(f"unknown duration unit: {unit}")


__all__ = ["parse_schedule"]
