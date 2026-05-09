"""scheduler v0.1 — Store 层（v0.1 文件存储）。

实现 ``docs/agent-cron-module-v0.1/04-data-and-state.md`` §4 描述的持久化骨架：

- ``<home>/cron/scheduled_tasks.json``    全量任务定义（原子覆盖）
- ``<home>/cron/runs/<task_id>.jsonl``    每任务 append-only 运行记录（含 superseded）
- ``<home>/cron/audits.jsonl``            全局 append-only 审计
- ``<home>/cron/.scheduler.lock``         进程级 file lock（fcntl / msvcrt）

边界约束（参考文档 §7.2）：
- 序列化 / 反序列化 :mod:`scheduler.domain` 的 dataclass
- 事务性更新 ``next_run_at`` / ``last_run_at`` / ``state``
- **不**直接调 ``Runner``、**不**解释审批策略
- 不依赖任何第三方库（``fcntl`` / ``msvcrt`` 来自标准库）

本文件不导出 logging，所有错误通过 ``raise`` 上抛由调用方处理。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from scheduler.domain import (
    GRACE_MIN_SECONDS,
    SCHEMA_VERSION,
    ConcurrencyPolicy,
    DeliveryChannel,
    DeliveryStatus,
    DueTaskReservation,
    MisfirePolicy,
    RunFailureReason,
    RunStatus,
    ScheduleDelivery,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    SessionMode,
    TaskExecutionPolicy,
    TaskOrigin,
    TaskState,
    TaskTarget,
    TriggerType,
)
from scheduler.timing import (
    grace_seconds_for_period,
    is_stale_recurring,
    is_within_oneshot_grace,
    parse_iso,
    to_iso,
    utc_now,
)

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SchedulerStoreError(Exception):
    """Store 层基类异常。"""


class SchedulerBusyError(SchedulerStoreError):
    """file lock 已被其他进程持有；上层决定 retry / 放弃。"""


class TaskNotFoundError(SchedulerStoreError):
    """指定 ``task_id`` 不存在。"""


# ---------------------------------------------------------------------------
# 平台 file lock 抽象（fcntl on Unix / msvcrt on Windows）
# ---------------------------------------------------------------------------


_IS_WINDOWS = sys.platform.startswith("win")


def _try_acquire(fd: int) -> bool:
    """非阻塞抢锁；成功 True，已被占用 False。"""
    if _IS_WINDOWS:  # pragma: no cover - 平台分支
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release(fd: int) -> None:
    """释放 file lock。"""
    if _IS_WINDOWS:  # pragma: no cover - 平台分支
        import msvcrt

        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 安全权限助手
# ---------------------------------------------------------------------------


def _secure_dir(path: Path) -> None:
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o700)


def _secure_file(path: Path) -> None:
    with contextlib.suppress(OSError, NotImplementedError):
        if path.exists():
            os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# 时间 / period helpers（time helpers 全部走 scheduler.timing，不重复实现）
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO8601 文本；调用方需要"现在写盘"时使用。"""
    return to_iso(utc_now())


def _period_seconds(trigger: ScheduleTrigger) -> int:
    """根据 trigger 反推单周期秒数。

    - ``once``：返回 :data:`sys.maxsize`，永不被判 stale。
    - ``seconds``：``expr`` 是正整数秒数文本（v0.2 新增，配合 1s ticker）。
      失败时退化到 ``GRACE_MIN_SECONDS``。
    - ``interval``：``expr`` 是分钟数文本（v0.1 简化口径，等价 Hermes
      ``schedule.minutes``）。失败时退化到 ``GRACE_MIN_SECONDS``。
    - ``cron``：用 :mod:`croniter` 读两次相邻 next，差值即周期；表达式不可解析
      时退化到 ``GRACE_MIN_SECONDS``（保留 v0.1 行为，避免误判 stale）。
    """
    if trigger.trigger_type is TriggerType.ONCE:
        return sys.maxsize
    if trigger.trigger_type is TriggerType.SECONDS:
        try:
            return max(int(trigger.expr.strip()), 1)
        except (ValueError, AttributeError):
            return GRACE_MIN_SECONDS
    if trigger.trigger_type is TriggerType.INTERVAL:
        try:
            return max(int(trigger.expr.strip()), 1) * 60
        except ValueError:
            return GRACE_MIN_SECONDS
    if trigger.trigger_type is TriggerType.CRON:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter  # type: ignore[import-untyped]

            # 关键修复（v0.2 P1）：cron 表达式必须按 trigger.timezone 解释。
            # croniter(expr, base) 不接受 timezone 参数时按 base.tzinfo 解释，
            # 之前 base = utc_now() 导致 cron 一律按 UTC 解释，与 timing.py
            # 的 compute_first_run_at 时区处理不一致（#3 已修）。
            try:
                tz = ZoneInfo(trigger.timezone) if trigger.timezone else None
            except Exception:
                tz = None  # 非法 timezone 退回 base.tz（UTC）
            base = utc_now().astimezone(tz) if tz else utc_now()
            # 5/6 字段判别：6 字段必须 second_at_beginning=True
            fields_ = trigger.expr.strip().split()
            if len(fields_) == 6:
                it = croniter(trigger.expr, base, second_at_beginning=True)
            else:
                it = croniter(trigger.expr, base)
            first = it.get_next(datetime)
            second = it.get_next(datetime)
            return max(1, int((second - first).total_seconds()))
        except Exception:
            return GRACE_MIN_SECONDS
    return GRACE_MIN_SECONDS


