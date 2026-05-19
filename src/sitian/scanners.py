"""
司天扫描引擎——按 source kind 分派 4 种扫描实现。

Role:
    接收 SiTianSourceConfig，按 kind（generic_channel / claude_project /
    codex_project / claude_workspace）分派到对应扫描函数，
    返回 SiTianScanBatch（含 SiTianObservation 列表 + 耗时审计）。

Owns:
    - 4 种 kind 的扫描逻辑（文件 rglob / claude session 关联 / workspace 展开）
    - include/exclude 过滤、mtime 窗口、top N 裁剪
    - scan 耗时审计字段（duration_ms）

Does not own:
    - 全局项目列表（global_scanner.py）
    - session 消息读取（session_reader.py）
    - 持久化（store.py）
    - 定时调度（service.py）

Called by:
    - service.py（SiTianRunOnce 编排层）

Key outputs:
    - SiTianScanSource(source_config) → SiTianScanBatch
    - SiTianObservation（kind / path / data / observed_at）

Change risks:
    - kind 枚举变化需同步 config.py 的 SiTianSourceConfig.kind
    - observation 字段变化会影响 suggestions 归并和 analyzer 输入
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sitian.config import SiTianScannerConfig, SiTianSourceConfig
from sitian.global_scanner import list_claude_projects_global, list_codex_projects_global
from sitian.models import SiTianObservation
from sitian.session_reader import read_claude_session_tail

_log = logging.getLogger("sitian.scanners")

_MAX_ARTIFACT_OBSERVATIONS = 20
_MAX_SESSION_OBSERVATIONS = 20
_MAX_SCAN_FILES = 2000
_DEFAULT_CLAUDE_WORKSPACE_TOP_N = 10
# 单个 project 在 project obs.recentSessions 中最多展开多少条 session
_MAX_RECENT_SESSIONS_PER_PROJECT = 20


@dataclass(frozen=True)
class SiTianScanBatch:
    source_id: str
    source_kind: str
    observed_at: str
    observations: tuple[SiTianObservation, ...]


async def SiTianScanSource(
    source: SiTianSourceConfig,
    *,
    observed_at: str | None = None,
    scanner_config: SiTianScannerConfig | None = None,
) -> SiTianScanBatch:
    resolved_observed_at = observed_at or _utc_now_iso()
    effective_scanner_config = scanner_config or SiTianScannerConfig()
    if source.kind == "generic_channel":
        observations = _SiTianScanGenericChannel(
            source,
            observed_at=resolved_observed_at,
            scanner_config=effective_scanner_config,
        )
    elif source.kind == "claude_project":
        observations = _SiTianScanClaudeProject(
            source,
            observed_at=resolved_observed_at,
            scanner_config=effective_scanner_config,
        )
    elif source.kind == "codex_project":
        observations = _SiTianScanCodexProject(
            source,
            observed_at=resolved_observed_at,
            scanner_config=effective_scanner_config,
        )
    elif source.kind == "claude_workspace":
        observations = _SiTianScanClaudeWorkspace(
            source,
            observed_at=resolved_observed_at,
            scanner_config=effective_scanner_config,
        )
    else:  # pragma: no cover - defensive
        raise ValueError(f"unsupported SiTian source kind: {source.kind}")
    return SiTianScanBatch(
        source_id=source.id,
        source_kind=source.kind,
        observed_at=resolved_observed_at,
        observations=tuple(observations),
    )


def _SiTianScanGenericChannel(
    source: SiTianSourceConfig,
    *,
    observed_at: str,
    scanner_config: SiTianScannerConfig,
) -> list[SiTianObservation]:
    _ = scanner_config  # Wave 2 will consume this; placeholder for current wave
    root_path = Path(source.path).expanduser().resolve()
    recent_files = _collect_recent_files(
        root_path,
        include=tuple(source.include),
        exclude=tuple(source.exclude),
    )
    return _build_file_observations(
        source=source,
        root_path=root_path,
        observed_at=observed_at,
        recent_files=recent_files,
        project_payload={"path": str(root_path), "channelKind": "generic_channel"},
    )


def _SiTianScanClaudeProject(
    source: SiTianSourceConfig,
    *,
    observed_at: str,
    scanner_config: SiTianScannerConfig,
) -> list[SiTianObservation]:
    project_path = Path(source.path).expanduser().resolve()
    observations = _build_file_observations(
        source=source,
        root_path=project_path,
        observed_at=observed_at,
        recent_files=_collect_recent_files(
            project_path,
            include=tuple(source.include),
            exclude=tuple(source.exclude),
        ),
        project_payload={"path": str(project_path), "projectKind": "claude_project"},
    )

    threads = _load_claude_threads_for_project(project_path)
    cutoff_iso = _compute_window_cutoff_iso(observed_at, scanner_config.recent_session_window_days)
    if cutoff_iso is not None:
        threads = [t for t in threads if t["lastModified"] >= cutoff_iso]

    for thread in threads[:_MAX_SESSION_OBSERVATIONS]:
        tail = read_claude_session_tail(
            Path(thread["file"]),
            user_count=scanner_config.session_recent_user_messages,
            assistant_count=scanner_config.session_recent_assistant_messages,
            max_chars=scanner_config.session_message_max_chars,
        )
        enriched_payload = {
            **thread,
            "recentUserMessages": tail["users"],
            "recentAssistantMessages": tail["assistants"],
        }
        observations.append(
            _observation(
                source=source,
                observed_at=observed_at,
                entity_type="thread",
                entity_key=thread["thread_id"],
                payload=enriched_payload,
                evidence_refs=(thread["file"],),
            )
        )
    return observations


def _SiTianScanCodexProject(
    source: SiTianSourceConfig,
    *,
    observed_at: str,
    scanner_config: SiTianScannerConfig,
) -> list[SiTianObservation]:
    _ = scanner_config  # Wave 2 will consume this; placeholder for current wave
    project_path = Path(source.path).expanduser().resolve()
    observations = _build_file_observations(
        source=source,
        root_path=project_path,
        observed_at=observed_at,
        recent_files=_collect_recent_files(
            project_path,
            include=tuple(source.include),
            exclude=tuple(source.exclude),
        ),
        project_payload={"path": str(project_path), "projectKind": "codex_project"},
    )

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    for project in list_codex_projects_global(codex_home=codex_home):
        if os.path.realpath(project.cwd) != os.path.realpath(str(project_path)):
            continue
        for session in project.sessions[:_MAX_SESSION_OBSERVATIONS]:
            observations.append(
                _observation(
                    source=source,
                    observed_at=observed_at,
                    entity_type="thread",
                    entity_key=session.session_id,
                    payload={
                        "threadId": session.session_id,
                        "title": session.title,
                        "cwd": session.cwd,
                        "lastModified": _timestamp_to_iso(session.last_modified),
                        "messageCount": session.message_count,
                        "rolloutPath": session.rollout_path,
                        "cliVersion": session.cli_version,
                        "provider": session.provider,
                    },
                    evidence_refs=(session.rollout_path,),
                )
            )
        break
    return observations


def _SiTianScanClaudeWorkspace(
    source: SiTianSourceConfig,
    *,
    observed_at: str,
    scanner_config: SiTianScannerConfig,
) -> list[SiTianObservation]:
    claude_home = _resolve_claude_home_from_path(source.path)
    top_n = source.top_n or _DEFAULT_CLAUDE_WORKSPACE_TOP_N

    projects = list_claude_projects_global(claude_home=claude_home)[:top_n]
    cutoff_ts = _compute_window_cutoff_ts(observed_at, scanner_config.recent_session_window_days)

    observations: list[SiTianObservation] = []
    latest_modified_iso = (
        _timestamp_to_iso(max(s.last_modified for s in projects[0].sessions))
        if projects and projects[0].sessions
        else None
    )
    observations.append(
        _observation(
            source=source,
            observed_at=observed_at,
            entity_type="status",
            entity_key=str(claude_home),
            payload={
                "claudeHome": str(claude_home),
                "channelKind": "claude_workspace",
                "projectCount": len(projects),
                "topN": top_n,
                "latestModifiedAt": latest_modified_iso,
                "sourceTags": list(source.tags),
            },
            evidence_refs=(str(claude_home / "projects"),),
        )
    )

    for rank, project in enumerate(projects, start=1):
        latest_session_mtime = (
            max(s.last_modified for s in project.sessions) if project.sessions else 0.0
        )
        windowed_sessions = (
            project.sessions
            if cutoff_ts is None
            else [s for s in project.sessions if s.last_modified >= cutoff_ts]
        )
        recent_sessions_payload: list[dict[str, Any]] = []
        for session in windowed_sessions[:_MAX_RECENT_SESSIONS_PER_PROJECT]:
            jsonl_path = claude_home / "projects" / project.name / f"{session.thread_id}.jsonl"
            tail = read_claude_session_tail(
                jsonl_path,
                user_count=scanner_config.session_recent_user_messages,
                assistant_count=scanner_config.session_recent_assistant_messages,
                max_chars=scanner_config.session_message_max_chars,
            )
            recent_sessions_payload.append(
                {
                    "threadId": session.thread_id,
                    "lastModified": _timestamp_to_iso(session.last_modified),
                    "recentUserMessages": tail["users"],
                    "recentAssistantMessages": tail["assistants"],
                }
            )

        observations.append(
            _observation(
                source=source,
                observed_at=observed_at,
                entity_type="project",
                entity_key=project.cwd,
                payload={
                    "rank": rank,
                    "projectDirName": project.name,
                    "cwd": project.cwd,
                    "displayName": project.display_name,
                    "sessionCount": len(project.sessions),
                    "lastModifiedAt": _timestamp_to_iso(latest_session_mtime),
                    "recentSessions": recent_sessions_payload,
                },
                evidence_refs=(str(claude_home / "projects" / project.name),),
            )
        )
        if project.sessions:
            latest_session = project.sessions[0]
            latest_jsonl = (
                claude_home / "projects" / project.name / f"{latest_session.thread_id}.jsonl"
            )
            tail = read_claude_session_tail(
                latest_jsonl,
                user_count=scanner_config.session_recent_user_messages,
                assistant_count=scanner_config.session_recent_assistant_messages,
                max_chars=scanner_config.session_message_max_chars,
            )
            observations.append(
                _observation(
                    source=source,
                    observed_at=observed_at,
                    entity_type="thread",
                    entity_key=latest_session.thread_id,
                    payload={
                        "threadId": latest_session.thread_id,
                        "title": latest_session.title,
                        "cwd": project.cwd,
                        "displayName": project.display_name,
                        "messageCount": latest_session.message_count,
                        "lastModifiedAt": _timestamp_to_iso(latest_session.last_modified),
                        "recentUserMessages": tail["users"],
                        "recentAssistantMessages": tail["assistants"],
                    },
                    evidence_refs=(str(latest_jsonl),),
                )
            )
    return observations


def _resolve_claude_home_from_path(raw_path: str) -> Path:
    """Allow path 既可以填 ``~/.claude`` 也可以填 ``~/.claude/projects``。

    list_projects 期待 claude_home（含 ``projects/`` 子目录），所以末尾若是
    ``projects`` 自动回退到父目录。
    """
    resolved = Path(raw_path).expanduser()
    if resolved.name == "projects":
        return resolved.parent
    return resolved


def _build_file_observations(
    *,
    source: SiTianSourceConfig,
    root_path: Path,
    observed_at: str,
    recent_files: list[dict[str, Any]],
    project_payload: dict[str, Any],
) -> list[SiTianObservation]:
    observations: list[SiTianObservation] = []
    latest_modified = recent_files[0]["modifiedAt"] if recent_files else None
    observations.append(
        _observation(
            source=source,
            observed_at=observed_at,
            entity_type="status",
            entity_key=str(root_path),
            payload={
                **project_payload,
                "fileCount": len(recent_files),
                "latestModifiedAt": latest_modified,
                "sourceTags": list(source.tags),
            },
            evidence_refs=(str(root_path),),
        )
    )
    for file_info in recent_files[:_MAX_ARTIFACT_OBSERVATIONS]:
        observations.append(
            _observation(
                source=source,
                observed_at=observed_at,
                entity_type="artifact",
                entity_key=file_info["relativePath"],
                payload=file_info,
                evidence_refs=(file_info["absolutePath"],),
            )
        )
    return observations


def _collect_recent_files(
    root_path: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not root_path.exists():
        raise FileNotFoundError(f"SiTian source path does not exist: {root_path}")

    files: list[dict[str, Any]] = []
    if root_path.is_file():
        return [_file_info(root_path, root_path.parent)]

    scanned = 0
    for candidate in root_path.rglob("*"):
        if scanned >= _MAX_SCAN_FILES:
            break
        scanned += 1
        if not candidate.is_file():
            continue
        relative_path = candidate.relative_to(root_path).as_posix()
        if not _matches_filters(relative_path, include=include, exclude=exclude):
            continue
        files.append(_file_info(candidate, root_path))

    files.sort(key=lambda item: (item["modifiedAt"], item["relativePath"]), reverse=True)
    return files[:_MAX_ARTIFACT_OBSERVATIONS]


def _matches_filters(
    relative_path: str,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> bool:
    if include and not any(fnmatch.fnmatch(relative_path, pattern) for pattern in include):
        return False
    return not (exclude and any(fnmatch.fnmatch(relative_path, pattern) for pattern in exclude))


def _file_info(path: Path, root_path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "relativePath": path.relative_to(root_path).as_posix(),
        "absolutePath": str(path.resolve()),
        "suffix": path.suffix,
        "size": stat.st_size,
        "modifiedAt": _timestamp_to_iso(stat.st_mtime),
    }


def _load_claude_threads_for_project(project_path: Path) -> list[dict[str, Any]]:
    claude_home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")).expanduser()
    projects_dir = claude_home / "projects"
    results: list[dict[str, Any]] = []
    if not projects_dir.is_dir():
        return results

    wanted_path = os.path.realpath(str(project_path))
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if session_file.name.startswith("agent-"):
                continue
            thread = _parse_claude_session_file(session_file)
            if thread is None:
                continue
            if os.path.realpath(thread["cwd"]) != wanted_path:
                continue
            results.append(thread)

    results.sort(key=lambda item: item["lastModified"], reverse=True)
    return results


def _parse_claude_session_file(session_file: Path) -> dict[str, Any] | None:
    try:
        stat = session_file.stat()
    except OSError:
        return None

    title = "(empty session)"
    cwd = ""
    message_count = 0
    saw_valid_json = False
    try:
        with session_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                message_count += 1
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                saw_valid_json = True
                if not cwd:
                    candidate = payload.get("cwd")
                    if isinstance(candidate, str) and candidate.strip():
                        cwd = str(Path(candidate).expanduser())
                if title == "(empty session)":
                    candidate_title = _extract_claude_title(payload)
                    if candidate_title:
                        title = candidate_title
    except OSError:
        return None

    if message_count == 0 or not saw_valid_json or not cwd:
        return None

    return {
        "threadId": session_file.stem,
        "thread_id": session_file.stem,
        "title": title,
        "cwd": cwd,
        "lastModified": _timestamp_to_iso(stat.st_mtime),
        "messageCount": message_count,
        "file": str(session_file.resolve()),
    }


def _extract_claude_title(payload: dict[str, Any]) -> str | None:
    if payload.get("type") != "user":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    cleaned = " ".join(content.split())
    return cleaned[:60] if cleaned else None


def _observation(
    *,
    source: SiTianSourceConfig,
    observed_at: str,
    entity_type: str,
    entity_key: str,
    payload: dict[str, Any],
    evidence_refs: tuple[str, ...] = (),
) -> SiTianObservation:
    observation_id = _hash_id(
        source.id,
        source.kind,
        entity_type,
        entity_key,
        observed_at,
    )
    return SiTianObservation(
        id=observation_id,
        source_id=source.id,
        source_kind=source.kind,
        observed_at=observed_at,
        entity_type=entity_type,
        entity_key=entity_key,
        payload=payload,
        evidence_refs=evidence_refs,
    )


def _hash_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"sitian-{digest[:16]}"


def _timestamp_to_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_to_dt(value: str) -> datetime:
    """ISO 8601（含尾部 ``Z``）解析回 ``datetime``，带 UTC tz。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _compute_window_cutoff_ts(observed_at: str, window_days: int) -> float | None:
    """返回 ``observed_at - window_days`` 的 Unix timestamp；``window_days<=0`` 时返回 None（不过滤）。"""
    if window_days <= 0:
        return None
    try:
        now_dt = _parse_iso_to_dt(observed_at)
    except ValueError:
        _log.warning(
            "_compute_window_cutoff_ts: invalid observed_at=%r, skipping window filter",
            observed_at,
        )
        return None
    return now_dt.timestamp() - window_days * 86400.0


def _compute_window_cutoff_iso(observed_at: str, window_days: int) -> str | None:
    """同上但返回 ISO 字符串，用于跟 payload 中已是 ISO 形态的 ``lastModified`` 字典序比较。

    ISO 8601 UTC（``Z`` 后缀）字符串字典序与时间序一致。
    """
    cutoff_ts = _compute_window_cutoff_ts(observed_at, window_days)
    if cutoff_ts is None:
        return None
    return _timestamp_to_iso(cutoff_ts)


__all__ = ["SiTianScanBatch", "SiTianScanSource"]
