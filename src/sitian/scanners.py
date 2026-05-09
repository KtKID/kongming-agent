from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sitian import SiTianSourceConfig
from sitian.models import SiTianObservation
from web.codex.projects_scanner import list_codex_projects

_MAX_ARTIFACT_OBSERVATIONS = 20
_MAX_SESSION_OBSERVATIONS = 20
_MAX_SCAN_FILES = 2000


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
) -> SiTianScanBatch:
    resolved_observed_at = observed_at or _utc_now_iso()
    if source.kind == "generic_channel":
        observations = _SiTianScanGenericChannel(source, observed_at=resolved_observed_at)
    elif source.kind == "claude_project":
        observations = _SiTianScanClaudeProject(source, observed_at=resolved_observed_at)
    elif source.kind == "codex_project":
        observations = _SiTianScanCodexProject(source, observed_at=resolved_observed_at)
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
) -> list[SiTianObservation]:
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

    for thread in _load_claude_threads_for_project(project_path)[:_MAX_SESSION_OBSERVATIONS]:
        observations.append(
            _observation(
                source=source,
                observed_at=observed_at,
                entity_type="thread",
                entity_key=thread["thread_id"],
                payload=thread,
                evidence_refs=(thread["file"],),
            )
        )
    return observations


def _SiTianScanCodexProject(
    source: SiTianSourceConfig,
    *,
    observed_at: str,
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
        project_payload={"path": str(project_path), "projectKind": "codex_project"},
    )

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    for project in list_codex_projects(codex_home=codex_home):
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


__all__ = ["SiTianScanBatch", "SiTianScanSource"]