# ---------------------------------------------------------------------------
# JSON helper（处理 Enum / datetime）
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Dataclass <-> dict
# ---------------------------------------------------------------------------


def _task_to_dict(task: ScheduledTask) -> dict[str, Any]:
    """``ScheduledTask`` → JSON-friendly dict（嵌套 dataclass 一并展开）。"""
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "name": task.name,
        "enabled": task.enabled,
        "state": task.state.value,
        "origin": task.origin.value,
        "trigger": {
            "trigger_type": task.trigger.trigger_type.value,
            "expr": task.trigger.expr,
            "timezone": task.trigger.timezone,
        },
        "policy": {
            "session_mode": task.policy.session_mode.value,
            "concurrency_policy": task.policy.concurrency_policy.value,
            "misfire_policy": task.policy.misfire_policy.value,
            "max_turns": task.policy.max_turns,
            "inactivity_timeout_seconds": task.policy.inactivity_timeout_seconds,
            "wall_timeout_seconds": task.policy.wall_timeout_seconds,
            "retry_limit": task.policy.retry_limit,
            "silent_marker_enabled": task.policy.silent_marker_enabled,
        },
        "target": {
            "agent_name": task.target.agent_name,
            "input_text": task.target.input_text,
            "metadata": dict(task.target.metadata),
        },
        "next_run_at": task.next_run_at,
        "last_run_at": task.last_run_at,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    # delivery 配置
    if task.delivery is not None:
        delivery_dict: dict[str, Any] = {"channel": task.delivery.channel.value}
        if task.delivery.target is not None:
            delivery_dict["target"] = task.delivery.target
        payload["delivery"] = delivery_dict
    else:
        payload["delivery"] = None
    # v0.4：preset_id
    payload["preset_id"] = task.preset_id
    return payload


def _dict_to_task(payload: dict[str, Any]) -> ScheduledTask:
    """逆向：dict → ``ScheduledTask``。字段缺失抛 ``KeyError``。"""
    trigger_raw = payload["trigger"]
    trigger = ScheduleTrigger(
        trigger_type=TriggerType(trigger_raw["trigger_type"]),
        expr=trigger_raw["expr"],
        timezone=trigger_raw["timezone"],
    )
    policy_raw = payload["policy"]
    policy = TaskExecutionPolicy(
        session_mode=SessionMode(policy_raw["session_mode"]),
        concurrency_policy=ConcurrencyPolicy(policy_raw["concurrency_policy"]),
        misfire_policy=MisfirePolicy(policy_raw["misfire_policy"]),
        max_turns=policy_raw.get("max_turns"),
        inactivity_timeout_seconds=policy_raw.get("inactivity_timeout_seconds"),
        wall_timeout_seconds=policy_raw.get("wall_timeout_seconds"),
        retry_limit=int(policy_raw.get("retry_limit", 0)),
        silent_marker_enabled=bool(policy_raw.get("silent_marker_enabled", True)),
    )
    target_raw = payload["target"]
    metadata = target_raw.get("metadata") or {}
    target = TaskTarget(
        agent_name=target_raw["agent_name"],
        input_text=target_raw["input_text"],
        metadata={str(k): str(v) for k, v in metadata.items()},
    )
    # delivery 字段；缺失或 None 为兼容旧任务（默认 web）
    delivery_raw = payload.get("delivery")
    delivery: ScheduleDelivery | None
    if delivery_raw is None:
        delivery = None
    else:
        delivery = ScheduleDelivery(
            channel=DeliveryChannel(delivery_raw["channel"]),
            target=delivery_raw.get("target"),
        )
    return ScheduledTask(
        task_id=payload["task_id"],
        name=payload["name"],
        enabled=bool(payload["enabled"]),
        state=TaskState(payload["state"]),
        origin=TaskOrigin(payload["origin"]),
        trigger=trigger,
        policy=policy,
        target=target,
        next_run_at=payload.get("next_run_at"),
        last_run_at=payload.get("last_run_at"),
        created_by=payload["created_by"],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        delivery=delivery,
        preset_id=payload.get("preset_id", ""),
    )


def _run_to_dict(run: ScheduledRun, *, superseded: bool = False) -> dict[str, Any]:
    payload = asdict(run)
    payload["status"] = run.status.value
    payload["failure_reason"] = run.failure_reason.value if run.failure_reason is not None else None
    # v0.3：DeliveryStatus 落盘成字符串值（StrEnum 也能 json，但显式更稳）
    payload["delivery_status"] = run.delivery_status.value
    payload["_superseded"] = superseded
    return payload


