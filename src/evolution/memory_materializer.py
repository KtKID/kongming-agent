"""Accepted nutrient -> MEMORY.md materialization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from core.clock import now_epoch_ms
from core.contracts import EventSink
from evolution.models import EvolutionNutrient
from memory import ENTRY_DELIMITER, MemoryStore, MemoryWriteAction, execute_write

_MAX_MEMORY_ENTRIES = 3
_SENTENCE_SPLIT_RE = re.compile(r"(?:\r?\n+|[。！？!?；;]+|\.(?=\s|$))")
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_BULLET_RE = re.compile(r"^[\-\*\u2022\u2023\u25E6\u2043\u2219]+")
_LEADING_ORDER_RE = re.compile(r"^\(?\d+[.)]\s*")
_TRAILING_PUNCT_RE = re.compile(r"[。！？!?；;,.，、：:]+$")


@dataclass(frozen=True)
class MaterializedMemory:
    source_nutrient_id: str
    normalized_content: str
    entries: tuple[str, ...]


@dataclass(frozen=True)
class MemoryMaterializationOutcome:
    source_nutrient_id: str
    status: str
    path: str
    mode: str
    applied_at_ms: int
    normalized_content: str
    entries: tuple[str, ...]
    written_entries: tuple[str, ...]
    skipped_entries: tuple[str, ...]
    error: str | None = None


class MemoryMaterializer:
    """Materialize accepted nutrient into workspace MEMORY.md."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        event_sinks: Sequence[EventSink] = (),
        run_id: str = "evolution-memory-apply",
    ) -> None:
        self._store = store
        self._event_sinks = tuple(event_sinks)
        self._run_id = run_id

    async def materialize(self, nutrient: EvolutionNutrient) -> MemoryMaterializationOutcome:
        materialized = materialize_memory_entries(nutrient)
        await self._store.load_from_disk()
        existing_entries = self._store.memory_entries
        existing_normalized = {
            normalize_memory_content(entry.content) for entry in existing_entries
        }

        new_entries: list[str] = []
        skipped_entries: list[str] = []
        for entry in materialized.entries:
            normalized = normalize_memory_content(entry)
            if normalized in existing_normalized:
                skipped_entries.append(entry)
                continue
            new_entries.append(entry)
            existing_normalized.add(normalized)

        applied_at_ms = now_epoch_ms()
        memory_path = str(self._store.memory_file_path)
        if not new_entries:
            return MemoryMaterializationOutcome(
                source_nutrient_id=nutrient.nutrient_id,
                status="skipped",
                path=memory_path,
                mode="append",
                applied_at_ms=applied_at_ms,
                normalized_content=materialized.normalized_content,
                entries=materialized.entries,
                written_entries=(),
                skipped_entries=tuple(skipped_entries),
            )

        result = await execute_write(
            self._store.memory_dir,
            MemoryWriteAction(
                action="add",
                target="memory",
                content=ENTRY_DELIMITER.join(new_entries),
                reason=f"self-evolution nutrient {nutrient.nutrient_id}",
            ),
            event_sinks=self._event_sinks,
            run_id=self._run_id,
        )
        if result.ok:
            current = await self._store.read_target("memory")
            self._store.refresh_entries_for("memory", current)
            return MemoryMaterializationOutcome(
                source_nutrient_id=nutrient.nutrient_id,
                status="written",
                path=result.path,
                mode="append",
                applied_at_ms=applied_at_ms,
                normalized_content=materialized.normalized_content,
                entries=materialized.entries,
                written_entries=tuple(new_entries),
                skipped_entries=tuple(skipped_entries),
            )
        status = "skipped" if result.status == "skipped" else "failed"
        return MemoryMaterializationOutcome(
            source_nutrient_id=nutrient.nutrient_id,
            status=status,
            path=result.path,
            mode="append",
            applied_at_ms=applied_at_ms,
            normalized_content=materialized.normalized_content,
            entries=materialized.entries,
            written_entries=(),
            skipped_entries=tuple(materialized.entries if status == "skipped" else skipped_entries),
            error=None if status == "skipped" else result.message,
        )


def materialize_memory_entries(nutrient: EvolutionNutrient) -> MaterializedMemory:
    unique_entries: list[str] = []
    seen: set[str] = set()
    for candidate in _iter_atomic_candidates(nutrient):
        normalized = normalize_memory_content(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_entries.append(candidate)
        if len(unique_entries) >= _MAX_MEMORY_ENTRIES:
            break
    if not unique_entries:
        fallback = (
            _clean_candidate(nutrient.summary)
            or _clean_candidate(nutrient.content)
            or nutrient.title
        )
        unique_entries.append(fallback)
        seen.add(normalize_memory_content(fallback))
    return MaterializedMemory(
        source_nutrient_id=nutrient.nutrient_id,
        normalized_content=" | ".join(normalize_memory_content(entry) for entry in unique_entries),
        entries=tuple(unique_entries),
    )


def normalize_memory_content(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    lines: list[str] = []
    for line in normalized.splitlines():
        stripped = _LEADING_BULLET_RE.sub("", line.strip())
        stripped = _LEADING_ORDER_RE.sub("", stripped)
        stripped = _TRAILING_PUNCT_RE.sub("", stripped)
        if stripped:
            lines.append(stripped)
    merged = " ".join(lines)
    return _WHITESPACE_RE.sub(" ", merged).strip()


def _iter_atomic_candidates(nutrient: EvolutionNutrient) -> tuple[str, ...]:
    candidates: list[str] = []
    for raw in (nutrient.summary, nutrient.content):
        for chunk in _SENTENCE_SPLIT_RE.split(raw):
            cleaned = _clean_candidate(chunk)
            if cleaned:
                candidates.append(cleaned)
    return tuple(candidates)


def _clean_candidate(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    normalized = _LEADING_BULLET_RE.sub("", normalized)
    normalized = _LEADING_ORDER_RE.sub("", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    normalized = _TRAILING_PUNCT_RE.sub("", normalized).strip()
    if len(normalized) < 6:
        return None
    return normalized
