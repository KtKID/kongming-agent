"""
司天编排层——串联扫描 → 建议 → 分析的全链路。

Role:
    顶层编排器，对外暴露三个入口函数：
    SiTianRunOnce（单轮扫描全链路）、SiTianRunLoop（循环模式）、
    SiTianReadState（只读状态查询）。

Owns:
    - runtime_state 读写与 interval 调度逻辑
    - scan → materialize → suggestions → (可选) LLM analysis 的调用顺序
    - 错误处理与 runtime_state 回退

Does not own:
    - 扫描细节（scanners.py）
    - 持久化细节（store.py）
    - LLM prompt 构造（analyzer.py）
    - CLI 入口（cli.py）

Called by:
    - cli.py（run-once / loop / state 子命令）

Key outputs:
    - SiTianRunResult（status / observations_count / suggestions_count / error）
    - 副作用：写 observations / runtime_state / workspace_state / report 到磁盘

Change risks:
    - 编排顺序变化会影响全链路输出
    - interval 逻辑变化会影响循环模式的触发频率
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sitian.config import SiTianConfig, SiTianSourceConfig
from sitian.models import JsonValue, SiTianObservation, SiTianSourceRuntimeState

if TYPE_CHECKING:
    from core.contracts import LLMProvider
    from infrastructure.config.models import Config
from sitian.scanners import SiTianScanBatch, SiTianScanSource
from sitian.store import SiTianRecordsStore
from sitian.suggestions import SiTianMaterializeState

_log = logging.getLogger("sitian.service")

_DEFAULT_RETRY_BACKOFF_SEC = 60


@dataclass(frozen=True)
class SiTianRunResult:
    observed_at: str
    ready_source_ids: tuple[str, ...]
    scanned_source_ids: tuple[str, ...]
    failed_sources: dict[str, str]
    observation_count: int


async def SiTianRunOnce(
    cfg: Config | SiTianConfig,
    *,
    store: SiTianRecordsStore | None = None,
    now: datetime | None = None,
    llm_provider: LLMProvider | None = None,
) -> SiTianRunResult:
    sitian_cfg = _extract_sitian_config(cfg)
    records = store or SiTianRecordsStore()
    observed_at = _to_iso(now)
    # 计时：startedAt 用墙钟时间戳（与 observed_at 一致），耗时用 perf_counter（单调）
    start_perf = time.perf_counter()
    _log.info(
        "sitian run started: observed_at=%s sources=%d",
        observed_at,
        len(sitian_cfg.sources),
    )

    await records.ensure_layout()
    await records.save_config_snapshot(sitian_cfg.model_dump(mode="json"))

    runtime_states = await _SiTianLoadRuntimeStates(records, sitian_cfg, observed_at=observed_at)
    ready_sources = _SiTianSelectReadySources(sitian_cfg, runtime_states, observed_at=observed_at)

    observations: list[SiTianScanBatch] = []
    failed_sources: dict[str, str] = {}
    runtime_by_source = {state.source_id: state for state in runtime_states}

    for source in ready_sources:
        try:
            batch = await SiTianScanSource(
                source,
                observed_at=observed_at,
                scanner_config=sitian_cfg.scanner,
            )
        except Exception as exc:
            failed_sources[source.id] = str(exc)
            runtime_by_source[source.id] = _SiTianAdvanceRuntimeError(
                source=source,
                runtime_state=runtime_by_source.get(source.id),
                observed_at=observed_at,
                error=str(exc),
                default_scan_interval_sec=sitian_cfg.default_scan_interval_sec,
            )
            continue

        observations.append(batch)
        runtime_by_source[source.id] = _SiTianAdvanceRuntimeSuccess(
            source=source,
            runtime_state=runtime_by_source.get(source.id),
            observed_at=observed_at,
            default_scan_interval_sec=sitian_cfg.default_scan_interval_sec,
        )

    merged_runtime_states = _SiTianFinalizeRuntimeStates(
        sitian_cfg,
        runtime_by_source=runtime_by_source,
        observed_at=observed_at,
    )
    flat_observations = tuple(
        observation for batch in observations for observation in batch.observations
    )
    if flat_observations:
        await records.append_observations(flat_observations)
    await records.save_runtime_states(merged_runtime_states)

    all_observations = await records.load_observations(limit=500)
    workspace_state, work_items, suggestions, summary = SiTianMaterializeState(
        sitian_cfg,
        runtime_states=merged_runtime_states,
        observations=all_observations,
        updated_at=observed_at,
    )
    await records.save_workspace_state(workspace_state)
    await records.save_work_items(work_items)
    await records.save_latest_suggestions(suggestions)
    await records.save_latest_summary(summary)

    analyzer_duration_ms: int | None = None
    if llm_provider is not None and sitian_cfg.analyzer.enabled:
        analyzer_duration_ms = await _run_llm_analysis(
            records=records,
            provider=llm_provider,
            sitian_cfg=sitian_cfg,
            observations=all_observations,
            observed_at=observed_at,
        )

    finished_at = _to_iso(None)
    duration_ms = int((time.perf_counter() - start_perf) * 1000)
    _log.info(
        "sitian run finished in %dms (analyzer=%s ms), observations=%d failures=%d",
        duration_ms,
        analyzer_duration_ms if analyzer_duration_ms is not None else "skipped",
        len(flat_observations),
        len(failed_sources),
    )

    await records.write_scan_snapshot(
        {
            "scanId": observed_at,
            "observedAt": observed_at,
            "startedAt": observed_at,
            "finishedAt": finished_at,
            "durationMs": duration_ms,
            "analyzerDurationMs": analyzer_duration_ms,
            "readySourceIds": [source.id for source in ready_sources],
            "scannedSourceIds": [batch.source_id for batch in observations],
            "failedSources": failed_sources,
            "observationCount": len(flat_observations),
        }
    )
    return SiTianRunResult(
        observed_at=observed_at,
        ready_source_ids=tuple(source.id for source in ready_sources),
        scanned_source_ids=tuple(batch.source_id for batch in observations),
        failed_sources=failed_sources,
        observation_count=len(flat_observations),
    )


async def _run_llm_analysis(
    *,
    records: SiTianRecordsStore,
    provider: LLMProvider,
    sitian_cfg: SiTianConfig,
    observations: tuple[SiTianObservation, ...],
    observed_at: str,
) -> int | None:
    """跑 LLM 分析，返回耗时（ms）。

    返回 None 表示：observations 未变化（skip_if_unchanged 命中）→ 没真的调 LLM。
    返回 >= 0 的 int 表示：LLM 调用确实跑过（无论成功失败，耗时都计入）。
    """
    from sitian.analyzer import compute_observations_hash, report_to_markdown, sitian_analyze

    if sitian_cfg.analyzer.skip_if_unchanged:
        current_hash = compute_observations_hash(observations)
        previous_hash = await records.load_observations_hash()
        if current_hash == previous_hash:
            _log.info("observations unchanged (hash=%s), skip LLM analysis", current_hash[:12])
            return None
    else:
        current_hash = None

    analyzer_start = time.perf_counter()
    report = await sitian_analyze(
        observations,
        provider=provider,
        analyzer_config=sitian_cfg.analyzer,
        interests_config=sitian_cfg.interests,
        observed_at=observed_at,
        sitian_root=records.root_dir,
    )
    analyzer_duration_ms = int((time.perf_counter() - analyzer_start) * 1000)

    await records.save_sitian_report(report.to_dict())
    await records.save_latest_summary(report_to_markdown(report))

    if current_hash is not None:
        await records.save_observations_hash(current_hash)

    if report.errors:
        for err in report.errors:
            _log.warning("sitian analysis error: %s", err)
    else:
        _log.info(
            "sitian analysis complete in %dms: %d alerts, %d projects, report_id=%s",
            analyzer_duration_ms,
            len(report.top_alerts),
            len(report.projects),
            report.report_id,
        )
    return analyzer_duration_ms


async def SiTianRunLoop(
    cfg: Config | SiTianConfig,
    *,
    store: SiTianRecordsStore | None = None,
    stop_event: asyncio.Event | None = None,
    llm_provider: LLMProvider | None = None,
) -> None:
    sitian_cfg = _extract_sitian_config(cfg)
    records = store or SiTianRecordsStore()
    stop_signal = stop_event or asyncio.Event()

    while not stop_signal.is_set():
        await SiTianRunOnce(sitian_cfg, store=records, llm_provider=llm_provider)
        runtime_states = await _SiTianLoadRuntimeStates(records, sitian_cfg, observed_at=_to_iso())
        sleep_seconds = _SiTianSleepSeconds(
            sitian_cfg=sitian_cfg,
            runtime_states=runtime_states,
            observed_at=_to_iso(),
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_signal.wait(), timeout=sleep_seconds)


async def SiTianReadState(
    *,
    store: SiTianRecordsStore | None = None,
) -> dict[str, JsonValue]:
    records = store or SiTianRecordsStore()
    await records.ensure_layout()
    runtime_states = await records.load_runtime_states()
    workspace_state = await records.load_workspace_state()
    suggestions = await records.load_latest_suggestions()
    summary = await records.load_latest_summary()
    return {
        "rootDir": str(records.root_dir),
        "runtimeStates": [state.to_dict() for state in runtime_states],
        "workspaceState": None if workspace_state is None else workspace_state.to_dict(),
        "latestSuggestions": suggestions,
        "latestSummary": summary,
    }


async def _SiTianLoadRuntimeStates(
    records: SiTianRecordsStore,
    sitian_cfg: SiTianConfig,
    *,
    observed_at: str,
) -> tuple[SiTianSourceRuntimeState, ...]:
    loaded_states = {state.source_id: state for state in await records.load_runtime_states()}
    merged: list[SiTianSourceRuntimeState] = []
    for source in sitian_cfg.sources:
        state = loaded_states.get(source.id)
        if state is None:
            merged.append(
                SiTianSourceRuntimeState(
                    source_id=source.id,
                    scan_interval_sec=_resolved_interval(source, sitian_cfg),
                    retry_backoff_sec=0,
                    next_run_at=observed_at,
                    status="idle",
                )
            )
            continue
        merged.append(
            SiTianSourceRuntimeState(
                source_id=source.id,
                scan_interval_sec=_resolved_interval(source, sitian_cfg),
                retry_backoff_sec=state.retry_backoff_sec,
                next_run_at=state.next_run_at,
                status=state.status,
                last_run_at=state.last_run_at,
                last_success_at=state.last_success_at,
                last_error_at=state.last_error_at,
                last_error=state.last_error,
            )
        )
    return tuple(merged)


def _SiTianSelectReadySources(
    sitian_cfg: SiTianConfig,
    runtime_states: tuple[SiTianSourceRuntimeState, ...],
    *,
    observed_at: str,
) -> tuple[SiTianSourceConfig, ...]:
    now = _parse_iso(observed_at)
    state_by_source = {state.source_id: state for state in runtime_states}
    ready: list[SiTianSourceConfig] = []
    for source in sitian_cfg.sources:
        state = state_by_source.get(source.id)
        if state is None:
            ready.append(source)
            continue
        if _parse_iso(state.next_run_at) <= now:
            ready.append(source)
    return tuple(ready)


def _SiTianAdvanceRuntimeSuccess(
    *,
    source: SiTianSourceConfig,
    runtime_state: SiTianSourceRuntimeState | None,
    observed_at: str,
    default_scan_interval_sec: int,
) -> SiTianSourceRuntimeState:
    interval = _resolved_interval_from_state_or_source(
        source,
        runtime_state=runtime_state,
        default_scan_interval_sec=default_scan_interval_sec,
    )
    next_run_at = _to_iso(_parse_iso(observed_at) + timedelta(seconds=interval))
    return SiTianSourceRuntimeState(
        source_id=source.id,
        scan_interval_sec=interval,
        retry_backoff_sec=0,
        next_run_at=next_run_at,
        status="idle",
        last_run_at=observed_at,
        last_success_at=observed_at,
        last_error_at=None,
        last_error=None,
    )


def _SiTianAdvanceRuntimeError(
    *,
    source: SiTianSourceConfig,
    runtime_state: SiTianSourceRuntimeState | None,
    observed_at: str,
    error: str,
    default_scan_interval_sec: int,
) -> SiTianSourceRuntimeState:
    interval = _resolved_interval_from_state_or_source(
        source,
        runtime_state=runtime_state,
        default_scan_interval_sec=default_scan_interval_sec,
    )
    current_backoff = runtime_state.retry_backoff_sec if runtime_state is not None else 0
    retry_backoff_sec = current_backoff * 2 if current_backoff else _DEFAULT_RETRY_BACKOFF_SEC
    next_run_at = _to_iso(
        _parse_iso(observed_at) + timedelta(seconds=max(interval, retry_backoff_sec))
    )
    return SiTianSourceRuntimeState(
        source_id=source.id,
        scan_interval_sec=interval,
        retry_backoff_sec=retry_backoff_sec,
        next_run_at=next_run_at,
        status="error",
        last_run_at=observed_at,
        last_success_at=None if runtime_state is None else runtime_state.last_success_at,
        last_error_at=observed_at,
        last_error=error,
    )


def _SiTianFinalizeRuntimeStates(
    sitian_cfg: SiTianConfig,
    *,
    runtime_by_source: dict[str, SiTianSourceRuntimeState],
    observed_at: str,
) -> tuple[SiTianSourceRuntimeState, ...]:
    finalized: list[SiTianSourceRuntimeState] = []
    for source in sitian_cfg.sources:
        state = runtime_by_source.get(source.id)
        if state is None:
            finalized.append(
                SiTianSourceRuntimeState(
                    source_id=source.id,
                    scan_interval_sec=_resolved_interval(source, sitian_cfg),
                    retry_backoff_sec=0,
                    next_run_at=observed_at,
                    status="idle",
                )
            )
            continue
        finalized.append(state)
    return tuple(finalized)


def _SiTianSleepSeconds(
    *,
    sitian_cfg: SiTianConfig,
    runtime_states: tuple[SiTianSourceRuntimeState, ...],
    observed_at: str,
) -> float:
    now = _parse_iso(observed_at)
    if not runtime_states:
        return float(sitian_cfg.idle_sleep_sec)
    soonest = min(_parse_iso(state.next_run_at) for state in runtime_states)
    delta = max((soonest - now).total_seconds(), 0.0)
    if delta == 0:
        return 0.1
    return min(delta, float(sitian_cfg.idle_sleep_sec))


def _resolved_interval(source: SiTianSourceConfig, sitian_cfg: SiTianConfig) -> int:
    return source.scan_interval_sec or sitian_cfg.default_scan_interval_sec


def _resolved_interval_from_state_or_source(
    source: SiTianSourceConfig,
    *,
    runtime_state: SiTianSourceRuntimeState | None,
    default_scan_interval_sec: int,
) -> int:
    if runtime_state is not None and runtime_state.scan_interval_sec > 0:
        return runtime_state.scan_interval_sec
    return source.scan_interval_sec or default_scan_interval_sec


def _extract_sitian_config(cfg: Config | SiTianConfig) -> SiTianConfig:
    if isinstance(cfg, SiTianConfig):
        return cfg
    return cfg.sitian


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _to_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(tz=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["SiTianReadState", "SiTianRunLoop", "SiTianRunOnce", "SiTianRunResult"]