def _dict_to_run(payload: dict[str, Any]) -> ScheduledRun:
    failure_reason_raw = payload.get("failure_reason")
    failure_reason = (
        RunFailureReason(failure_reason_raw) if failure_reason_raw is not None else None
    )
    # v0.3：旧 v2 数据没有这三个字段；缺失时按"已存在但未投递"语义取默认值。
    # delivery_status 默认 PENDING，但 v2→v3 迁移时会把已有 run 标记成 SKIPPED
    # （不进未读计数）—— 这里的默认 PENDING 仅用于完全空白的反序列化。
    delivery_status_raw = payload.get("delivery_status", DeliveryStatus.PENDING.value)
    delivery_status = DeliveryStatus(delivery_status_raw)
    return ScheduledRun(
        run_id=payload["run_id"],
        task_id=payload["task_id"],
        status=RunStatus(payload["status"]),
        scheduled_for=payload["scheduled_for"],
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        session_id=payload.get("session_id"),
        result_status=payload.get("result_status"),
        final_message_excerpt=payload.get("final_message_excerpt"),
        error_message=payload.get("error_message"),
        failure_reason=failure_reason,
        delivery_error=payload.get("delivery_error"),
        silent_suppressed=bool(payload.get("silent_suppressed", False)),
        delivered_at=payload.get("delivered_at"),
        delivery_status=delivery_status,
        seen_at=payload.get("seen_at"),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


_TASKS_FILENAME = "scheduled_tasks.json"
_RUNS_DIRNAME = "runs"
_AUDITS_FILENAME = "audits.jsonl"
_LOCK_FILENAME = ".scheduler.lock"


class Store:
    """v0.1 文件 Store。

    所有方法同步实现。读写都先经过 :meth:`_scheduler_lock` 串行化（除单纯查询外）。
    传入的 ``home_dir`` 是 cron 数据根目录（**不**调 ``get_kongming_home()``，避免
    单元测试受 env 污染）。
    """

    def __init__(self, home_dir: Path) -> None:
        self._home = Path(home_dir).resolve()
        self._tasks_path = self._home / _TASKS_FILENAME
        self._runs_dir = self._home / _RUNS_DIRNAME
        self._audits_path = self._home / _AUDITS_FILENAME
        self._lock_path = self._home / _LOCK_FILENAME
        self._home.mkdir(parents=True, exist_ok=True)
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        _secure_dir(self._home)
        _secure_dir(self._runs_dir)
        self._migrate_schema_if_needed()

    # ------------------------------------------------------------------
    # schema 迁移（v0.2 启动检测）
    # ------------------------------------------------------------------

    def _migrate_schema_if_needed(self) -> None:
        """检测 ``scheduled_tasks.json`` 的 ``schema_version`` 是否为当前
        :data:`scheduler.domain.SCHEMA_VERSION`。

        不为 ``SCHEMA_VERSION``（包括缺字段、< 当前、> 当前、文件损坏）时
        采用 **重置式迁移**（与 v0.2 一致；不做字段转换）：

        1. 把现有 ``scheduled_tasks.json`` 重命名为
           ``scheduled_tasks.json.v<detected>.bak.<unix_ts>``
           （``v<detected>`` 反映检测到的来源版本，例如 ``v1``、``v2``、``vmissing``）
        2. 写一份空的当前 schema 文件（``schema_version=SCHEMA_VERSION``、
           ``tasks=[]``）
        3. 写 audit 一条 ``schema_migrated_v<detected>_to_v<current>``，
           ``payload`` 含 ``backup_path`` / ``detected_version`` /
           ``current_schema_version``
        4. stderr 输出 warning，告知备份路径与原因，让用户知情

        ``scheduled_tasks.json`` 不存在时什么都不做：后续
        :meth:`_write_tasks_payload` 在首次创建任务时按当前 schema 落盘。

        设计权衡（v0.3 cron-delivery）：v0.3 引入 ``ScheduleDelivery`` /
        ``ScheduledRun.delivered_at`` 等字段。**仍然走重置式而非字段转换**，
        理由：与 v0.2 一致 / bug 面小 / 用户当前 cron 任务多为已 completed
        的 oneshot，重置影响小。代价：旧 recurring 任务需用户重新创建。
        """
        if not self._tasks_path.exists():
            return

        detected_version: Any
        try:
            with open(self._tasks_path, encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and "schema_version" in payload:
                detected_version = payload.get("schema_version")
            else:
                detected_version = "missing"
        except (OSError, json.JSONDecodeError):
            detected_version = "unparseable"

        if detected_version == SCHEMA_VERSION:
            return  # 当前 schema，无需迁移

        import time

        # 用 detected_version 生成备份后缀，明确记录来源（v0.3 起不再硬编码 v1）
        if isinstance(detected_version, int) or detected_version in ("missing", "unparseable"):
            bak_label = f"v{detected_version}"
        else:
            bak_label = "vunknown"

        ts = int(time.time())
        backup = self._tasks_path.with_name(f"{_TASKS_FILENAME}.{bak_label}.bak.{ts}")
        # 防御：极端情况下同秒多次启动；用 counter 避免覆盖已有备份
        counter = 1
        while backup.exists():
            backup = self._tasks_path.with_name(f"{_TASKS_FILENAME}.{bak_label}.bak.{ts}.{counter}")
            counter += 1
        self._tasks_path.rename(backup)

        # 写一份空的当前 schema 文件
        self._write_tasks_payload([])

        # audit + stderr warning
        audit_action = f"schema_migrated_{bak_label}_to_v{SCHEMA_VERSION}"
        self.append_audit(
            action=audit_action,
            task_id=None,
            actor="store",
            payload={
                "backup_path": str(backup),
                "detected_version": detected_version,
                "current_schema_version": SCHEMA_VERSION,
            },
        )

        print(
            f"[scheduler] WARNING: scheduled_tasks.json schema_version={detected_version!r} "
            f"!= {SCHEMA_VERSION}; backed up to {backup} and reset to empty v{SCHEMA_VERSION}. "
            "Old tasks must be recreated.",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # File lock
    # ------------------------------------------------------------------

    @contextmanager
    def _scheduler_lock(self) -> Iterator[None]:
        """非阻塞 file lock；抢不到 raise :class:`SchedulerBusyError`。"""
        # touch + open 'r+' 保证 fd 始终存在；写入 PID + 时间戳便于排查
        if not self._lock_path.exists():
            self._lock_path.touch()
            _secure_file(self._lock_path)
        fd = os.open(self._lock_path, os.O_RDWR)
        try:
            if not _try_acquire(fd):
                raise SchedulerBusyError(
                    f"scheduler lock {self._lock_path} held by another process"
                )
            try:
                # 把当前 owner 信息写进 lock 文件（best-effort，不影响互斥）
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()} {_now_iso()}\n".encode())
                yield
            finally:
                _release(fd)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # tasks.json 原子读写
    # ------------------------------------------------------------------

    def _load_tasks_payload(self) -> dict[str, Any]:
        if not self._tasks_path.exists():
            return {"schema_version": SCHEMA_VERSION, "updated_at": _now_iso(), "tasks": []}
        try:
            with open(self._tasks_path, encoding="utf-8") as f:
                payload: dict[str, Any] = json.load(f)
                return payload
        except json.JSONDecodeError:
            try:
                with open(self._tasks_path, encoding="utf-8") as f:
                    fallback: dict[str, Any] = json.loads(f.read(), strict=False)
                    return fallback
            except Exception as exc:
                raise RuntimeError(f"cron tasks.json 损坏: {self._tasks_path} ({exc})") from exc

    def _write_tasks_payload(self, tasks: list[dict[str, Any]]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "tasks": tasks,
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._tasks_path.parent), suffix=".tmp", prefix=".tasks_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._tasks_path)
            _secure_file(self._tasks_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _load_tasks(self) -> list[ScheduledTask]:
        payload = self._load_tasks_payload()
        raw = payload.get("tasks", []) or []
        return [_dict_to_task(item) for item in raw]

    def _save_tasks(self, tasks: list[ScheduledTask]) -> None:
        self._write_tasks_payload([_task_to_dict(t) for t in tasks])

    # ------------------------------------------------------------------
    # task CRUD
    # ------------------------------------------------------------------

    def create_task(self, task: ScheduledTask) -> ScheduledTask:
        """新增任务；同 ``task_id`` 已存在抛 ``ValueError``。

        cron 触发器在创建期立刻校验表达式：``croniter`` 不能解析 → ``ValueError``，
        避免不合法 cron 一直走 ``GRACE_MIN_SECONDS`` 兜底导致 stale 误判。
        """
        if task.trigger.trigger_type is TriggerType.CRON:
            try:
                from croniter import croniter

                croniter(task.trigger.expr)
            except Exception as exc:
                raise ValueError(f"invalid cron expression: {task.trigger.expr!r} ({exc})") from exc
        with self._scheduler_lock():
            tasks = self._load_tasks()
            if any(t.task_id == task.task_id for t in tasks):
                raise ValueError(f"task_id 已存在: {task.task_id}")
            tasks.append(task)
            self._save_tasks(tasks)
            return task

    def update_task(self, task_id: str, **fields_to_update: Any) -> ScheduledTask:
        """部分字段更新；自动刷新 ``updated_at``；不存在抛 ``TaskNotFoundError``。

        注意：状态约束（``enabled=False`` 时 ``state`` 必须 ∈ {paused, disabled,
        completed}）由 dataclass ``__post_init__`` 校验，调用方负责传入合法组合。
        """
        with self._scheduler_lock():
            tasks = self._load_tasks()
            updated: ScheduledTask | None = None
            allowed = {f.name for f in fields(ScheduledTask)}
            unknown = set(fields_to_update) - allowed
            if unknown:
                raise SchedulerStoreError(f"未知字段: {sorted(unknown)}")
            payload = dict(fields_to_update)
            payload.setdefault("updated_at", _now_iso())
            for idx, t in enumerate(tasks):
                if t.task_id == task_id:
                    updated = replace(t, **payload)
                    tasks[idx] = updated
                    break
            if updated is None:
                raise TaskNotFoundError(task_id)
            self._save_tasks(tasks)
            return updated

    def get_task(self, task_id: str) -> ScheduledTask | None:
        for t in self._load_tasks():
            if t.task_id == task_id:
                return t
        return None

    def list_tasks(self, *, include_disabled: bool = False) -> list[ScheduledTask]:
        tasks = self._load_tasks()
        if include_disabled:
            return tasks
        return [t for t in tasks if t.enabled]

    def delete_task(self, task_id: str) -> bool:
        with self._scheduler_lock():
            tasks = self._load_tasks()
            new_tasks = [t for t in tasks if t.task_id != task_id]
            if len(new_tasks) == len(tasks):
                return False
            self._save_tasks(new_tasks)
            return True

    # ------------------------------------------------------------------
    # run CRUD
    # ------------------------------------------------------------------

    def _runs_path(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or "\\" in task_id or task_id in {".", ".."}:
            raise SchedulerStoreError(f"非法 task_id: {task_id!r}")
        return self._runs_dir / f"{task_id}.jsonl"

    def _append_run_line(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json_dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _secure_file(path)

    def append_run(self, run: ScheduledRun) -> None:
        """新增一条 run 行（默认 ``_superseded=False``）。"""
        path = self._runs_path(run.task_id)
        self._append_run_line(path, _run_to_dict(run))

    def supersede_and_append_run(self, run: ScheduledRun) -> None:
        """状态变更：先把同 ``run_id`` 的最近一条非 superseded 行复制并打上
        ``_superseded=True``，再 append 新状态行。

        不存在旧行时退化为普通 append（首次写入即新状态的少见情况）。
        """
        path = self._runs_path(run.task_id)
        previous = self._read_last_active_line(path, run_id=run.run_id)
        if previous is not None:
            previous["_superseded"] = True
            self._append_run_line(path, previous)
        self._append_run_line(path, _run_to_dict(run))

    def _iter_run_payloads(self, path: Path) -> Iterator[dict[str, Any]]:
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # 损坏的单行不能阻塞整个流；跳过即可（下游若严格可后续加 strict 参数）
                    continue

    def _read_last_active_line(self, path: Path, *, run_id: str) -> dict[str, Any] | None:
        last: dict[str, Any] | None = None
        for payload in self._iter_run_payloads(path):
            if payload.get("run_id") != run_id:
                continue
            if payload.get("_superseded"):
                continue
            last = payload
        return last

    def list_runs(self, task_id: str, *, limit: int | None = None) -> list[ScheduledRun]:
        """按落盘顺序返回非 superseded 行；``limit`` 取最近 N 条。

        语义：每个 ``run_id`` 在物理 jsonl 中可能出现多行（``running`` → ``completed``
        会先 append 一份带 ``_superseded=True`` 的副本再 append 新状态行；首次
        append 的"原始"那条不会被回写）。本方法按 ``run_id`` 折叠，只保留每个
        ``run_id`` 在文件里 **最后一条** 记录；再过滤 ``_superseded=True``。

        这样既保持物理层 append-only，又让逻辑层只看到"当前状态"。
        """
        path = self._runs_path(task_id)
        latest_by_run: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for payload in self._iter_run_payloads(path):
            run_id = payload.get("run_id")
            if not isinstance(run_id, str):
                continue
            if run_id not in latest_by_run:
                order.append(run_id)
            latest_by_run[run_id] = payload

        runs: list[ScheduledRun] = []
        for run_id in order:
            payload = latest_by_run[run_id]
            if payload.get("_superseded"):
                continue
            runs.append(_dict_to_run(payload))
        if limit is not None and limit >= 0:
            return runs[-limit:]
        return runs

    def get_latest_run(self, task_id: str) -> ScheduledRun | None:
        runs = self.list_runs(task_id)
        return runs[-1] if runs else None

    # ------------------------------------------------------------------
    # v0.3 cron-delivery：跨 task run 聚合查询 + seen 状态
    # ------------------------------------------------------------------

    def list_recent_runs(
        self,
        *,
        limit: int = 50,
        since: str | None = None,
        task_id: str | None = None,
        cursor: str | None = None,
    ) -> list[ScheduledRun]:
        """列出最近的 runs，跨 task 聚合，按 ``started_at`` 倒序。

        - ``task_id``：限定单 task；``None`` 表示扫所有 task 的 runs（``runs/*.jsonl``）
        - ``since``：ISO8601；只返回 ``started_at >= since`` 的 run；缺 ``started_at``
          的 run 被过滤（v0.3 cron-delivery 引入）
        - ``cursor``：ISO8601；非 ``None`` 时只返回 ``started_at`` **严格 <** cursor
          的 run；缺 ``started_at`` 的 run 被过滤（v0.5 web-cron-router 引入，配合
          ``GET /api/cron/runs?cursor=`` 分页；与 ``since`` 正交，可同时给）
        - ``limit``：默认 50，上限 200，超出抛 ``ValueError``；``<= 0`` 同样越界
        - 排序键：``(started_at is None, started_at or "", run_id)``；
          ``started_at=None`` 的 run（PENDING）排到最后；同 started_at 用 run_id
          做 tie-break。整体倒序（``reverse=True``）。

        实现方式：复用 ``list_runs(task_id)`` 的 superseded 过滤；本方法只做
        跨 task 聚合 + 过滤 + 排序 + 切片，不引入新的物理读路径。

        性能：当前 runs/*.jsonl 数量 <100、每个 jsonl <1k 行，全扫 ~10ms 量级；
        若数据量上来再加索引（``DeliveryStatus`` / 时间范围）。
        """
        if not isinstance(limit, int) or limit <= 0 or limit > 200:
            raise ValueError(f"limit must be 1..200, got {limit!r}")

        if task_id is not None:
            runs = self.list_runs(task_id, limit=None)
        else:
            runs = []
            for run_path in sorted(self._runs_dir.glob("*.jsonl")):
                runs.extend(self.list_runs(run_path.stem, limit=None))

        if since is not None:
            runs = [r for r in runs if r.started_at is not None and r.started_at >= since]

        if cursor is not None:
            runs = [r for r in runs if r.started_at is not None and r.started_at < cursor]

        # 排序键说明：(started_at is None, started_at or "", run_id) + reverse=True
        # - started_at is None → True 排在 False 前（reverse 后 None 在末尾）
        # - 有 started_at 时按字典序倒序（ISO8601 字符串字典序 == 时间序）
        # - tie-break 用 run_id 保证 deterministic
        runs.sort(
            key=lambda r: (r.started_at is None, r.started_at or "", r.run_id),
            reverse=True,
        )

        return runs[:limit]

    def mark_run_seen(self, run_id: str) -> ScheduledRun | None:
        """v0.3：把 ``run_id`` 对应 run 的 ``seen_at`` 设为现在 ISO 时间，
        并 supersede 旧记录。

        - 找不到 run → 返回 ``None``，不抛
        - 已有 ``seen_at`` → 不重复写盘，直接返回原 run（幂等）
        - 找到且未读 → ``supersede_and_append_run`` 写入新行，返回新 run
        """
        with self._scheduler_lock():
            for run_path in sorted(self._runs_dir.glob("*.jsonl")):
                task_id = run_path.stem
                for run in self.list_runs(task_id, limit=None):
                    if run.run_id != run_id:
                        continue
                    if run.seen_at is not None:
                        return run
                    updated = replace(run, seen_at=_now_iso())
                    self.supersede_and_append_run(updated)
                    return updated
        return None

    def mark_all_runs_seen_until(self, until_ts: str) -> int:
        """v0.3：批量把 ``started_at <= until_ts`` 且未读的 run 标记为已读。

        - ``until_ts``：ISO8601 时间；缺 ``started_at`` 的 run 不算入比较
        - 返回值：本次实际更新的 run 数（已读 / 缺 started_at 不计）

        语义：用户在 web ``/cron`` 点 "全部已读" 时调用，``until_ts`` 通常传当前
        时间；这样未来 run 不被误标。
        """
        updated_count = 0
        with self._scheduler_lock():
            for run_path in sorted(self._runs_dir.glob("*.jsonl")):
                task_id = run_path.stem
                for run in self.list_runs(task_id, limit=None):
                    if run.seen_at is not None:
                        continue
                    if run.started_at is None:
                        continue
                    if run.started_at > until_ts:
                        continue
                    updated = replace(run, seen_at=_now_iso())
                    self.supersede_and_append_run(updated)
                    updated_count += 1
        return updated_count

    def count_unseen_runs(self) -> int:
        """v0.3：用于红点未读计数。

        计数口径：``delivery_status == DELIVERED`` 且 ``seen_at is None`` 的
        run 总数（未投递 / 已读 / 跳过的不计）。
        """
        count = 0
        for run_path in sorted(self._runs_dir.glob("*.jsonl")):
            for run in self.list_runs(run_path.stem, limit=None):
                if run.delivery_status == DeliveryStatus.DELIVERED and run.seen_at is None:
                    count += 1
        return count

    # ------------------------------------------------------------------
    # due / advance
    # ------------------------------------------------------------------

    def reserve_due_tasks(self, *, now: str) -> list[DueTaskReservation]:
        """在 file lock 内挑出 due tasks 并推进 ``next_run_at``。

        算法（与 Hermes ``get_due_jobs`` 等价口径，文档 §9）：

        1. 跳过 ``enabled=False`` 或缺 ``next_run_at`` 的任务
        2. ``next_run_dt > now``：未到点跳过
        3. one-shot：若 ``last_run_at is None`` 且 ``next_run_at <=
           now + ONESHOT_GRACE_SECONDS`` → due（不再推进 ``next_run_at``）
        4. recurring：``period = _period_seconds(trigger)``、``grace = clamp(...)``
           - 偏移 ≤ grace → 视为正常 due，推进 ``next_run_at = now + period`` 后返回
           - 偏移 > grace → 视为 stale，按 ``task.policy.misfire_policy`` 分派：

             * ``SKIP``：快进 + 写 audit ``run_skipped_stale``，**不**返回 due
             * ``CATCH_UP_ONCE``：推进 ``next_run_at``，**保留原** ``next_run_at``
               作为 ``scheduled_for``，写 audit ``run_catch_up_once``，返回 due
             * ``FIRE_NOW``：推进 ``next_run_at``，``scheduled_for`` 用 ``now``
               （让审计/run 看起来是"现在"），写 audit ``run_fire_now``，返回 due
        """
        now_dt = parse_iso(now)
        reservations: list[DueTaskReservation] = []
        with self._scheduler_lock():
            tasks = self._load_tasks()
            mutated = False

            for idx, task in enumerate(tasks):
                if not task.enabled:
                    continue
                if not task.next_run_at:
                    continue

                next_dt = parse_iso(task.next_run_at)
                trigger_type = task.trigger.trigger_type

                # one-shot
                if trigger_type is TriggerType.ONCE:
                    if task.last_run_at is not None:
                        continue
                    # P0 修复（v0.2.1）：未到点的 ONCE 任务必须跳过。
                    # is_within_oneshot_grace(future_run_at, now) 对未来时刻
                    # 永远返回 True（因为 future >= now - 120s），需要先判
                    # next_dt > now_dt 才能正确放过未到期任务。
                    # recurring 分支已有同款检查，本分支之前漏了 → 用户实测
                    # "5 分钟后提醒" 任务被立即 fire。
                    if next_dt > now_dt:
                        continue
                    if not is_within_oneshot_grace(next_dt, now_dt):
                        continue
                    # 关键修复（v0.2 P0）：reserve 时立即归档 ONCE 任务。
                    #
                    # 不归档会让 ONCE 在 grace 窗口（120s）内被持续 reserve →
                    # 持续 fire fresh run，单个一次性任务被触发 100+ 次。
                    # 这里同步标记 last_run_at + state=COMPLETED + enabled=False
                    # + next_run_at=None，让下次 tick 进入 ONCE 分支时被
                    # ``last_run_at is not None`` 跳掉，也不会被外层
                    # ``not task.enabled`` / ``not task.next_run_at`` 兜底放过。
                    archived = replace(
                        task,
                        last_run_at=now,
                        state=TaskState.COMPLETED,
                        enabled=False,
                        next_run_at=None,
                        updated_at=_now_iso(),
                    )
                    tasks[idx] = archived
                    mutated = True
                    self._append_audit_unlocked(
                        action="run_oneshot_archived",
                        task_id=task.task_id,
                        actor="scheduler",
                        payload={
                            "original_next_run_at": task.next_run_at,
                            "reserved_at": now,
                        },
                    )
                    reservations.append(
                        DueTaskReservation(
                            task=archived,
                            scheduled_for=task.next_run_at,
                            reserved_at=now,
                        )
                    )
                    continue

                # recurring（interval / cron）
                if next_dt > now_dt:
                    continue

                period = _period_seconds(task.trigger)
                grace = grace_seconds_for_period(period)
                offset = (now_dt - next_dt).total_seconds()
                advance_to = to_iso(now_dt + timedelta(seconds=period))
                original_scheduled_for = task.next_run_at

                if is_stale_recurring(next_dt, now_dt, period):
                    # stale → 按 misfire_policy 分派
                    misfire = task.policy.misfire_policy

                    if misfire is MisfirePolicy.SKIP:
                        tasks[idx] = replace(task, next_run_at=advance_to, updated_at=_now_iso())
                        mutated = True
                        self._append_audit_unlocked(
                            action="run_skipped_stale",
                            task_id=task.task_id,
                            actor="scheduler",
                            payload={
                                "previous_next_run_at": original_scheduled_for,
                                "advanced_to": advance_to,
                                "offset_seconds": offset,
                                "grace_seconds": grace,
                                "misfire_policy": misfire.value,
                            },
                        )
                        continue

                    if misfire is MisfirePolicy.CATCH_UP_ONCE:
                        # 补跑 1 次：scheduled_for 保留原 next_run_at；同时推进 next_run_at
                        tasks[idx] = replace(task, next_run_at=advance_to, updated_at=_now_iso())
                        mutated = True
                        self._append_audit_unlocked(
                            action="run_catch_up_once",
                            task_id=task.task_id,
                            actor="scheduler",
                            payload={
                                "original_scheduled_for": original_scheduled_for,
                                "advanced_to": advance_to,
                                "offset_seconds": offset,
                                "grace_seconds": grace,
                                "misfire_policy": misfire.value,
                            },
                        )
                        reservations.append(
                            DueTaskReservation(
                                task=tasks[idx],
                                scheduled_for=original_scheduled_for,
                                reserved_at=now,
                            )
                        )
                        continue

                    if misfire is MisfirePolicy.FIRE_NOW:
                        # 立即触发：scheduled_for 用 now（不是原 next_run_at）；同时推进 next_run_at
                        tasks[idx] = replace(task, next_run_at=advance_to, updated_at=_now_iso())
                        mutated = True
                        self._append_audit_unlocked(
                            action="run_fire_now",
                            task_id=task.task_id,
                            actor="scheduler",
                            payload={
                                "original_scheduled_for": original_scheduled_for,
                                "fired_at": now,
                                "advanced_to": advance_to,
                                "offset_seconds": offset,
                                "grace_seconds": grace,
                                "misfire_policy": misfire.value,
                            },
                        )
                        reservations.append(
                            DueTaskReservation(
                                task=tasks[idx],
                                scheduled_for=now,
                                reserved_at=now,
                            )
                        )
                        continue

                    # 防御式：枚举之外的 misfire_policy 取值
                    raise ValueError(f"unknown misfire_policy: {misfire!r}")

                scheduled_for = original_scheduled_for
                tasks[idx] = replace(task, next_run_at=advance_to, updated_at=_now_iso())
                mutated = True
                reservations.append(
                    DueTaskReservation(
                        task=tasks[idx],
                        scheduled_for=scheduled_for,
                        reserved_at=now,
                    )
                )

            if mutated:
                self._save_tasks(tasks)
        return reservations

    def advance_next_run(self, task_id: str, *, next_run_at: str) -> None:
        """显式把 ``next_run_at`` 推进到给定时间（用于 trigger engine 重算后回写）。"""
        with self._scheduler_lock():
            tasks = self._load_tasks()
            found = False
            for idx, t in enumerate(tasks):
                if t.task_id == task_id:
                    tasks[idx] = replace(t, next_run_at=next_run_at, updated_at=_now_iso())
                    found = True
                    break
            if not found:
                raise TaskNotFoundError(task_id)
            self._save_tasks(tasks)

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------

    def recover_stale_runs(self) -> int:
        """启动时收尾：所有任务的 latest run 处于 ``RUNNING`` → 强制 ``ABANDONED``
        + ``failure_reason=ABANDONED_ON_RESTART`` + 写 audit ``run_failed``。

        返回被收尾的 run 数。
        """
        recovered = 0
        with self._scheduler_lock():
            tasks = self._load_tasks()
            for task in tasks:
                latest = self.get_latest_run(task.task_id)
                if latest is None or latest.status is not RunStatus.RUNNING:
                    continue
                finished = ScheduledRun(
                    run_id=latest.run_id,
                    task_id=latest.task_id,
                    status=RunStatus.ABANDONED,
                    scheduled_for=latest.scheduled_for,
                    started_at=latest.started_at,
                    finished_at=_now_iso(),
                    session_id=latest.session_id,
                    result_status=latest.result_status,
                    final_message_excerpt=latest.final_message_excerpt,
                    error_message=latest.error_message,
                    failure_reason=RunFailureReason.ABANDONED_ON_RESTART,
                    delivery_error=latest.delivery_error,
                    silent_suppressed=latest.silent_suppressed,
                )
                self.supersede_and_append_run(finished)
                self._append_audit_unlocked(
                    action="run_failed",
                    task_id=task.task_id,
                    actor="scheduler",
                    payload={
                        "run_id": finished.run_id,
                        "failure_reason": RunFailureReason.ABANDONED_ON_RESTART.value,
                        "recovery": "abandoned_on_restart",
                    },
                )
                recovered += 1
        return recovered

    # ------------------------------------------------------------------
    # cancel run（concurrency_policy=replace 时调用）
    # ------------------------------------------------------------------

    def cancel_run(
        self,
        *,
        run_id: str,
        task_id: str,
        failure_reason: RunFailureReason | None = None,
        error_message: str | None = None,
    ) -> None:
        """把仍在 RUNNING 的某条 run 收尾为 :attr:`RunStatus.CANCELLED`。

        内部走 :meth:`supersede_and_append_run`：先把当前 RUNNING 行复制并
        打 ``_superseded=True``，再 append 一条同 ``run_id`` 的 CANCELLED 行。

        - 找不到 ``run_id`` → :class:`TaskNotFoundError`
        - 该 run 已经不是 RUNNING（已 completed / failed / cancelled / ...）
          → :class:`ValueError`，不允许重复收尾
        """
        latest = self.get_latest_run(task_id)
        if latest is None or latest.run_id != run_id:
            raise TaskNotFoundError(f"run_id {run_id!r} not found for task {task_id!r}")
        if latest.status is not RunStatus.RUNNING:
            raise ValueError(
                f"cannot cancel run {run_id!r}: status is {latest.status.value}, expected RUNNING"
            )

        cancelled = ScheduledRun(
            run_id=latest.run_id,
            task_id=latest.task_id,
            status=RunStatus.CANCELLED,
            scheduled_for=latest.scheduled_for,
            started_at=latest.started_at,
            finished_at=_now_iso(),
            session_id=latest.session_id,
            result_status=None,
            final_message_excerpt=latest.final_message_excerpt,
            error_message=error_message,
            failure_reason=failure_reason,
            delivery_error=None,
            silent_suppressed=False,
        )
        self.supersede_and_append_run(cancelled)

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def append_audit(
        self,
        *,
        action: str,
        task_id: str | None,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        """带锁 append 一条 audit 行；推荐外部调用走这里。"""
        with self._scheduler_lock():
            self._append_audit_unlocked(
                action=action, task_id=task_id, actor=actor, payload=payload
            )

    def _append_audit_unlocked(
        self,
        *,
        action: str,
        task_id: str | None,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        """**已持锁** 时使用；reserve_due_tasks / recover_stale_runs 内部走这条
        以避免重入。文档 §4.3 字段：``audit_id / task_id / action / actor /
        payload / created_at``。
        """
        record = {
            "audit_id": _new_audit_id(),
            "task_id": task_id,
            "action": action,
            "actor": actor,
            "payload": dict(payload),
            "created_at": _now_iso(),
        }
        self._audits_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._audits_path, "a", encoding="utf-8") as f:
            f.write(_json_dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _secure_file(self._audits_path)

    def list_audits(
        self,
        *,
        limit: int | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._audits_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self._audits_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if task_id is not None and record.get("task_id") != task_id:
                    continue
                out.append(record)
        if limit is not None and limit >= 0:
            return out[-limit:]
        return out


# ---------------------------------------------------------------------------
# 工具：audit_id（self-contained，不依赖第三方 uuid lib 之外的东西）
# ---------------------------------------------------------------------------


def _new_audit_id() -> str:
    """简短 audit_id：``<unix_ns>-<urandom_hex8>``。"""
    nanos = utc_now().timestamp() * 1_000_000_000
    return f"{int(nanos)}-{os.urandom(4).hex()}"


__all__ = [
    "SchedulerBusyError",
    "SchedulerStoreError",
    "Store",
    "TaskNotFoundError",
]
