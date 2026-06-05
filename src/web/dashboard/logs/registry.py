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
3. Enforce a whitelist: every resolved path must fall under an
   *allowed root* (``kongming_home`` or ``cwd / ".kongming"``).
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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config_loader.models import Config

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogSourceSpec:
    """Static specification of a log source (no runtime state)."""

    type: str
    label: str
    format: Literal["jsonl", "plain", "mixed"]
    description: str
    resolve_path: Callable[[Config, Path], Path]


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
    return [
        kongming_home,
        (Path.cwd() / ".kongming").resolve(),
    ]


def _resolve_relative(path_str: str, kongming_home: Path) -> Path:
    """Resolve a potentially-relative path string against cwd, then validate."""
    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


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


# ---------------------------------------------------------------------------
# Resolve helpers per log type
# ---------------------------------------------------------------------------


def _resolve_web_server(_cfg: Config, home: Path) -> Path:
    return (home / "web" / "server.log").resolve()


def _resolve_full_log(cfg: Config, _home: Path) -> Path:
    return _resolve_relative(cfg.web.full_log.path, _home)


def _resolve_trace(cfg: Config, _home: Path) -> Path:
    return _resolve_relative(cfg.trace.output_path, _home)


def _resolve_heartbeat(_cfg: Config, home: Path) -> Path:
    return (home / "logs" / "heartbeat" / "heartbeat.log").resolve()


def _resolve_generic_channel(_cfg: Config, home: Path) -> Path:
    return (home / "logs" / "generic-channel" / "generic-channel.jsonl").resolve()


def _resolve_evolution(_cfg: Config, home: Path) -> Path:
    return (home / "logs" / "evolution.log").resolve()


def _resolve_cron_audit(_cfg: Config, home: Path) -> Path:
    return (home / "cron" / "audits.jsonl").resolve()


def _resolve_auto_approval_audit(_cfg: Config, home: Path) -> Path:
    return (home / "web" / "auto_approval" / "audit.jsonl").resolve()


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

    def list_sources(self) -> list[ResolvedLogSource]:
        """Return every registered log source with live file-system metadata."""
        return [self._resolve_one(spec) for spec in self._sources]

    def get_source(self, type: str) -> ResolvedLogSource:
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
        return self._resolve_one(spec)

    def resolve_source_path(self, type: str) -> Path:
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
        resolved = spec.resolve_path(self._config, self._kongming_home)
        return _ensure_under_allowed_root(resolved, self._allowed_roots)

    # -- internal -----------------------------------------------------------

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

    def _resolve_one(self, spec: LogSourceSpec) -> ResolvedLogSource:
        """Resolve a single spec into a :class:`ResolvedLogSource`."""
        resolved = spec.resolve_path(self._config, self._kongming_home)
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
