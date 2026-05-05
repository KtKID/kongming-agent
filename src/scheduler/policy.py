"""scheduler v0.1 — concurrency_policy / misfire_policy 应用器（Wave D1，#18/#19）。

本模块对应 ``docs/agent-cron-module-v0.1/04-data-and-state.md`` §3.2 的策略字段
解释，把 task 装配期声明的 policy 在执行前置阶段落地：

- :func:`apply_concurrency_policy`: 由 Execution Bridge 在拉起 ``Runner.run()``
  之前调用，决定本次 due reservation 该 ``proceed`` / ``skip`` / ``replace``。
  ``replace`` 路径会**真正写入** Store（cancel 旧 RUNNING 行 + 写 audit），
  调用方拿到结果后正常 ``execute`` 即可，不需要再补充收尾。
- :func:`get_misfire_support` / :func:`assert_misfire_policy_supported`:
  v0.1 三种 misfire 策略均已落地（``skip`` / ``catch_up_once`` / ``fire_now``），
  全部在 ``Store.reserve_due_tasks`` 内按 ``task.policy.misfire_policy`` 分派；
  :func:`assert_misfire_policy_supported` 保留作为防御未来加入新枚举值的钩子。

模块边界：
- 仅依赖 :mod:`scheduler.domain` / :mod:`scheduler.store` / :mod:`scheduler.timing`
- 不引入第三方依赖、不调 logging
- 不直接装配 Runner / approval / event sink
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scheduler.domain import (
    ConcurrencyPolicy,
    MisfirePolicy,
    RunStatus,
    ScheduledTask,
)
from scheduler.store import Store

# ---------------------------------------------------------------------------
# concurrency_policy
# ---------------------------------------------------------------------------


ConcurrencyAction = Literal["proceed", "skip", "replace"]


@dataclass(frozen=True)
class ConcurrencyDecision:
    """一次 :func:`apply_concurrency_policy` 的输出。

    - ``action="proceed"``: 调用方按正常路径 ``execute``
    - ``action="skip"``:    forbid 策略命中已有 RUNNING；调用方应放弃本轮，
      不应再启动 Runner，可合成一行 ``CANCELLED`` 记录
    - ``action="replace"``: replace 策略已经把旧 RUNNING 行收尾为 ``CANCELLED``
      并写入 audit；调用方继续启动新 Runner
    """

    action: ConcurrencyAction
    cancelled_run_id: str | None
    reason: str | None


def apply_concurrency_policy(
    *,
    task: ScheduledTask,
    store: Store,
) -> ConcurrencyDecision:
    """根据 ``task.policy.concurrency_policy`` 决定本次 execute 的行为。

    语义：

    - :attr:`ConcurrencyPolicy.ALLOW`: 直接 ``proceed``，不查 Store
    - :attr:`ConcurrencyPolicy.FORBID`: 已有 latest run 处于 RUNNING → ``skip``，
      ``reason`` 含旧 run_id；否则 ``proceed``
    - :attr:`ConcurrencyPolicy.REPLACE`: 已有 latest run 处于 RUNNING →
      调用 :meth:`Store.cancel_run` 把旧行收尾为 ``CANCELLED``，
      写 audit ``run_cancelled_by_replace``，返回 ``replace`` +
      ``cancelled_run_id``；否则 ``proceed``

    本函数是 Execution Bridge 唯一允许"装配前写入 Store"的钩子；调用方拿到
    决策后即可放心 ``execute``，不需要再判二次状态。
    """
    policy = task.policy.concurrency_policy
    if policy is ConcurrencyPolicy.ALLOW:
        return ConcurrencyDecision(action="proceed", cancelled_run_id=None, reason=None)

    latest = store.get_latest_run(task.task_id)
    if latest is None or latest.status is not RunStatus.RUNNING:
        return ConcurrencyDecision(action="proceed", cancelled_run_id=None, reason=None)

    if policy is ConcurrencyPolicy.FORBID:
        return ConcurrencyDecision(
            action="skip",
            cancelled_run_id=None,
            reason=f"forbid: another run {latest.run_id} is already running",
        )

    if policy is ConcurrencyPolicy.REPLACE:
        store.cancel_run(
            run_id=latest.run_id,
            task_id=task.task_id,
            failure_reason=None,
            error_message="replaced by new run",
        )
        store.append_audit(
            action="run_cancelled_by_replace",
            task_id=task.task_id,
            actor="scheduler",
            payload={"cancelled_run_id": latest.run_id},
        )
        return ConcurrencyDecision(
            action="replace",
            cancelled_run_id=latest.run_id,
            reason=f"replace: cancelled previous run {latest.run_id}",
        )

    # 防御式：枚举之外的取值（例如绕过 frozen dataclass 注入的非法值）
    raise ValueError(f"unknown concurrency_policy: {policy!r}")


# ---------------------------------------------------------------------------
# misfire_policy（v0.1 三策略全部落地）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MisfireSupportInfo:
    """:func:`get_misfire_support` 返回值。"""

    policy: MisfirePolicy
    supported_in_v01: bool
    note: str


_MISFIRE_NOTES: dict[MisfirePolicy, str] = {
    MisfirePolicy.SKIP: "store.reserve_due_tasks 自动快进 stale 任务，不补跑",
    MisfirePolicy.CATCH_UP_ONCE: (
        "stale 任务允许补跑 1 次，scheduled_for 保留原 next_run_at；"
        "next_run_at 同时推进，避免下轮重复补跑"
    ),
    MisfirePolicy.FIRE_NOW: (
        "stale 任务立即触发 1 次，scheduled_for 用 now；next_run_at 推进到 now+period"
    ),
}


def get_misfire_support(policy: MisfirePolicy) -> MisfireSupportInfo:
    """查询某个 :class:`MisfirePolicy` 在 v0.1 的落地状况。

    v0.1 三策略 (:attr:`MisfirePolicy.SKIP` / :attr:`MisfirePolicy.CATCH_UP_ONCE`
    / :attr:`MisfirePolicy.FIRE_NOW`) 均已落地，全部在
    :meth:`Store.reserve_due_tasks` 内按 ``task.policy.misfire_policy`` 分派。
    """
    note = _MISFIRE_NOTES.get(policy, f"unknown misfire_policy: {policy!r}")
    return MisfireSupportInfo(
        policy=policy,
        supported_in_v01=policy in _MISFIRE_NOTES,
        note=note,
    )


def assert_misfire_policy_supported(policy: MisfirePolicy) -> None:
    """task 装配期防御钩子：v0.1 三策略均已支持，本函数仅在未来扩枚举值时拦截。

    引擎 / CLI / Web 在 ``ScheduledTask`` 落盘前调一次本函数即可；当前不会抛
    :class:`NotImplementedError`，但保留接口避免装配点未来再做兼容判断。
    """
    info = get_misfire_support(policy)
    if not info.supported_in_v01:
        raise NotImplementedError(f"misfire_policy={policy.value} 未在 v0.1 支持；{info.note}")


__all__ = [
    "ConcurrencyAction",
    "ConcurrencyDecision",
    "MisfireSupportInfo",
    "apply_concurrency_policy",
    "assert_misfire_policy_supported",
    "get_misfire_support",
]
