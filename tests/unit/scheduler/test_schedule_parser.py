"""unit：scheduler.schedule_parser 解析覆盖（v0.2 模块 2）。

覆盖矩阵：
- duration 一次性（5 case）：秒/分/时/日 + 0s 边界
- every 周期（5 case）：秒级走 SECONDS / 分钟以上走 INTERVAL
- cron（4 case）：5 字段、6 字段、不规则 token、字段数错
- ISO（3 case）：含 offset / 仅日期 / 非法
- 不识别 / 空串（3 case）

测试用 ``parse_iso`` 反解 ONCE 路径 expr 跟 utc_now() 的距离做断言（≤2s 容差）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler.domain import ScheduleTrigger, TriggerType
from scheduler.schedule_parser import parse_schedule
from scheduler.timing import parse_iso, utc_now

# ---------------------------------------------------------------------------
# duration 一次性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_seconds"),
    [
        ("10s", 10),
        ("30m", 30 * 60),
        ("2h", 2 * 3600),
        ("1d", 86400),
        ("0s", 0),
    ],
)
def test_duration_once(text: str, expected_seconds: int) -> None:
    before = utc_now()
    trigger = parse_schedule(text)
    after = utc_now()

    assert trigger.trigger_type is TriggerType.ONCE
    run_at = parse_iso(trigger.expr)
    delta = (run_at - before).total_seconds()
    upper = (after + timedelta(seconds=expected_seconds + 2) - before).total_seconds()
    # run_at 必须 >= now + expected_seconds（精确折算，可能略晚一点）
    assert delta >= expected_seconds
    # 但不能超过 expected_seconds + 一点 wall-clock 抖动
    assert delta <= upper


def test_duration_default_timezone_is_utc() -> None:
    trigger = parse_schedule("10s")
    assert trigger.timezone == "UTC"


def test_duration_custom_timezone() -> None:
    trigger = parse_schedule("10s", default_tz="Asia/Shanghai")
    assert trigger.timezone == "Asia/Shanghai"


def test_duration_long_form_units() -> None:
    """支持长形式单位（minutes / hours / seconds 等）。"""
    trigger = parse_schedule("5 minutes")
    assert trigger.trigger_type is TriggerType.ONCE
    run_at = parse_iso(trigger.expr)
    delta = (run_at - utc_now()).total_seconds()
    assert 4 * 60 < delta <= 5 * 60 + 2


# ---------------------------------------------------------------------------
# every 周期
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_expr"),
    [
        ("every 10s", TriggerType.SECONDS, "10"),
        ("every 30s", TriggerType.SECONDS, "30"),
        ("every 1m", TriggerType.INTERVAL, "1"),
        ("every 2h", TriggerType.INTERVAL, "120"),
        ("every 1d", TriggerType.INTERVAL, "1440"),
    ],
)
def test_every_recurring(text: str, expected_type: TriggerType, expected_expr: str) -> None:
    trigger = parse_schedule(text)
    assert trigger.trigger_type is expected_type
    assert trigger.expr == expected_expr
    assert trigger.timezone == "UTC"


def test_every_case_insensitive() -> None:
    """every 关键字大小写无关。"""
    trigger = parse_schedule("EVERY 30s")
    assert trigger.trigger_type is TriggerType.SECONDS
    assert trigger.expr == "30"


def test_every_zero_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        parse_schedule("every 0s")


def test_every_invalid_duration() -> None:
    with pytest.raises(ValueError, match="invalid 'every' duration"):
        parse_schedule("every foobar")


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "0 9 * * *",
        "*/5 * * * *",
        "15 0,12 * * 1-5",
    ],
)
def test_cron_5_field(expr: str) -> None:
    trigger = parse_schedule(expr)
    assert trigger.trigger_type is TriggerType.CRON
    assert trigger.expr == expr
    assert trigger.timezone == "UTC"


def test_cron_6_field_with_seconds() -> None:
    expr = "*/30 * * * * *"
    trigger = parse_schedule(expr)
    assert trigger.trigger_type is TriggerType.CRON
    assert trigger.expr == expr


def test_cron_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid cron expression"):
        parse_schedule("x y z * *")


def test_cron_field_count_mismatch_falls_through() -> None:
    """3 字段不算 cron，也不是 duration / every / ISO → 不识别。"""
    with pytest.raises(ValueError, match="unrecognized schedule"):
        parse_schedule("*/5 * *")


# ---------------------------------------------------------------------------
# ISO8601
# ---------------------------------------------------------------------------


def test_iso_with_offset() -> None:
    trigger = parse_schedule("2026-05-03T09:00:00+08:00")
    assert trigger.trigger_type is TriggerType.ONCE
    assert trigger.timezone == "UTC"  # default_tz；ISO 文本里 offset 已记入 expr
    parsed = parse_iso(trigger.expr)
    assert parsed.utcoffset() == timedelta(hours=8)


def test_iso_with_z_suffix() -> None:
    trigger = parse_schedule("2026-05-03T09:00:00Z")
    assert trigger.trigger_type is TriggerType.ONCE
    parsed = parse_iso(trigger.expr)
    assert parsed.utcoffset() == timedelta(0)


def test_iso_date_only() -> None:
    """仅日期形式 fromisoformat 接受为 00:00:00（naive）。"""
    trigger = parse_schedule("2026-05-03")
    assert trigger.trigger_type is TriggerType.ONCE
    # naive → to_iso() 补本地 tz；解析回来必然 timezone-aware
    parsed = parse_iso(trigger.expr)
    assert parsed.tzinfo is not None


def test_iso_invalid_month_raises() -> None:
    with pytest.raises(ValueError, match="invalid ISO8601"):
        parse_schedule("2026-13-99")


# ---------------------------------------------------------------------------
# 不识别 / 边界
# ---------------------------------------------------------------------------


def test_empty_string_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_schedule("")


def test_whitespace_only_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_schedule("   ")


def test_unrecognized_text_raises() -> None:
    with pytest.raises(ValueError, match="unrecognized schedule"):
        parse_schedule("foo bar")


def test_returns_schedule_trigger_instance() -> None:
    """所有成功路径都必须返回 ScheduleTrigger 实例（dataclass post_init 校验通过）。"""
    for text in ["10s", "every 30s", "every 5m", "0 9 * * *", "2026-05-03T09:00:00+08:00"]:
        trigger = parse_schedule(text)
        assert isinstance(trigger, ScheduleTrigger)
