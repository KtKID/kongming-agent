"""Log source registry -- discover, resolve and stat known log files.

The registry owns a static catalogue of *log types* (``web_server``,
``full_log``, ``trace``, …).  Each type carries a human-readable label,
a format hint, a description, and a *resolve function* that turns the
application :class:`Config` + ``kongming_home`` into an absolute
:class:`~pathlib.Path`.

Responsibilities
----------------
1. Return a list of available log sources with ``exists / size / mtime``.
2. Resolve paths from config values or ``kongming_home`` conventions.
3. Enforce a whitelist: every resolved path must fall under ``kongming_home``.
4. Annotate each source with ``format`` and ``description``.
5. Missing files are reported as ``exists=False`` -- **never raise**.
6. Queries for unknown ``type`` values raise :class:`ValueError`.

Constraints
-----------
- No import of ``web.protocol`` (avoid circular deps; DTO migration is
  tracked separately as task #3).
- No import of ``safety``, ``tools``, ``host`` or other kernel modules.
- File I/O is limited to ``Path.stat()``; no reading of log contents.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path

_THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogSourceContext:
    """Dynamic context used by thread-scoped log sources."""

    thread_id: str | None = None


@dataclass(frozen=True)
class LogSourceSpec:
    """Static specification of a log source (no runtime state)."""

    type: str
    label: str
    format: Literal["jsonl", "plain", "mixed"]
    description: str
    resolve_path: Callable[[Config, Path, LogSourceContext], Path]
    requires_thread_context: bool = False


@dataclass(frozen=True)
class ResolvedLogSource:
    """A log source resolved to an absolute path with file-system metadata."""

    type: str
    label: str
    format: Literal["jsonl", "plain", "mixed"]
    description: str
    path: str  # absolute path as string
    exists: bool
    size_bytes: int | None
    updated_at_ms: int | None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _allowed_roots(kongming_home: Path) -> list[Path]:
    """Compute the list of allowed root directories (resolved, absolute)."""
    return [kongming_home]


def _resolve_relative(path_str: str, kongming_home: Path) -> Path:
    """Resolve a potentially-relative path string against kongming home."""
    return resolve_kongming_path(path_str, kongming_home=kongming_home)


def _ensure_under_allowed_root(resolved: Path, allowed_roots: list[Path]) -> Path:
    """Raise ``ValueError`` if *resolved* does not fall under any allowed root."""
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"Resolved path {resolved} is outside allowed roots: "
        f"{', '.join(str(r) for r in allowed_roots)}"
    )


def _validate_thread_id(thread_id: str) -> str:
    """Validate a Web thread id before it is used in a filesystem path."""
    if not _THREAD_ID_RE.match(thread_id):
        raise ValueError(f"Invalid thread_id: {thread_id!r}")
    return thread_id


# ---------------------------------------------------------------------------
# Resolve helpers per log type
# ---------------------------------------------------------------------------


def _resolve_web_server(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "web" / "server.log").resolve()


def _resolve_full_log(cfg: Config, _home: Path, _context: LogSourceContext) -> Path:
    return _resolve_relative(cfg.web.full_log.path, _home)


def _resolve_trace(cfg: Config, _home: Path, _context: LogSourceContext) -> Path:
    return _resolve_relative(cfg.trace.output_path, _home)


def _resolve_heartbeat(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "logs" / "heartbeat" / "heartbeat.log").resolve()


def _resolve_generic_channel(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "logs" / "generic-channel" / "generic-channel.jsonl").resolve()


def _resolve_evolution(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "logs" / "evolution.log").resolve()


def _resolve_cron_audit(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "cron" / "audits.jsonl").resolve()


def _resolve_auto_approval_audit(_cfg: Config, home: Path, _context: LogSourceContext) -> Path:
    return (home / "web" / "auto_approval" / "audit.jsonl").resolve()


def _resolve_session_conversation(cfg: Config, home: Path, context: LogSourceContext) -> Path:
    if context.thread_id is None:
        raise ValueError("thread_id is required for session_conversation log source")
    thread_id = _validate_thread_id(context.thread_id)
    session_root = _resolve_relative(cfg.session.file_store_path, home)
    return (session_root / thread_id / f"{thread_id}.jsonl").resolve()


# ---------------------------------------------------------------------------
# LogSourceRegistry
# ---------------------------------------------------------------------------


class LogSourceRegistry:
    """Discover, resolve and stat known log sources."""

    def __init__(self, config: Config, kongming_home: Path) -> None:
        self._config = config
        self._kongming_home = kongming_home.resolve()
        self._sources: list[LogSourceSpec] = self._build_sources()
        self._source_by_type: dict[str, LogSourceSpec] = {s.type: s for s in self._sources}
        self._allowed_roots = _allowed_roots(self._kongming_home)

    # -- public API ---------------------------------------------------------

    def list_sources(self, *, thread_id: str | None = None) -> list[ResolvedLogSource]:
        """Return every registered log source with live file-system metadata."""
        context = self._build_context(thread_id)
        return [
            self._resolve_one(spec, context)
            for spec in self._sources
            if not spec.requires_thread_context or context.thread_id is not None
        ]

    def get_source(self, type: str, *, thread_id: str | None = None) -> ResolvedLogSource:
        """Return a single resolved log source by *type*.

        Raises:
            ValueError: if *type* is not registered.
        """
        spec = self._source_by_type.get(type)
        if spec is None:
            raise ValueError(
                f"Unknown log source type: {type!r}. "
                f"Available types: {sorted(self._source_by_type)}"
            )
        context = self._build_context(thread_id)
        if spec.requires_thread_context and context.thread_id is None:
            raise ValueError(f"thread_id is required for log source type: {type!r}")
        return self._resolve_one(spec, context)

    def resolve_source_path(self, type: str, *, thread_id: str | None = None) -> Path:
        """Resolve the absolute path for *type* after whitelist validation.

        Raises:
            ValueError: if *type* is not registered, or the resolved path
                falls outside the allowed roots.
        """
        spec = self._source_by_type.get(type)
        if spec is None:
            raise ValueError(
                f"Unknown log source type: {type!r}. "
                f"Available types: {sorted(self._source_by_type)}"
            )
        context = self._build_context(thread_id)
        if spec.requires_thread_context and context.thread_id is None:
            raise ValueError(f"thread_id is required for log source type: {type!r}")
        resolved = spec.resolve_path(self._config, self._kongming_home, context)
        return _ensure_under_allowed_root(resolved, self._allowed_roots)

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _build_context(thread_id: str | None) -> LogSourceContext:
        if thread_id is None:
            return LogSourceContext()
        return LogSourceContext(thread_id=_validate_thread_id(thread_id))

    def _build_sources(self) -> list[LogSourceSpec]:
        return [
            LogSourceSpec(
                type="web_server",
                label="Web Server Log",
                format="plain",
                description="Web 服务 stdout/stderr",
                resolve_path=_resolve_web_server,
            ),
            LogSourceSpec(
                type="full_log",
                label="Full Communication Log",
                format="jsonl",
                description="前后端通信全量日志",
                resolve_path=_resolve_full_log,
            ),
            LogSourceSpec(
                type="trace",
                label="Runner Trace",
                format="jsonl",
                description="Runner / tool / LLM trace",
                resolve_path=_resolve_trace,
            ),
            LogSourceSpec(
                type="heartbeat",
                label="Heartbeat Log",
                format="plain",
                description="网络心跳诊断",
                resolve_path=_resolve_heartbeat,
            ),
            LogSourceSpec(
                type="generic_channel",
                label="Generic Channel Log",
                format="jsonl",
                description="通用频道关键路径诊断",
                resolve_path=_resolve_generic_channel,
            ),
            LogSourceSpec(
                type="session_conversation",
                label="Session Conversation",
                format="jsonl",
                description="当前 thread 的 FileSession 完整对话记录",
                resolve_path=_resolve_session_conversation,
                requires_thread_context=True,
            ),
            LogSourceSpec(
                type="evolution",
                label="Evolution Log",
                format="plain",
                description="Self-evolution 内部日志",
                resolve_path=_resolve_evolution,
            ),
            LogSourceSpec(
                type="cron_audit",
                label="Cron Audit",
                format="jsonl",
                description="Cron 审计",
                resolve_path=_resolve_cron_audit,
            ),
            LogSourceSpec(
                type="auto_approval_audit",
                label="Auto Approval Audit",
                format="jsonl",
                description="智能审批审计",
                resolve_path=_resolve_auto_approval_audit,
            ),
        ]

    def _resolve_one(self, spec: LogSourceSpec, context: LogSourceContext) -> ResolvedLogSource:
        """Resolve a single spec into a :class:`ResolvedLogSource`."""
        resolved = spec.resolve_path(self._config, self._kongming_home, context)
        try:
            validated = _ensure_under_allowed_root(resolved, self._allowed_roots)
        except ValueError:
            # Path escapes whitelist -- treat as non-existent rather than crash
            return ResolvedLogSource(
                type=spec.type,
                label=spec.label,
                format=spec.format,
                description=spec.description,
                path=str(resolved),
                exists=False,
                size_bytes=None,
                updated_at_ms=None,
            )

        return self._stat_source(spec, validated)

    @staticmethod
    def _stat_source(spec: LogSourceSpec, path: Path) -> ResolvedLogSource:
        """Stat *path* and build a :class:`ResolvedLogSource`."""
        try:
            st = path.stat()
            return ResolvedLogSource(
                type=spec.type,
                label=spec.label,
                format=spec.format,
                description=spec.description,
                path=str(path),
                exists=True,
                size_bytes=st.st_size,
                updated_at_ms=int(st.st_mtime * 1000),
            )
        except FileNotFoundError:
            return ResolvedLogSource(
                type=spec.type,
                label=spec.label,
                format=spec.format,
                description=spec.description,
                path=str(path),
                exists=False,
                size_bytes=None,
                updated_at_ms=None,
            )
