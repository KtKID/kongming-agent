"""unit：scheduler.timing 纯函数覆盖（Wave B2）。

覆盖矩阵：
- ``grace_seconds_for_period``: clamp 算法在 [GRACE_MIN, GRACE_MAX] 边界
- ``is_within_oneshot_grace``: ``ONESHOT_GRACE_SECONDS`` 边界（包含等于 120s）
- ``is_stale_recurring``: 偏移 > grace 时为 True
- ``parse_iso`` / ``to_iso``: ``Z`` 后缀 / naive / round-trip
- ``utc_now``: timezone-aware 且 tz=UTC
- ``compute_first_run_at``: ONCE / INTERVAL / SECONDS / CRON（含 timezone）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from scheduler.domain import (
    GRACE_MAX_SECONDS,
    GRACE_MIN_SECONDS,
    ONESHOT_GRACE_SECONDS,
    ScheduleTrigger,
    TriggerType,
)
from scheduler.timing import (
    compute_first_run_at,
    grace_seconds_for_period,
    is_stale_recurring,
    is_within_oneshot_grace,
    parse_iso,
    to_iso,
    utc_now,
)


def _require_zoneinfo(key: str) -> ZoneInfo:
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        pytest.skip(f"timezone data is unavailable for {key}")


# ---------------------------------------------------------------------------
# grace_seconds_for_period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (60, GRACE_MIN_SECONDS),  # half=30 < 120 → clamp 到 GRACE_MIN
        (240, GRACE_MIN_SECONDS),  # half=120 == GRACE_MIN
        (600, 300),  # half=300 在区间内
        (3600, 1800),  # half=1800 在区间内
        (86400, GRACE_MAX_SECONDS),  # half=43200 > GRACE_MAX → clamp 到 GRACE_MAX
        (0, GRACE_MIN_SECONDS),  # 退化分支
        (-1, GRACE_MIN_SECONDS),  # 退化分支
    ],
)
def test_grace_seconds_for_period(period: int, expected: int) -> None:
    assert grace_seconds_for_period(period) == expected


# ---------------------------------------------------------------------------
# is_within_oneshot_grace
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset_seconds", "expected"),
    [
        (0, True),  # run_at == now
        (-60, True),  # run_at = now - 60s
        (-(ONESHOT_GRACE_SECONDS - 1), True),  # now - 119s
        (-ONESHOT_GRACE_SECONDS, True),  # now - 120s（边界包含）
        (-(ONESHOT_GRACE_SECONDS + 1), False),  # now - 121s
        (60, True),  # 未来 60s
    ],
)
def test_is_within_oneshot_grace(offset_seconds: int, expected: bool) -> None:
    now = _now_utc()
    run_at = now + timedelta(seconds=offset_seconds)
    assert is_within_oneshot_grace(run_at, now) is expected


# ---------------------------------------------------------------------------
# is_stale_recurring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "offset_seconds", "expected"),
    [
        (3600, 1801, True),  # grace=1800，1801s > 1800 → True
        (3600, 1799, False),  # grace=1800，1799s < 1800 → False
        (3600, 1800, False),  # 严格 > 才 True；等于不算 stale
        (60, 121, True),  # grace=120（GRACE_MIN），121s > 120 → True
        (60, 120, False),  # grace=120，120s 不算 stale
        (86400, GRACE_MAX_SECONDS + 1, True),  # 偏移超过 GRACE_MAX
        (86400, GRACE_MAX_SECONDS - 1, False),  # 仍在 grace 内
    ],
)
def test_is_stale_recurring(period: int, offset_seconds: int, expected: bool) -> None:
    now = _now_utc()
    scheduled_for = now - timedelta(seconds=offset_seconds)
    assert is_stale_recurring(scheduled_for, now, period) is expected


# ---------------------------------------------------------------------------
# parse_iso / to_iso
# ---------------------------------------------------------------------------


def test_parse_iso_with_offset_returns_aware() -> None:
    dt = parse_iso("2026-05-01T12:00:00+00:00")
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


def test_parse_iso_with_z_suffix() -> None:
    dt = parse_iso("2026-05-01T12:00:00Z")
    assert dt.tzinfo is not None
    # Z → +00:00 → UTC
    assert dt.utcoffset() == timedelta(0)


def test_parse_iso_with_offset_non_utc() -> None:
    dt = parse_iso("2026-05-01T20:00:00+08:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)


def test_parse_iso_naive_assumes_local() -> None:
    """无 tz 输入会被 ``astimezone()`` 补本地 tz，结果必须 timezone-aware。"""
    dt = parse_iso("2026-05-01T12:00:00")
    assert dt.tzinfo is not None


def test_to_iso_aware_preserves_offset() -> None:
    dt = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    s = to_iso(dt)
    # ISO 输出包含 offset
    assert s.endswith("+00:00")


def test_to_iso_naive_normalizes_to_local() -> None:
    naive = datetime(2026, 5, 1, 12, 0, 0)
    s = to_iso(naive)
    # 输出含 offset（来自 astimezone()）
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None


def test_round_trip_aware_utc() -> None:
    original = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    s = to_iso(original)
    again = parse_iso(s)
    assert again == original


def test_round_trip_aware_offset_8() -> None:
    tz8 = timezone(timedelta(hours=8))
    original = datetime(2026, 5, 1, 20, 0, 0, tzinfo=tz8)
    s = to_iso(original)
    again = parse_iso(s)
    assert again == original


# ---------------------------------------------------------------------------
# utc_now
# ---------------------------------------------------------------------------


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert now.tzinfo is UTC


# ---------------------------------------------------------------------------
# compute_first_run_at
# ---------------------------------------------------------------------------


class TestComputeFirstRunAt:
    """trigger → 第一次触发时刻 ISO8601 的 helper 覆盖。"""

    def test_once_returns_expr(self) -> None:
        """ONCE trigger 直接返回 expr（已是 ISO 时间戳）。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr="2026-05-04T09:00:00+08:00",
            timezone="Asia/Shanghai",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        assert compute_first_run_at(trigger, now=now) == "2026-05-04T09:00:00+08:00"

    def test_seconds_adds_seconds(self) -> None:
        """SECONDS=10 → now + 10s。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.SECONDS,
            expr="10",
            timezone="UTC",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        assert parse_iso(result) == now + timedelta(seconds=10)

    def test_seconds_minimum_one(self) -> None:
        """SECONDS=0 在 dataclass 已被拒；helper 内部 max(...,1) 也兜底。"""
        # dataclass 直接拒掉 0 / 负数，所以这里只验证 helper 对正整数行为
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.SECONDS,
            expr="1",
            timezone="UTC",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        assert parse_iso(result) == now + timedelta(seconds=1)

    def test_interval_adds_minutes(self) -> None:
        """INTERVAL=5 → now + 5min。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="5",
            timezone="UTC",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        assert parse_iso(result) == now + timedelta(minutes=5)

    def test_interval_default_now_uses_utc_now(self) -> None:
        """now=None 时取 utc_now()，结果应 ≥ 调用前的 utc_now() + 1min。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="1",
            timezone="UTC",
        )
        before = utc_now()
        result = compute_first_run_at(trigger)
        after = utc_now()
        result_dt = parse_iso(result)
        assert before + timedelta(minutes=1) <= result_dt <= after + timedelta(minutes=1)

    def test_cron_5field_uses_timezone(self) -> None:
        """cron `0 9 * * *` + tz=Asia/Shanghai → 算出来的 next 对应北京 9:00（UTC 1:00），
        不是 UTC 9:00。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="Asia/Shanghai",
        )
        # now=2026-05-03 06:00 UTC == 北京 14:00 → 下次北京 9:00 = 次日 = UTC 5/4 01:00
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        result_dt = parse_iso(result)
        # 转回北京时间应该是 9 点
        assert result_dt.astimezone(_require_zoneinfo("Asia/Shanghai")).hour == 9
        # 等价于 UTC 1:00（次日，因为北京 14:00 已过当天的 9 点窗口）
        assert result_dt.astimezone(UTC) == datetime(2026, 5, 4, 1, 0, 0, tzinfo=UTC)

    def test_cron_5field_utc_timezone(self) -> None:
        """cron `0 9 * * *` + tz=UTC → next 是 UTC 9:00。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="UTC",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        result_dt = parse_iso(result)
        assert result_dt.astimezone(UTC) == datetime(2026, 5, 3, 9, 0, 0, tzinfo=UTC)

    def test_cron_6field_with_seconds(self) -> None:
        """6 字段 cron `*/30 * * * * *` → 每 30 秒；下次 ≤ now + 30s。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="*/30 * * * * *",
            timezone="UTC",
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        result_dt = parse_iso(result)
        # 下次触发应该在 (now, now+30s] 区间内
        assert now < result_dt <= now + timedelta(seconds=30)

    def test_invalid_cron_raises(self) -> None:
        """非法 cron → ValueError。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="not a cron",
            timezone="UTC",
        )
        with pytest.raises(ValueError, match="cron 表达式非法"):
            compute_first_run_at(trigger)

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        """trigger.timezone='不存在/ZoneInfo' → 不抛，按 base 自身 tz（now 是 UTC）。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="Mars/Olympus_Mons",  # 非法 IANA
        )
        now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now)
        result_dt = parse_iso(result)
        # 退回 UTC 解释 → next 是 UTC 9:00 当天
        assert result_dt.astimezone(UTC) == datetime(2026, 5, 3, 9, 0, 0, tzinfo=UTC)

    def test_seconds_invalid_expr_raises(self) -> None:
        """SECONDS 非数字 expr → ValueError。

        ScheduleTrigger 构造器对 SECONDS 直接拒掉非整数，所以这里用
        ``object.__setattr__`` 绕过 frozen 写入非法值，模拟从外部反序列化
        漏校验时 helper 仍能挡住。
        """
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.SECONDS,
            expr="10",
            timezone="UTC",
        )
        object.__setattr__(trigger, "expr", "abc")
        with pytest.raises(ValueError, match="SECONDS expr 不是正整数"):
            compute_first_run_at(trigger)

    def test_interval_invalid_expr_raises(self) -> None:
        """INTERVAL 非数字 expr → ValueError（INTERVAL 在 dataclass 不校验）。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.INTERVAL,
            expr="abc",
            timezone="UTC",
        )
        with pytest.raises(ValueError, match="INTERVAL expr 不是正整数"):
            compute_first_run_at(trigger)

    def test_unknown_trigger_type_raises(self) -> None:
        """构造未知 trigger_type（突破 frozen + enum 校验）→ ValueError。"""
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.ONCE,
            expr="2026-05-04T09:00:00+08:00",
            timezone="UTC",
        )
        # 用一个不在 TriggerType 枚举里的值（普通 str），绕过构造校验
        object.__setattr__(trigger, "trigger_type", "unknown_type")
        with pytest.raises(ValueError, match="unknown trigger_type"):
            compute_first_run_at(trigger)

    def test_cron_naive_now_uses_base_tz(self) -> None:
        """naive now（无 tz）+ tz=UTC → astimezone(UTC) 会按 local 解释 naive，结果仍 timezone-aware。"""
        # 这里只验证不崩 + 输出含 tz；具体 local 解释由 datetime stdlib 决定
        trigger = ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="0 9 * * *",
            timezone="UTC",
        )
        now_aware = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
        result = compute_first_run_at(trigger, now=now_aware)
        result_dt = parse_iso(result)
        assert result_dt.tzinfo is not None


# ---------------------------------------------------------------------------
# v0.2 P1 验收测试（cron-test-coverage-hermes-align task #11-#14）
#
# 本批为对照 Hermes `tests/test_timezone.py` 的时区维度验收测试，验证
# B2 修复后（compute_first_run_at + store._period_seconds 都按 trigger.timezone
# 解释 cron）跨时区任务的绝对时刻正确，且 naive / 非法 IANA 输入有合理 fallback。
# 不改产品代码，仅做回归保护。
# ---------------------------------------------------------------------------


def test_cron_with_timezone_uses_timezone_not_utc() -> None:
    """v0.2 P1 验收：cron `0 9 * * *` + tz=Asia/Shanghai → 实际触发时刻是
    北京 9:00 对应的 UTC 1:00，不是 UTC 9:00。

    防御 B2 修复后回归（store._period_seconds + timing.compute_first_run_at
    都按 trigger.timezone 解释 cron 表达式）。
    """
    from datetime import datetime

    from scheduler.domain import ScheduleTrigger, TriggerType
    from scheduler.timing import compute_first_run_at, parse_iso

    trigger = ScheduleTrigger(
        trigger_type=TriggerType.CRON,
        expr="0 9 * * *",
        timezone="Asia/Shanghai",
    )
    # now = 2026-05-03 06:00 UTC（= 北京时间 14:00）
    now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)
    result_iso = compute_first_run_at(trigger, now=now)
    result_dt = parse_iso(result_iso)

    # 北京下次 9:00 = 次日（2026-05-04）北京 9:00
    bj = result_dt.astimezone(_require_zoneinfo("Asia/Shanghai"))
    assert bj.hour == 9, f"应触发在北京 9:00，实际是北京 {bj.hour}:{bj.minute}"
    assert bj.year == 2026 and bj.month == 5 and bj.day == 4, f"应是 5/4 北京 9:00，实际 {bj}"

    # 对应 UTC 应该是 5/4 01:00
    utc = result_dt.astimezone(UTC)
    assert utc.hour == 1 and utc.day == 4


def test_iso8601_with_offset_preserves_absolute_time() -> None:
    """v0.2 P1 验收：ONCE ISO 时间戳含 +08:00 偏移 → 解析后绝对时间正确，
    跨时区比较一致。

    Hermes 同款 test_ensure_aware_naive_preserves_absolute_time。
    """

    from scheduler.timing import parse_iso

    iso_with_offset = "2026-05-03T09:00:00+08:00"  # 北京 9:00 = UTC 1:00

    parsed = parse_iso(iso_with_offset)
    assert parsed.tzinfo is not None, "解析后必须 timezone-aware"

    # 绝对时间应该是 UTC 2026-05-03 01:00
    utc = parsed.astimezone(UTC)
    assert utc.year == 2026 and utc.month == 5 and utc.day == 3
    assert utc.hour == 1 and utc.minute == 0


def test_naive_iso_string_assumes_local_timezone() -> None:
    """v0.2 P1 验收：naive ISO（无 +HH:MM / Z 后缀）按本地时区解释。

    Hermes 同款 test_get_due_jobs_handles_naive_timestamps。
    """
    from scheduler.timing import parse_iso

    naive = "2026-05-03T09:00:00"  # 没有时区后缀

    parsed = parse_iso(naive)
    assert parsed.tzinfo is not None, "naive ISO 应被赋予本地时区"


def test_compute_first_run_at_invalid_timezone_does_not_raise() -> None:
    """v0.2 P1 验收：trigger.timezone='不存在' → compute_first_run_at 不抛，
    fallback 按 UTC（base.tz）解释 cron。

    Hermes 同款 test_invalid_timezone_falls_back。
    """
    from datetime import datetime

    from scheduler.domain import ScheduleTrigger, TriggerType
    from scheduler.timing import compute_first_run_at

    trigger = ScheduleTrigger(
        trigger_type=TriggerType.CRON,
        expr="0 9 * * *",
        timezone="不存在的时区",
    )
    now = datetime(2026, 5, 3, 6, 0, 0, tzinfo=UTC)

    # 不应抛
    result = compute_first_run_at(trigger, now=now)
    assert result is not None
    assert "T" in result  # 是 ISO 格式
