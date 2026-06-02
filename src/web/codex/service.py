"""Codex CLI 子进程管理 + 事件流主循环（v0.1）。

参考 ccui ``server/openai-codex.js::queryCodex`` + ``mapPermissionModeToCodexOptions``。

职责：

- 装配 ``codex exec --json`` 启动参数（含 ``-s/--ask-for-approval`` 三档映射）
- spawn 子进程（``stdin=DEVNULL`` 必需，否则 codex 不退出）
- 注册到 :class:`SessionManager`，初始 register_id 是 ``pending-XXX`` placeholder；
  收到 ``thread.started`` 后 :meth:`SessionManager.rename` 替换为真实 thread_id
- stdout 行级 reader：``json.loads`` → :func:`normalize` → ``writer.send_json``
- stderr 不直接转给前端（噪音多），但保留 auth 关键词的转译
- :meth:`abort` SIGTERM → 等 2s → SIGKILL fallback
- 终态出 ``kind:complete``（normalizer 已经从 ``turn.completed`` 发过的话不重复）

设计决策：

- 不感知 codex 厂商内部细节（CLAUDE.md 第 11 条），只做命令组装 + 事件转发
- ``CodexService`` 与 :class:`web.claude_code.service.ClaudeCodeService` 平级，
  共享 :class:`SessionManager`
- 不实现 per-call 审批桥接（spec 砍了）
- 不读 ``~/.codex/sessions/`` / 不解析 rollout jsonl（projects_scanner / jsonl_history 的活）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from network.network_log import log_network_exception
from web._shared.session_manager import SessionManager
from web.codex._image_cli_args import CodexImageCliArgsBuilder
from web.codex.approval import map_permission_mode
from web.codex.normalizer import normalize
from web.thread_status_ws import get_broadcaster

if TYPE_CHECKING:
    from web.protocol.rest_models import UserInputAttachment
    from web.uploads.storage import AssetStorage

logger = logging.getLogger(__name__)

# stderr 中命中以下关键词的行（大小写不敏感）会被翻译成 codex login 引导
_AUTH_KEYWORDS: tuple[str, ...] = (
    "not authenticated",
    "not logged in",
    "please login",
    "please log in",
    "auth.json",
)

# stderr 中命中以下噪音前缀的行直接丢弃（不进 trace 也不转前端）；
# 其他 stderr 行走 logger.warning（转 trace），仍**不**直接转前端。
_STDERR_NOISE_PREFIXES: tuple[str, ...] = (
    "Reading additional input from stdin",
    "failed to load skill",
    "rmcp transport closed",
)


class CodexService:
    """Codex CLI 子进程管理 + 事件流主循环。

    每个 ``CodexService`` 实例（per-connection）持有：

    - ``session_manager``：共享 :class:`SessionManager` 引用
    - ``_processes``：``session_id → asyncio.subprocess.Process`` 映射，
      用于 :meth:`abort` 找到当前子进程
    """

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        thread_manager: Any = None,
        asset_storage: AssetStorage | None = None,
    ) -> None:
        """codex-channel-image-paste 新增 ``asset_storage`` 注入：

        ``None`` 表示不支持图片附件（attachments kwarg 会被忽略）；测试 stub /
        CLI 装配不传时无影响。prod 装配在 ``web.app.create_app`` 注入
        ``app.state.asset_storage`` 单例。
        """
        self.session_manager = session_manager
        self.thread_manager = thread_manager
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # per-thread run 计数器——每次 query 调用自增，用于构造 run_id
        self._run_counters: dict[str, int] = {}
        # image CLI args builder：注入 storage 后 lazy 构造一次复用
        self._image_args_builder: CodexImageCliArgsBuilder | None = (
            CodexImageCliArgsBuilder(asset_storage) if asset_storage is not None else None
        )

    # ------------------------------------------------------------------ query

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
        """spawn ``codex exec --json``，把事件流喂到 ``writer``。

        Args:
            session_id: 注册到 SessionManager 的初始 id。在 ``thread.started``
                到达前是临时占位（建议传 ``pending-XXX``），到达后会被 rename
                成真实 thread_id。
            command: 用户 prompt（作为 codex 命令的最后一个位置参数）
            cwd: 工作目录（``-C`` flag）
            permission_mode: 三档之一（``default`` / ``acceptEdits`` /
                ``bypassPermissions``）；未知值走 default 兜底
            model: 可选 ``-m`` flag
            resume: True 时使用 ``codex exec resume <session_id>`` 形式
            kongming_thread_id: Kongming 产品层 thread id；提供时会把 Codex
                ``thread.started`` 的真实 id 回写到 ``codex_thread_id``。
            writer: duck-typed，任何有 ``async def send_json(msg: dict)`` 的对象
        """
        # codex-channel-image-paste 新增：三者齐全才拼 --image flag，否则空 list 安全
        image_args: list[str] = []
        if attachments and self._image_args_builder is not None and kongming_thread_id:
            image_args = self._image_args_builder.build(attachments, thread_id=kongming_thread_id)

        args = self._build_args(
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
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                # codex 不在 PATH
                await self._safe_send(
                    writer,
                    self._error_msg(
                        active_sid,
                        "codex CLI not installed; npm i -g @openai/codex",
                    ),
                )
                return

            self._processes[active_sid] = proc

            # stderr 抽取 task（不抢 stdout）
            stderr_task = asyncio.create_task(self._drain_stderr(proc))

            # 注册到 SessionManager；query_task 设为当前 task（被 abort 时
            # SessionManager.request_abort 会 cancel 它，触发 CancelledError）
            current_task = asyncio.current_task()
            await self.session_manager.register(
                active_sid,
                writer,
                query_task=current_task,
            )

            # run_index：per-thread 自增，用于构造 run_id（跟 core.Runner 一致）
            effective_tid = kongming_thread_id or active_sid
            run_index = self._run_counters.get(effective_tid, 0) + 1
            self._run_counters[effective_tid] = run_index

            # stdout 主循环
            active_sid, complete_already_sent = await self._consume_stdout(
                proc,
                writer,
                active_sid,
                kongming_thread_id=kongming_thread_id,
                cwd=cwd,
                run_index=run_index,
                model=model,
            )

            # 等 stderr 收尾 + 拿子进程退出码
            stderr_text = await stderr_task
            return_code = await proc.wait()

            # 检查是否被 abort
            record = self.session_manager.get(active_sid)
            aborted = record is not None and record.abort_event.is_set()

            if aborted:
                if not complete_already_sent:
                    await self._safe_send(
                        writer,
                        self._complete_msg(active_sid, exit_code=return_code, aborted=True),
                    )
                return

            # auth 错误转译（即便 exit_code != 0 也走友好提示）
            if self._stderr_has_auth_error(stderr_text):
                await self._safe_send(
                    writer,
                    self._error_msg(
                        active_sid,
                        "codex not authenticated; run `codex login`",
                    ),
                )
                return

            # 子进程异常退出 + 没收到 turn.completed → 发 error 含 stderr 末尾
            if return_code != 0 and not complete_already_sent:
                tail = self._tail(stderr_text, max_chars=400) or "no stderr captured"
                await self._safe_send(
                    writer,
                    self._error_msg(
                        active_sid,
                        f"codex exited with code {return_code}: {tail}",
                    ),
                )
                return

            # 正常退出但 normalizer 没从 turn.completed 发过 complete → 补发
            if not complete_already_sent:
                await self._safe_send(
                    writer,
                    self._complete_msg(active_sid, exit_code=return_code, aborted=False),
                )
        except asyncio.CancelledError:
            # 被 SessionManager.request_abort cancel —— abort 路径已 SIGTERM/SIGKILL
            logger.info("codex service.query cancelled (session=%s)", active_sid)
            await self._safe_send(
                writer,
                self._complete_msg(active_sid, exit_code=1, aborted=True),
            )
            # 不再 raise——让 finally 收尾，调用方不需要 CancelledError
        except Exception as exc:
            logger.exception("codex service.query failed")
            await self._safe_send(
                writer,
                self._error_msg(active_sid, f"codex service error: {exc}"),
            )
        finally:
            # 移除子进程映射（active_sid 可能已 rename，旧 placeholder 也尝试删）
            self._processes.pop(active_sid, None)
            self._processes.pop(session_id, None)
            await self.session_manager.unregister(active_sid)
            # 如果 placeholder 没 rename 成真实 id，旧 placeholder 也清一下
            if active_sid != session_id:
                await self.session_manager.unregister(session_id)

    # ------------------------------------------------------------------ abort

    async def abort(self, session_id: str) -> bool:
        """SIGTERM 子进程；2s 后还活着 SIGKILL。

        同时通知 :class:`SessionManager` 让主循环看见 abort_event（即便子进程
        已经在退出途中，也能让 finally 走 aborted=True 路径）。

        Returns:
            ``True`` 已请求中止；``False`` session 不存在
        """
        proc = self._processes.get(session_id)
        # 先发 abort_event——即使子进程已结束，也让正在跑的 query 主循环看见
        had_session = await self.session_manager.request_abort(session_id)
        if proc is None:
            return had_session

        if proc.returncode is not None:
            # 子进程已退出
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
                    "web.codex.service",
                    "kill_wait_failed",
                    exc,
                    session_id=session_id,
                )
        return True

    # ----------------------------------------------------------- 内部辅助

    def _build_args(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_mode: str,
        model: str | None,
        resume: bool,
        image_args: list[str] | None = None,
    ) -> list[str]:
        """构造 ``codex exec`` 启动参数。

        非 resume 形式：
            ``codex exec --json --skip-git-repo-check -C <cwd> -s <sandbox>
            --ask-for-approval <policy> [-m <model>] <command>``

        resume 形式：
            ``codex exec resume <session_id> --json --skip-git-repo-check
            -C <cwd> -s <sandbox> --ask-for-approval <policy> [-m <model>] <command>``
        """
        sandbox, policy = map_permission_mode(permission_mode)
        if resume:
            args: list[str] = [
                "codex",
                "exec",
                "resume",
                session_id,
                "--json",
                "--skip-git-repo-check",
                "-C",
                cwd,
                "-s",
                sandbox,
                "--ask-for-approval",
                policy,
            ]
        else:
            args = [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                cwd,
                "-s",
                sandbox,
                "--ask-for-approval",
                policy,
            ]
        if model:
            args += ["-m", model]
        # codex-channel-image-paste：--image flag 必须在 command (positional) 之前
        # 否则 Codex CLI 会把 image 路径当成 prompt 的一部分
        if image_args:
            args += image_args
        args.append(command)
        return args

    async def _consume_stdout(
        self,
        proc: asyncio.subprocess.Process,
        writer: Any,
        session_id: str,
        *,
        kongming_thread_id: str | None,
        cwd: str,
        run_index: int = 0,
        model: str | None = None,
    ) -> tuple[str, bool]:
        """逐行读取 stdout，归一化后送 writer。

        Returns:
            ``(active_session_id, complete_already_sent)`` —— active id 可能在
            ``thread.started`` 后被 rename；complete 标志用于让 :meth:`query`
            决定是否补发 ``kind:complete``。
        """
        active_sid = session_id
        complete_already_sent = False
        broadcaster = get_broadcaster()

        if proc.stdout is None:
            return active_sid, complete_already_sent

        async for raw in proc.stdout:
            # abort 检查——拿当前活跃 record（可能已 rename）
            record = self.session_manager.get(active_sid)
            if record is not None and record.abort_event.is_set():
                break

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                # 不抛错，发 kind:error 含 raw 行（截断）
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

            # thread.started → 拿 thread_id 替换 placeholder
            if event.get("type") == "thread.started":
                new_sid = event.get("thread_id")
                if isinstance(new_sid, str) and new_sid and new_sid != active_sid:
                    renamed = await self.session_manager.rename(active_sid, new_sid)
                    if renamed:
                        # 同步更新 _processes 映射 key
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
                            "web.codex.service",
                            "bind_codex_thread_failed",
                            exc,
                            thread_id=kongming_thread_id,
                            session_id=new_sid,
                        )

            # usage 派生：turn.completed 时从 codex rollout 真源派生最新 usage 推前端。
            # **v2**：manager 无状态门面，service 不再写盘。
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
                        await broadcaster.broadcast(
                            {
                                "type": "usage_summary_updated",
                                "threadId": kongming_thread_id,
                                "usage": usage_dto.model_dump(),
                            }
                        )
                except Exception as exc:
                    log_network_exception(
                        "web.codex.service",
                        "complete_usage_broadcast_failed",
                        exc,
                        thread_id=kongming_thread_id,
                        session_id=active_sid,
                    )

            # 归一化 + 下发
            for msg in normalize(event, active_sid):
                # 把出站消息的 sessionId 字段同步成最新 active_sid（normalizer
                # 用调用时传入的 session_id，可能是 rename 前的）
                if msg.get("sessionId") != active_sid:
                    msg["sessionId"] = active_sid
                if msg.get("frame_type") == "complete":
                    complete_already_sent = True
                await self._safe_send(writer, dict(msg))
                if kongming_thread_id is not None:
                    await broadcaster.emit(kongming_thread_id, dict(msg))

        return active_sid, complete_already_sent

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> str:
        """收 stderr 全文（不转前端，但保留转译需要）。

        - 噪音前缀直接丢
        - 其余行 ``logger.warning``（转 trace）
        - 全文返回供 :meth:`query` 做 auth 关键词检测
        """
        if proc.stderr is None:
            return ""
        chunks: list[str] = []
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            chunks.append(line)
            if any(line.startswith(p) for p in _STDERR_NOISE_PREFIXES):
                continue
            logger.warning("codex stderr: %s", line)
        return "\n".join(chunks)

    @staticmethod
    def _stderr_has_auth_error(text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(kw in lowered for kw in _AUTH_KEYWORDS)

    @staticmethod
    def _tail(text: str, *, max_chars: int) -> str:
        """取末尾 max_chars 字符，前面用 ``…`` 省略。"""
        if not text:
            return ""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return "…" + text[-max_chars:]

    @staticmethod
    def _error_msg(session_id: str, error: str) -> dict[str, Any]:
        """构造 ``frame_type:error`` 出站消息（最小字段集）。"""
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
        """构造 ``frame_type:complete`` 出站消息（用于 turn.completed 缺失时补发）。"""
        return {
            "frame_type": "complete",
            "provider": "codex",
            "sessionId": session_id,
            "exitCode": exit_code,
            "aborted": aborted,
        }

    @staticmethod
    async def _safe_send(writer: Any, msg: dict[str, Any]) -> None:
        """``writer.send_json`` 的容错包装（连接断开时不抛）。"""
        try:
            await writer.send_json(msg)
        except Exception as exc:
            log_network_exception(
                "web.codex.service",
                "safe_send_failed",
                exc,
                frame_kind=msg.get("kind"),
                session_id=msg.get("sessionId"),
            )


__all__ = ["CodexService"]
