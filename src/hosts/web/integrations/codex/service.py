"""Codex CLI process management for the web bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hosts.web.integrations.codex._image_cli_args import CodexImageCliArgsBuilder
from hosts.web.integrations.codex.approval import map_permission_mode
from hosts.web.integrations.codex.normalizer import normalize
from hosts.web.protocol import UsageSummaryUpdatedFrame
from hosts.web.shared.session_manager import SessionManager
from hosts.web.websocket.thread_status import (
    get_thread_status_manager,
    publish_normalized_status,
)
from hosts.web.websocket.thread_status_manager import ThreadStatusRunLease
from network.network_log import log_network_exception

if TYPE_CHECKING:
    from hosts.web.protocol.rest_models import UserInputAttachment
    from hosts.web.uploads.storage import AssetStorage

logger = logging.getLogger(__name__)

_AUTH_KEYWORDS: tuple[str, ...] = (
    "not authenticated",
    "not logged in",
    "please login",
    "please log in",
    "auth.json",
)

_STDERR_NOISE_PREFIXES: tuple[str, ...] = (
    "Reading additional input from stdin",
    "failed to load skill",
    "rmcp transport closed",
)


def _resolve_codex_program() -> str:
    """Return a CreateProcess-friendly Codex executable on Windows."""

    if sys.platform != "win32":
        return "codex"
    return shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex"


@dataclass(frozen=True)
class _CodexInvocation:
    """Arguments and stdin payload for one ``codex exec`` invocation."""

    argv: list[str]
    stdin_text: str


class CodexService:
    """Codex CLI child-process orchestration for one web connection."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        thread_manager: Any = None,
        asset_storage: AssetStorage | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.thread_manager = thread_manager
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._run_counters: dict[str, int] = {}
        self._image_args_builder: CodexImageCliArgsBuilder | None = (
            CodexImageCliArgsBuilder(asset_storage) if asset_storage is not None else None
        )

    async def query(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_mode: str = "default",
        model: str | None = None,
        resume: bool = False,
        kongming_thread_id: str | None = None,
        writer: Any,
        attachments: Sequence[UserInputAttachment] | None = None,
    ) -> None:
        image_args: list[str] = []
        if attachments and self._image_args_builder is not None and kongming_thread_id:
            image_args = self._image_args_builder.build(attachments, thread_id=kongming_thread_id)

        invocation = self._build_invocation(
            session_id=session_id,
            command=command,
            cwd=cwd,
            permission_mode=permission_mode,
            model=model,
            resume=resume,
            image_args=image_args,
        )

        proc: asyncio.subprocess.Process | None = None
        active_sid = session_id
        complete_already_sent = False
        status_lease: ThreadStatusRunLease | None = None
        try:
            effective_tid = kongming_thread_id or active_sid
            run_index = self._run_counters.get(effective_tid, 0) + 1
            self._run_counters[effective_tid] = run_index
            status_manager = get_thread_status_manager()
            status_lease = await status_manager.begin_run(
                effective_tid,
                f"{effective_tid}-{run_index}",
            )
            await status_manager.publish_status(
                status_lease,
                phase="responding",
            )
            current_task = asyncio.current_task()
            if current_task is None:
                raise RuntimeError("codex query requires an active asyncio task")
            await self.session_manager.register(
                active_sid,
                writer,
                query_task=current_task,
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    *invocation.argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                await self._safe_send(
                    writer,
                    self._error_msg(
                        active_sid,
                        "codex CLI not installed; npm i -g @openai/codex",
                    ),
                )
                await status_manager.publish_status(
                    status_lease,
                    phase="error",
                )
                return

            self._processes[active_sid] = proc
            await self._write_stdin(proc, invocation.stdin_text)

            stderr_task = asyncio.create_task(self._drain_stderr(proc))

            active_sid, complete_already_sent = await self._consume_stdout(
                proc,
                writer,
                active_sid,
                kongming_thread_id=kongming_thread_id,
                cwd=cwd,
                status_lease=status_lease,
                run_index=run_index,
                model=model,
            )

            stderr_text = await stderr_task
            return_code = await proc.wait()

            record = self.session_manager.get(active_sid)
            aborted = record is not None and record.abort_event.is_set()

            if aborted:
                if not complete_already_sent:
                    await self._safe_send(
                        writer,
                        self._complete_msg(active_sid, exit_code=return_code, aborted=True),
                    )
                    await status_manager.publish_status(
                        status_lease,
                        phase="idle",
                    )
                return

            if self._stderr_has_auth_error(stderr_text):
                await self._safe_send(
                    writer,
                    self._error_msg(active_sid, "codex not authenticated; run `codex login`"),
                )
                await status_manager.publish_status(
                    status_lease,
                    phase="error",
                )
                return

            if return_code != 0 and not complete_already_sent:
                tail = self._tail(stderr_text, max_chars=400) or "no stderr captured"
                await self._safe_send(
                    writer,
                    self._error_msg(active_sid, f"codex exited with code {return_code}: {tail}"),
                )
                await status_manager.publish_status(
                    status_lease,
                    phase="error",
                )
                return

            if not complete_already_sent:
                await self._safe_send(
                    writer,
                    self._complete_msg(active_sid, exit_code=return_code, aborted=False),
                )
                await status_manager.publish_status(
                    status_lease,
                    phase="complete",
                )
        except asyncio.CancelledError:
            logger.info("codex service.query cancelled (session=%s)", active_sid)
            await self._safe_send(
                writer,
                self._complete_msg(active_sid, exit_code=1, aborted=True),
            )
            if status_lease is not None:
                await get_thread_status_manager().publish_status(
                    status_lease,
                    phase="idle",
                )
        except Exception as exc:
            logger.exception("codex service.query failed")
            await self._safe_send(
                writer,
                self._error_msg(active_sid, f"codex service error: {exc}"),
            )
            if status_lease is not None:
                await get_thread_status_manager().publish_status(
                    status_lease,
                    phase="error",
                )
        finally:
            self._processes.pop(active_sid, None)
            self._processes.pop(session_id, None)
            await self.session_manager.unregister(active_sid)
            if active_sid != session_id:
                await self.session_manager.unregister(session_id)

    async def abort(self, session_id: str) -> bool:
        proc = self._processes.get(session_id)
        had_session = await self.session_manager.request_abort(session_id)
        if proc is None:
            return had_session

        if proc.returncode is not None:
            return True

        with contextlib.suppress(ProcessLookupError):
            proc.terminate()

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await proc.wait()
            except Exception as exc:
                log_network_exception(
                    "hosts.web.integrations.codex.service",
                    "kill_wait_failed",
                    exc,
                    session_id=session_id,
                )
        return True

    def _build_invocation(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_mode: str,
        model: str | None,
        resume: bool,
        image_args: list[str] | None = None,
    ) -> _CodexInvocation:
        sandbox, policy = map_permission_mode(permission_mode)
        args: list[str] = [
            _resolve_codex_program(),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            cwd,
            "--sandbox",
            sandbox,
            "--config",
            f'approval_policy="{policy}"',
        ]
        if model:
            args += ["--model", model]
        if resume:
            args += ["resume", session_id]
        if image_args:
            args += image_args
        return _CodexInvocation(argv=args, stdin_text=command)

    async def _write_stdin(
        self,
        proc: asyncio.subprocess.Process,
        prompt: str,
    ) -> None:
        stdin = proc.stdin
        if stdin is None:
            raise RuntimeError("codex child process has no stdin")

        try:
            stdin.write(prompt.encode("utf-8"))
            await stdin.drain()
        finally:
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if callable(wait_closed):
                await wait_closed()

    async def _consume_stdout(
        self,
        proc: asyncio.subprocess.Process,
        writer: Any,
        session_id: str,
        *,
        kongming_thread_id: str | None,
        cwd: str,
        status_lease: ThreadStatusRunLease,
        run_index: int = 0,
        model: str | None = None,
    ) -> tuple[str, bool]:
        active_sid = session_id
        complete_already_sent = False
        status_manager = get_thread_status_manager()

        if proc.stdout is None:
            return active_sid, complete_already_sent

        async for raw in proc.stdout:
            record = self.session_manager.get(active_sid)
            if record is not None and record.abort_event.is_set():
                break

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                await self._safe_send(
                    writer,
                    self._error_msg(
                        active_sid,
                        f"jsonl parse failed: {exc}: {self._tail(line, max_chars=200)}",
                    ),
                )
                continue

            if not isinstance(event, dict):
                continue

            if event.get("type") == "thread.started":
                new_sid = event.get("thread_id")
                if isinstance(new_sid, str) and new_sid and new_sid != active_sid:
                    renamed = await self.session_manager.rename(active_sid, new_sid)
                    if renamed:
                        proc_ref = self._processes.pop(active_sid, None)
                        if proc_ref is not None:
                            self._processes[new_sid] = proc_ref
                        active_sid = new_sid
                if (
                    isinstance(new_sid, str)
                    and new_sid
                    and kongming_thread_id
                    and self.thread_manager is not None
                ):
                    try:
                        metas = self.thread_manager.list_threads()
                        meta = next((m for m in metas if m.id == kongming_thread_id), None)
                        if meta is not None and not getattr(meta, "codex_thread_id", ""):
                            await self.thread_manager.bind_codex_thread(
                                kongming_thread_id,
                                new_sid,
                                cwd,
                            )
                    except Exception as exc:
                        log_network_exception(
                            "hosts.web.integrations.codex.service",
                            "bind_codex_thread_failed",
                            exc,
                            thread_id=kongming_thread_id,
                            session_id=new_sid,
                        )

            if (
                event.get("type") == "turn.completed"
                and self.thread_manager is not None
                and kongming_thread_id is not None
            ):
                try:
                    usage_dto = await self.thread_manager.usage_manager.get_thread_usage(
                        kongming_thread_id
                    )
                    if usage_dto is not None:
                        frame = UsageSummaryUpdatedFrame(
                            threadId=kongming_thread_id,
                            usage=usage_dto.model_dump(),
                        )
                        await status_manager.broadcast(frame.model_dump())
                except Exception as exc:
                    log_network_exception(
                        "hosts.web.integrations.codex.service",
                        "complete_usage_broadcast_failed",
                        exc,
                        thread_id=kongming_thread_id,
                        session_id=active_sid,
                    )

            for msg in normalize(event, active_sid):
                if msg.get("sessionId") != active_sid:
                    msg["sessionId"] = active_sid
                if msg.get("frame_type") == "complete":
                    complete_already_sent = True
                await self._safe_send(writer, dict(msg))
                if kongming_thread_id is not None:
                    await publish_normalized_status(
                        status_manager,
                        status_lease,
                        dict(msg),
                    )

        return active_sid, complete_already_sent

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> str:
        if proc.stderr is None:
            return ""
        chunks: list[str] = []
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            chunks.append(line)
            if any(line.startswith(prefix) for prefix in _STDERR_NOISE_PREFIXES):
                continue
            logger.warning("codex stderr: %s", line)
        return "\n".join(chunks)

    @staticmethod
    def _stderr_has_auth_error(text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(keyword in lowered for keyword in _AUTH_KEYWORDS)

    @staticmethod
    def _tail(text: str, *, max_chars: int) -> str:
        if not text:
            return ""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return "..." + text[-max_chars:]

    @staticmethod
    def _error_msg(session_id: str, error: str) -> dict[str, Any]:
        return {
            "frame_type": "error",
            "provider": "codex",
            "sessionId": session_id,
            "error": error,
        }

    @staticmethod
    def _complete_msg(
        session_id: str,
        *,
        exit_code: int,
        aborted: bool,
    ) -> dict[str, Any]:
        return {
            "frame_type": "complete",
            "provider": "codex",
            "sessionId": session_id,
            "exitCode": exit_code,
            "aborted": aborted,
        }

    @staticmethod
    async def _safe_send(writer: Any, msg: dict[str, Any]) -> None:
        try:
            await writer.send_json(msg)
        except Exception as exc:
            log_network_exception(
                "hosts.web.integrations.codex.service",
                "safe_send_failed",
                exc,
                frame_kind=msg.get("kind"),
                session_id=msg.get("sessionId"),
            )


__all__ = ["CodexService"]
