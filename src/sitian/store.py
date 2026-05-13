from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from sitian.models import (
    JsonValue,
    SiTianObservation,
    SiTianSourceRuntimeState,
    SiTianWorkItem,
    SiTianWorkspaceState,
)

_CONFIG_SNAPSHOT_FILENAME = "config.snapshot.json"
_OBSERVATIONS_FILENAME = "observations.jsonl"
_RUNTIME_STATE_FILENAME = "runtime_state.json"
_WORKSPACE_STATE_FILENAME = "workspace_state.json"
_LATEST_SUGGESTIONS_FILENAME = "latest_suggestions.json"
_LATEST_SUMMARY_FILENAME = "latest_summary.md"
_WORK_ITEMS_DIRNAME = "work_items"
_SCANS_DIRNAME = "scans"


def resolve_sitian_root(root_dir: str | Path | None = None) -> Path:
    if root_dir is None:
        return (Path.home() / ".kongming" / "SiTian").expanduser().resolve()
    return Path(root_dir).expanduser().resolve()


class SiTianRecordsStore:
    def __init__(self, root_dir: str | Path | None = None) -> None:
        self._root_dir = resolve_sitian_root(root_dir)
        self._observations_path = self._root_dir / _OBSERVATIONS_FILENAME
        self._runtime_state_path = self._root_dir / _RUNTIME_STATE_FILENAME
        self._workspace_state_path = self._root_dir / _WORKSPACE_STATE_FILENAME
        self._latest_suggestions_path = self._root_dir / _LATEST_SUGGESTIONS_FILENAME
        self._latest_summary_path = self._root_dir / _LATEST_SUMMARY_FILENAME
        self._config_snapshot_path = self._root_dir / _CONFIG_SNAPSHOT_FILENAME
        self._work_items_dir = self._root_dir / _WORK_ITEMS_DIRNAME
        self._scans_dir = self._root_dir / _SCANS_DIRNAME
        self._lock = asyncio.Lock()

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    async def ensure_layout(self) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            return self._root_dir

    async def save_config_snapshot(self, config_snapshot: dict[str, JsonValue]) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(
                self._atomic_write_json_sync, self._config_snapshot_path, config_snapshot
            )
            return self._config_snapshot_path

    async def load_config_snapshot(self) -> dict[str, JsonValue] | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._read_json_optional_sync, self._config_snapshot_path
            )

    async def append_observations(
        self,
        observations: list[SiTianObservation] | tuple[SiTianObservation, ...],
    ) -> int:
        if not observations:
            return 0
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(self._append_observations_sync, observations)
            return len(observations)

    async def load_observations(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[SiTianObservation, ...]:
        async with self._lock:
            rows = await asyncio.to_thread(self._load_jsonl_sync, self._observations_path, limit)
        return tuple(SiTianObservation.from_dict(row) for row in rows)

    async def save_runtime_states(
        self,
        runtime_states: list[SiTianSourceRuntimeState] | tuple[SiTianSourceRuntimeState, ...],
    ) -> Path:
        payload = {
            "version": "v1",
            "updatedAt": _utc_now_iso(),
            "items": [state.to_dict() for state in runtime_states],
        }
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(self._atomic_write_json_sync, self._runtime_state_path, payload)
            return self._runtime_state_path

    async def load_runtime_states(self) -> tuple[SiTianSourceRuntimeState, ...]:
        async with self._lock:
            raw = await asyncio.to_thread(self._read_json_optional_sync, self._runtime_state_path)
        items = [] if raw is None else raw.get("items", [])
        return tuple(SiTianSourceRuntimeState.from_dict(dict(item)) for item in items)

    async def save_workspace_state(self, workspace_state: SiTianWorkspaceState) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(
                self._atomic_write_json_sync,
                self._workspace_state_path,
                workspace_state.to_dict(),
            )
            return self._workspace_state_path

    async def load_workspace_state(self) -> SiTianWorkspaceState | None:
        async with self._lock:
            raw = await asyncio.to_thread(self._read_json_optional_sync, self._workspace_state_path)
        if raw is None:
            return None
        return SiTianWorkspaceState.from_dict(raw)

    async def save_work_items(
        self,
        work_items: list[SiTianWorkItem] | tuple[SiTianWorkItem, ...],
    ) -> tuple[Path, ...]:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            written_paths = await asyncio.to_thread(self._upsert_work_items_sync, work_items)
            return written_paths

    async def upsert_work_item(self, work_item: SiTianWorkItem) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            path = self._work_item_path(work_item.id)
            await asyncio.to_thread(self._atomic_write_json_sync, path, work_item.to_dict())
            return path

    async def load_work_items(self) -> tuple[SiTianWorkItem, ...]:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            paths = await asyncio.to_thread(self._list_json_files_sync, self._work_items_dir)
            rows = await asyncio.to_thread(self._load_many_json_sync, paths)
        items = [SiTianWorkItem.from_dict(row) for row in rows]
        items.sort(key=lambda item: (item.updated_at, item.id))
        return tuple(items)

    async def save_latest_suggestions(self, suggestions: JsonValue) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(
                self._atomic_write_json_sync,
                self._latest_suggestions_path,
                suggestions,
            )
            return self._latest_suggestions_path

    async def load_latest_suggestions(self) -> JsonValue | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._read_json_optional_sync, self._latest_suggestions_path
            )

    async def save_latest_summary(self, summary_markdown: str) -> Path:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(
                self._atomic_write_text_sync,
                self._latest_summary_path,
                summary_markdown,
            )
            return self._latest_summary_path

    async def load_latest_summary(self) -> str | None:
        async with self._lock:
            return await asyncio.to_thread(self._read_text_optional_sync, self._latest_summary_path)

    async def write_scan_snapshot(
        self,
        snapshot: dict[str, JsonValue],
        *,
        scan_id: str | None = None,
    ) -> Path:
        resolved_scan_id = scan_id or _resolve_scan_id(snapshot)
        path = self._scan_snapshot_path(resolved_scan_id)
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            await asyncio.to_thread(self._atomic_write_json_sync, path, snapshot)
            return path

    async def load_scan_snapshot(self, scan_id: str) -> dict[str, JsonValue] | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._read_json_optional_sync,
                self._scan_snapshot_path(scan_id),
            )

    async def list_scan_snapshots(
        self, *, limit: int | None = None
    ) -> tuple[dict[str, JsonValue], ...]:
        async with self._lock:
            await asyncio.to_thread(self._ensure_layout_sync)
            paths = await asyncio.to_thread(self._list_json_files_sync, self._scans_dir)
            if limit is not None and limit >= 0:
                paths = paths[-limit:]
            rows = await asyncio.to_thread(self._load_many_json_sync, paths)
            return tuple(rows)

    def _ensure_layout_sync(self) -> None:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._work_items_dir.mkdir(parents=True, exist_ok=True)
        self._scans_dir.mkdir(parents=True, exist_ok=True)
        if not self._observations_path.exists():
            self._observations_path.touch()
        if not self._workspace_state_path.exists():
            empty_state = SiTianWorkspaceState.empty(updated_at=_utc_now_iso())
            self._atomic_write_json_sync(self._workspace_state_path, empty_state.to_dict())

    def _append_observations_sync(
        self,
        observations: list[SiTianObservation] | tuple[SiTianObservation, ...],
    ) -> None:
        lines = [
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) for item in observations
        ]
        with self._observations_path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _upsert_work_items_sync(
        self,
        work_items: list[SiTianWorkItem] | tuple[SiTianWorkItem, ...],
    ) -> tuple[Path, ...]:
        written_paths: list[Path] = []
        for item in work_items:
            path = self._work_item_path(item.id)
            self._atomic_write_json_sync(path, item.to_dict())
            written_paths.append(path)
        return tuple(written_paths)

    def _work_item_path(self, work_item_id: str) -> Path:
        encoded = quote(work_item_id, safe="")
        return self._work_items_dir / f"{encoded}.json"

    def _scan_snapshot_path(self, scan_id: str) -> Path:
        normalized = _normalize_scan_id(scan_id)
        return self._scans_dir / f"scan-{normalized}.json"

    @staticmethod
    def _atomic_write_json_sync(path: Path, payload: JsonValue) -> None:
        SiTianRecordsStore._atomic_write_text_sync(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    @staticmethod
    def _atomic_write_text_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".sitian_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def _read_json_optional_sync(path: Path) -> JsonValue | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _read_text_optional_sync(path: Path) -> str | None:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _load_jsonl_sync(path: Path, limit: int | None) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    rows.append(parsed)
        if limit is not None and limit >= 0:
            return rows[-limit:]
        return rows

    @staticmethod
    def _list_json_files_sync(directory: Path) -> list[Path]:
        if not directory.exists():
            return []
        return sorted(path for path in directory.glob("*.json") if path.is_file())

    @staticmethod
    def _load_many_json_sync(paths: list[Path]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in paths:
            parsed = SiTianRecordsStore._read_json_optional_sync(path)
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows


def _resolve_scan_id(snapshot: dict[str, JsonValue]) -> str:
    candidate = snapshot.get("scanId")
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    observed_at = snapshot.get("observedAt")
    if isinstance(observed_at, str) and observed_at.strip():
        return observed_at
    return _utc_now_compact()


def _normalize_scan_id(scan_id: str) -> str:
    scan_id = scan_id.strip()
    if not scan_id:
        return _utc_now_compact()
    if scan_id.startswith("scan-"):
        scan_id = scan_id[5:]
    if scan_id.endswith(".json"):
        scan_id = scan_id[:-5]
    if "/" in scan_id or "\\" in scan_id:
        raise ValueError(f"invalid scan_id: {scan_id}")
    return scan_id.replace(":", "").replace(".", "-")


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now_compact() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def decode_work_item_filename(path: str | Path) -> str:
    return unquote(Path(path).stem)


__all__ = [
    "SiTianRecordsStore",
    "decode_work_item_filename",
    "resolve_sitian_root",
]
