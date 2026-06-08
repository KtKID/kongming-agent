"""Log tail-read service -- read, parse and filter the trailing portion of a log file.

Responsibilities
----------------
1. Read the tail of a log file (bounded by *max_bytes*) without loading the
   entire file into memory.
2. Return an array of lines, total bytes, read bytes, a truncation flag,
   and the file mtime.
3. Optionally parse each line as JSON (JSONL); parse failures are recorded
   per-line and do not affect other lines.
4. If the file does not exist, return ``exists=False`` with an empty line
   array -- **never raise**.

Constraints
-----------
- No import of ``web.routers`` or other handler-layer modules.
- No import of ``safety``, ``tools``, ``host`` or other kernel modules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hosts.web.dashboard.logs.registry import LogSourceRegistry, ResolvedLogSource
from hosts.web.protocol.log_dto import LogLineDTO, LogReadResponseDTO, LogSourceDTO

# ---------------------------------------------------------------------------
# Parameter limits
# ---------------------------------------------------------------------------

_DEFAULT_TAIL_LINES: int = 500
_MAX_TAIL_LINES: int = 5000

_DEFAULT_MAX_BYTES: int = 524288  # 512 KiB
_MAX_MAX_BYTES: int = 5242880  # 5 MiB

_MAX_QUERY_LEN: int = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _resolved_to_dto(source: ResolvedLogSource) -> LogSourceDTO:
    """Convert a :class:`ResolvedLogSource` dataclass to a :class:`LogSourceDTO`."""
    return LogSourceDTO(
        type=source.type,
        label=source.label,
        format=source.format,
        description=source.description,
        path=source.path,
        exists=source.exists,
        size_bytes=source.size_bytes,
        updated_at_ms=source.updated_at_ms,
    )


# ---------------------------------------------------------------------------
# LogReadService
# ---------------------------------------------------------------------------


class LogReadService:
    """Read, parse and filter the trailing portion of a log file."""

    def __init__(self, registry: LogSourceRegistry) -> None:
        self._registry = registry

    # -- public API ----------------------------------------------------------

    def read_tail(
        self,
        type: str,
        tail_lines: int = _DEFAULT_TAIL_LINES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        query: str = "",
    ) -> LogReadResponseDTO:
        """Read the tail of the log source identified by *type*.

        Steps:
            1. ``registry.get_source(type)`` → :class:`ResolvedLogSource`.
            2. If ``not source.exists`` → return empty lines.
            3. Seek to file tail and read up to *max_bytes*.
            4. ``splitlines()`` and keep the last *tail_lines*.
            5. If *query* is non-empty, case-insensitive filter.
            6. Parse each line as JSON where possible.
            7. Assemble and return :class:`LogReadResponseDTO`.
        """
        source = self._registry.get_source(type)
        source_dto = _resolved_to_dto(source)

        # File missing → empty response.
        if not source.exists:
            return LogReadResponseDTO(
                source=source_dto,
                lines=[],
                truncated=False,
                read_bytes=0,
                total_bytes=None,
            )

        path = Path(source.path)

        # Clamp parameters.
        tail_lines = _clamp(tail_lines, 1, _MAX_TAIL_LINES)
        max_bytes = _clamp(max_bytes, 1, _MAX_MAX_BYTES)
        if len(query) > _MAX_QUERY_LEN:
            query = query[:_MAX_QUERY_LEN]

        try:
            raw_text, truncated, read_bytes, file_size = self._read_tail_bytes(path, max_bytes)
        except FileNotFoundError:
            # File disappeared between stat and read.
            return LogReadResponseDTO(
                source=LogSourceDTO(
                    type=source.type,
                    label=source.label,
                    format=source.format,
                    description=source.description,
                    path=source.path,
                    exists=False,
                    size_bytes=None,
                    updated_at_ms=None,
                ),
                lines=[],
                truncated=False,
                read_bytes=0,
                total_bytes=None,
            )

        lines = self._parse_lines(raw_text)

        # Filter by query (case-insensitive).
        if query:
            query_lower = query.lower()
            lines = [ln for ln in lines if query_lower in ln.raw.lower()]

        # Keep only the last *tail_lines* lines after filtering.
        if len(lines) > tail_lines:
            lines = lines[-tail_lines:]

        # Re-number lines after filtering + truncation.
        lines = [
            LogLineDTO(
                line_no=i,
                raw=ln.raw,
                parsed=ln.parsed,
                parse_error=ln.parse_error,
            )
            for i, ln in enumerate(lines)
        ]

        # Refresh mtime after reading.
        try:
            updated_at_ms = int(path.stat().st_mtime * 1000)
        except FileNotFoundError:
            updated_at_ms = None

        # Refresh source DTO with latest file metadata.
        source_dto = LogSourceDTO(
            type=source.type,
            label=source.label,
            format=source.format,
            description=source.description,
            path=source.path,
            exists=True,
            size_bytes=file_size,
            updated_at_ms=updated_at_ms,
        )

        return LogReadResponseDTO(
            source=source_dto,
            lines=lines,
            truncated=truncated,
            read_bytes=read_bytes,
            total_bytes=file_size,
        )

    # -- internal: tail read -------------------------------------------------

    @staticmethod
    def _read_tail_bytes(path: Path, max_bytes: int) -> tuple[str, bool, int, int | None]:
        """Read up to *max_bytes* from the tail of *path*.

        Returns ``(text, truncated, read_bytes, file_size)``.
        """
        file_size: int = path.stat().st_size
        if file_size == 0:
            return "", False, 0, 0

        read_bytes = min(max_bytes, file_size)
        truncated = file_size > max_bytes

        with open(path, "rb") as f:
            if truncated:
                f.seek(file_size - read_bytes)
            content_bytes = f.read(read_bytes)

        # Decode with error replacement for robustness.
        text = content_bytes.decode("utf-8", errors="replace")

        # When truncated, the first line may be incomplete -- drop it.
        if truncated and content_bytes[0:1] != b"\n":
            first_nl = text.find("\n")
            text = text[first_nl + 1 :] if first_nl >= 0 else ""

        return text, truncated, read_bytes, file_size

    # -- internal: line parsing ----------------------------------------------

    @staticmethod
    def _parse_lines(raw_text: str) -> list[LogLineDTO]:
        """Split *raw_text* into lines and attempt JSON parsing for each."""
        if not raw_text:
            return []

        lines = raw_text.splitlines()
        result: list[LogLineDTO] = []
        for i, line in enumerate(lines):
            parsed: dict[str, Any] | None = None
            parse_error: str | None = None
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, ValueError) as exc:
                    parse_error = str(exc)
            result.append(
                LogLineDTO(
                    line_no=i,
                    raw=line,
                    parsed=parsed,
                    parse_error=parse_error,
                )
            )
        return result
