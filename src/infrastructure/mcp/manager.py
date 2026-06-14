"""最小 MCP JSON-RPC over stdio client。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Coroutine
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from infrastructure.mcp.models import McpCallResult, McpToolDescriptor

_JSON_RPC_VERSION = "2.0"
_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_INITIALIZE_TIMEOUT_MS = 5_000
_DEFAULT_CALL_TIMEOUT_MS = 30_000
_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_T = TypeVar("_T")


class _McpProtocolError(Exception):
    """MCP stdio 协议错误。"""


class _McpProcessExitedError(_McpProtocolError):
    """MCP 子进程提前退出。"""


class _McpJsonRpcError(_McpProtocolError):
    """JSON-RPC error response。"""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message") or error))
        self.error = error


@dataclass(slots=True)
class _ServerRuntime:
    server_id: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    initialize_timeout_ms: int
    call_timeout_ms: int
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    stderr_lines: list[str] = field(default_factory=list)
    request_id: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status: str = "configured"
    tools: tuple[McpToolDescriptor, ...] = ()


class McpManager:
    """管理一组 stdio MCP server 的最小生命周期边界类。"""

    def __init__(self, server_configs: Any) -> None:
        self._servers: dict[str, _ServerRuntime] = {}
        self._diagnostics: dict[str, Any] = {"servers": {}, "events": []}
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        for config in server_configs or ():
            if not _get_bool(config, "enabled", True):
                server_id = str(_get_value(config, "server_id", "unknown"))
                self._record(server_id, "server_disabled", status="disabled")
                continue
            runtime = _runtime_from_config(config)
            self._servers[runtime.server_id] = runtime
            self._ensure_server_diag(runtime.server_id, status="configured")

    async def start_all(self) -> None:
        """启动所有启用的 MCP server，并完成 initialize + tools/list。"""

        self._owner_loop = asyncio.get_running_loop()
        for server_id, runtime in self._servers.items():
            await self._start_one(server_id, runtime)

    async def list_tools(self, server_id: str) -> tuple[McpToolDescriptor, ...]:
        """返回指定 server 的 tools/list 描述，必要时重新发起查询。"""

        return await self._run_on_owner_loop(self._list_tools(server_id))

    async def _list_tools(self, server_id: str) -> tuple[McpToolDescriptor, ...]:
        """在 MCP owner loop 上读取工具列表，输入 server_id，输出 descriptor 列表。"""

        runtime = self._servers.get(server_id)
        if runtime is None:
            self._record(server_id, "list_tools_failed", status="failed", message="unknown server")
            return ()
        if runtime.status == "ready" and runtime.tools:
            return runtime.tools
        if runtime.process is None or runtime.process.returncode is not None:
            self._record(
                server_id,
                "process_exited",
                status="failed",
                exit_code=None if runtime.process is None else runtime.process.returncode,
            )
            return ()
        try:
            runtime.tools = await self._request_tools_list_with_timeout(runtime)
        except TimeoutError:
            self._record(server_id, "list_tools_timeout", status="failed")
            runtime.status = "failed"
            await self._close_one(runtime)
            return ()
        except Exception as exc:
            self._record(
                server_id,
                "list_tools_failed",
                status="failed",
                message=str(exc),
                error_class=type(exc).__name__,
            )
            return ()
        runtime.status = "ready"
        self._record(server_id, "tools_listed", status="ready", tool_count=len(runtime.tools))
        return runtime.tools

    async def call_tool(
        self, server_id: str, tool_name: str, args: dict[str, Any] | None
    ) -> McpCallResult:
        """调用指定 MCP tool，并返回结构化结果和诊断。"""

        return await self._run_on_owner_loop(self._call_tool(server_id, tool_name, args))

    async def _call_tool(
        self, server_id: str, tool_name: str, args: dict[str, Any] | None
    ) -> McpCallResult:
        """在 MCP owner loop 上调用工具，输入 server/tool/args，输出调用结果。"""

        started_at = time.monotonic()
        runtime = self._servers.get(server_id)
        if runtime is None:
            return self._failed_call(
                server_id,
                tool_name,
                "unknown server",
                "unknown_server",
                started_at,
            )
        if runtime.process is None or runtime.process.returncode is not None:
            exit_code = None if runtime.process is None else runtime.process.returncode
            self._record(
                server_id,
                "process_exited",
                status="failed",
                tool_name=tool_name,
                exit_code=exit_code,
            )
            return self._failed_call(
                server_id,
                tool_name,
                "MCP server process exited",
                "process_exited",
                started_at,
                {"exit_code": exit_code},
            )

        params = {"name": tool_name, "arguments": args or {}}
        try:
            response = await asyncio.wait_for(
                self._send_request(runtime, "tools/call", params),
                timeout=_timeout_seconds(runtime.call_timeout_ms),
            )
        except TimeoutError:
            self._record(server_id, "call_timeout", status=runtime.status, tool_name=tool_name)
            return self._failed_call(
                server_id, tool_name, "MCP tool call timed out", "call_timeout", started_at
            )
        except _McpJsonRpcError as exc:
            self._record(
                server_id,
                "json_rpc_error",
                status=runtime.status,
                tool_name=tool_name,
                error=exc.error,
            )
            return self._failed_call(
                server_id,
                tool_name,
                str(exc),
                "json_rpc_error",
                started_at,
                {"json_rpc_error": exc.error},
            )
        except _McpProcessExitedError as exc:
            self._record(server_id, "process_exited", status="failed", tool_name=tool_name)
            return self._failed_call(server_id, tool_name, str(exc), "process_exited", started_at)
        except Exception as exc:
            self._record(
                server_id,
                "json_rpc_error",
                status=runtime.status,
                tool_name=tool_name,
                message=str(exc),
                error_class=type(exc).__name__,
            )
            return self._failed_call(server_id, tool_name, str(exc), "json_rpc_error", started_at)

        result = _as_dict(response.get("result"))
        data = _extract_structured_data(result)
        diagnostics = {
            "server_id": server_id,
            "tool_name": tool_name,
            "elapsed_ms": _elapsed_ms(started_at),
            "status": runtime.status,
        }
        return McpCallResult(
            ok=True,
            content_text=_extract_content_text(result),
            data=data,
            error_message=None,
            diagnostics=diagnostics,
        )

    async def aclose(self) -> None:
        """关闭所有已启动 MCP 子进程。"""

        await self._run_on_owner_loop(self._aclose())

    async def _aclose(self) -> None:
        """在 MCP owner loop 上关闭所有子进程，输入为空，输出为清理完成。"""

        for runtime in self._servers.values():
            await self._close_one(runtime)

    def diagnostics(self) -> dict[str, Any]:
        """返回当前 MCP manager 诊断快照。"""

        return deepcopy(self._diagnostics)

    async def _run_on_owner_loop(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """把 MCP I/O 固定到 owner loop，输入 coroutine，输出执行结果。"""

        owner_loop = self._owner_loop
        current_loop = asyncio.get_running_loop()
        if (
            owner_loop is None
            or owner_loop is current_loop
            or owner_loop.is_closed()
            or not owner_loop.is_running()
        ):
            return await coro

        future = asyncio.run_coroutine_threadsafe(coro, owner_loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _start_one(self, server_id: str, runtime: _ServerRuntime) -> None:
        if not runtime.command or shutil.which(runtime.command) is None:
            self._record(
                server_id,
                "missing_command",
                status="failed",
                command=runtime.command,
                message="MCP server command is missing",
            )
            runtime.status = "failed"
            return

        runtime.status = "starting"
        self._record(server_id, "starting", status="starting", command=runtime.command)
        try:
            runtime.process = await asyncio.create_subprocess_exec(
                runtime.command,
                *runtime.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=runtime.env,
            )
            runtime.stderr_task = asyncio.create_task(self._collect_stderr(runtime))
        except Exception as exc:
            self._record(
                server_id,
                "startup_failed",
                status="failed",
                command=runtime.command,
                message=str(exc),
                error_class=type(exc).__name__,
            )
            runtime.status = "failed"
            return

        runtime.status = "initializing"
        self._record(server_id, "process_started", status="initializing", pid=runtime.process.pid)
        try:
            await asyncio.wait_for(
                self._initialize(runtime),
                timeout=_timeout_seconds(runtime.initialize_timeout_ms),
            )
        except TimeoutError:
            self._record(server_id, "initialize_timeout", status="failed")
            runtime.status = "failed"
            await self._close_one(runtime)
            return
        except Exception as exc:
            self._record(
                server_id,
                "startup_failed",
                status="failed",
                message=str(exc),
                error_class=type(exc).__name__,
            )
            runtime.status = "failed"
            await self._close_one(runtime)
            return

        runtime.status = "listing_tools"
        try:
            runtime.tools = await self._request_tools_list_with_timeout(runtime)
        except TimeoutError:
            self._record(server_id, "list_tools_timeout", status="failed")
            runtime.status = "failed"
            await self._close_one(runtime)
            return
        except Exception as exc:
            self._record(
                server_id,
                "list_tools_failed",
                status="failed",
                message=str(exc),
                error_class=type(exc).__name__,
            )
            runtime.status = "failed"
            await self._close_one(runtime)
            return

        runtime.status = "ready"
        self._record(server_id, "ready", status="ready", tool_count=len(runtime.tools))

    async def _initialize(self, runtime: _ServerRuntime) -> None:
        await self._send_request(
            runtime,
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "kongming-agent", "version": "0.1.0"},
            },
        )

    async def _request_tools_list_with_timeout(
        self,
        runtime: _ServerRuntime,
    ) -> tuple[McpToolDescriptor, ...]:
        return await asyncio.wait_for(
            self._request_tools_list(runtime),
            timeout=_timeout_seconds(runtime.initialize_timeout_ms),
        )

    async def _request_tools_list(self, runtime: _ServerRuntime) -> tuple[McpToolDescriptor, ...]:
        response = await self._send_request(runtime, "tools/list", {})
        result = _as_dict(response.get("result"))
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise _McpProtocolError("tools/list response missing tools list")
        descriptors = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            description = item.get("description")
            descriptors.append(
                McpToolDescriptor(
                    server_id=runtime.server_id,
                    name=name,
                    title=item.get("title") if isinstance(item.get("title"), str) else None,
                    description=description if isinstance(description, str) else "",
                    input_schema=_as_dict(item.get("inputSchema") or item.get("input_schema")),
                    raw_descriptor=dict(item),
                )
            )
        return tuple(descriptors)

    async def _send_request(
        self, runtime: _ServerRuntime, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            runtime.process is None
            or runtime.process.stdin is None
            or runtime.process.stdout is None
        ):
            raise _McpProcessExitedError("MCP server process is unavailable")
        if runtime.process.returncode is not None:
            raise _McpProcessExitedError("MCP server process exited")

        async with runtime.lock:
            runtime.request_id += 1
            request_id = runtime.request_id
            request = {
                "jsonrpc": _JSON_RPC_VERSION,
                "id": request_id,
                "method": method,
                "params": params,
            }
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
            runtime.process.stdin.write(encoded + b"\n")
            await runtime.process.stdin.drain()

            while True:
                line = await runtime.process.stdout.readline()
                if line == b"":
                    raise _McpProcessExitedError("MCP server process exited")
                response = _decode_json_line(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise _McpJsonRpcError(_as_dict(response.get("error")))
                return response

    async def _collect_stderr(self, runtime: _ServerRuntime) -> None:
        if runtime.process is None or runtime.process.stderr is None:
            return
        while True:
            line = await runtime.process.stderr.readline()
            if line == b"":
                return
            runtime.stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())
            del runtime.stderr_lines[:-20]

    async def _close_one(self, runtime: _ServerRuntime) -> None:
        started_at = time.monotonic()
        process = runtime.process
        if process is None:
            runtime.status = "closed"
            self._record(runtime.server_id, "closed", status="closed")
            return

        runtime.status = "closing"
        if process.stdin is not None:
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        killed = False
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                killed = True
                process.kill()
                await process.wait()
        else:
            await process.wait()

        if runtime.stderr_task is not None:
            try:
                await asyncio.wait_for(runtime.stderr_task, timeout=0.2)
            except TimeoutError:
                runtime.stderr_task.cancel()

        runtime.status = "closed"
        self._record(
            runtime.server_id,
            "closed",
            status="closed",
            pid=process.pid,
            exit_code=process.returncode,
            killed=killed,
            elapsed_ms=_elapsed_ms(started_at),
            stderr_tail=tuple(runtime.stderr_lines),
        )

    def _failed_call(
        self,
        server_id: str,
        tool_name: str,
        message: str,
        error_type: str,
        started_at: float,
        extra: dict[str, Any] | None = None,
    ) -> McpCallResult:
        diagnostics = {
            "server_id": server_id,
            "tool_name": tool_name,
            "error_type": error_type,
            "elapsed_ms": _elapsed_ms(started_at),
        }
        if extra:
            diagnostics.update(extra)
        return McpCallResult(
            ok=False,
            content_text="",
            data={},
            error_message=message,
            diagnostics=diagnostics,
        )

    def _ensure_server_diag(self, server_id: str, **values: Any) -> dict[str, Any]:
        servers = self._diagnostics.setdefault("servers", {})
        server_diag = servers.setdefault(server_id, {"events": []})
        server_diag.update(values)
        return cast(dict[str, Any], server_diag)

    def _record(self, server_id: str, event_type: str, **values: Any) -> None:
        event = {"type": event_type, "server_id": server_id, **values}
        self._diagnostics.setdefault("events", []).append(event)
        server_diag = self._ensure_server_diag(server_id, **values)
        server_diag["last_event"] = event_type
        server_diag.setdefault("events", []).append(event)


def _runtime_from_config(config: Any) -> _ServerRuntime:
    server_id = str(_get_value(config, "server_id", "") or "")
    command = str(_get_value(config, "command", "") or "")
    args = tuple(str(arg) for arg in (_get_value(config, "args", ()) or ()))
    env = dict(os.environ)
    for key, value in dict(_get_value(config, "env", {}) or {}).items():
        env.setdefault(str(key), str(value))
    for key in tuple(_get_value(config, "secret_env_keys", ()) or ()):
        key_str = str(key)
        if key_str in os.environ:
            env[key_str] = os.environ[key_str]
    return _ServerRuntime(
        server_id=server_id,
        command=command,
        args=args,
        env=env,
        initialize_timeout_ms=int(
            _get_value(config, "initialize_timeout_ms", _DEFAULT_INITIALIZE_TIMEOUT_MS)
            or _DEFAULT_INITIALIZE_TIMEOUT_MS
        ),
        call_timeout_ms=int(
            _get_value(config, "call_timeout_ms", _DEFAULT_CALL_TIMEOUT_MS)
            or _DEFAULT_CALL_TIMEOUT_MS
        ),
    )


def _get_value(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def _get_bool(config: Any, name: str, default: bool) -> bool:
    return bool(_get_value(config, name, default))


def _timeout_seconds(timeout_ms: int) -> float:
    return max(timeout_ms, 1) / 1000


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _decode_json_line(line: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise _McpProtocolError(f"invalid JSON-RPC line: {exc}") from exc
    if not isinstance(payload, dict):
        raise _McpProtocolError("JSON-RPC line must be an object")
    return payload


def _extract_content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    for key in ("content_text", "text"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_structured_data(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "structuredContent", "structured_content"):
        value = result.get(key)
        if isinstance(value, dict):
            data = dict(value)
            data["raw_result"] = result
            return data
    return {"raw_result": result}
