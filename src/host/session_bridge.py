"""agent 事件流 ↔ 宿主事件流的双向翻译层。

:class:`SessionBridge` 是 host 模块里**唯一**能把一次"用户输入 → runtime
执行 → 输出"完整串起来的对象。它不承担：

- 装配：那是 :class:`executors.agent_runtime.native_runtime.NativeRuntime.build`
  的事；``SessionBridge`` 只消费一个**已经装好**的 runtime。
- 事件落盘：观测事件通过 :class:`core.contracts.EventSink` 从 runtime
  内部 fan-out，不走 bridge。如果宿主想看 UI 级别进度，通过
  :class:`host.cli_adapter.CLIEventSink` 注册到 ``NativeRuntime.build
  (event_sinks=[...])`` 即可。
- 第二套 run loop：turn 推进永远在 :class:`core.runner.Runner` 里。

这一层存在的真正价值，是把"读输入 / 写输出"这一对 CLI/HTTP/游戏宿主都
会反复写的胶水逻辑集中到一处，让 runtime 本身对宿主完全无感。
"""

from __future__ import annotations

from core.result import Result
from executors.agent_runtime.native_runtime import NativeRuntime
from host.base import HostAdapter


class SessionBridge:
    """把 :class:`HostAdapter` 和 :class:`NativeRuntime` 缝起来。

    每个 ``SessionBridge`` 实例绑定一个 ``session_id``；同一个实例反复
    ``run_once`` 会走进 ``NativeRuntime`` 内部的 session cache，从而
    保留多轮对话历史。
    """

    def __init__(
        self,
        *,
        runtime: NativeRuntime,
        adapter: HostAdapter,
        session_id: str,
        echo_final_content: bool = True,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._runtime = runtime
        self._adapter = adapter
        self._session_id = session_id
        # 流式路径下 CLIStreamSink 已经实时打印过完整 content（打字机效果），
        # 此处再 write_output(final.content) 会造成视觉双重输出。
        # cli/main.py 在装配时按 `cfg.stream.enabled and isinstance(runtime.llm,
        # SupportsLLMStream)` 计算后传入 False；非流式或 fallback 路径默认 True。
        self._echo_final_content = echo_final_content

    # ------------------------------------------------------------------
    # 访问器
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def runtime(self) -> NativeRuntime:
        return self._runtime

    @property
    def adapter(self) -> HostAdapter:
        return self._adapter

    # ------------------------------------------------------------------
    # 单轮 / 交互循环
    # ------------------------------------------------------------------

    async def run_once(
        self,
        user_input: str,
        *,
        reasoning_effort: str | None = None,
    ) -> Result:
        """执行一次 run，把结果写回 adapter。

        不抛异常——runner 会把所有异常收口到 :class:`core.result.Result`
        的 ``error`` 字段，这里只做结果 → 文本的翻译。
        """
        result = await self._runtime.run(
            user_input,
            session_id=self._session_id,
            reasoning_effort=reasoning_effort,
        )

        final = result.final_message
        if self._echo_final_content and final is not None and final.content:
            await self._adapter.write_output(final.content)

        if result.error is not None:
            await self._adapter.write_output(
                f"[error] {type(result.error).__name__}: {result.error.message}"
            )

        usage = result.metadata.get("usage")
        if isinstance(usage, dict) and usage:
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            total = usage.get("total_tokens", 0)
            await self._adapter.write_output(f"[tokens ↑{prompt} ↓{completion} ={total}]")

        return result

    async def run_loop(self) -> None:
        """交互式主循环：读输入 → run_once → 再读，直到 EOF / Ctrl-C。

        ``HostAdapter.read_input`` 返回 ``None`` 时正常退出。
        ``KeyboardInterrupt`` / ``EOFError`` 也视为退出信号而不是故障。
        """
        try:
            while True:
                try:
                    user_input = await self._adapter.read_input()
                except (KeyboardInterrupt, EOFError):
                    break
                if user_input is None:
                    break
                if not user_input.strip():
                    continue
                try:
                    await self.run_once(user_input)
                except (KeyboardInterrupt, EOFError):
                    # 用户在 agent 执行过程中按下 Ctrl-C：中断当前轮，
                    # 但不视为进程故障；回到读输入等待下一条。
                    await self._adapter.write_output("[interrupted]")
                    continue
        finally:
            await self._adapter.close()


__all__ = ["SessionBridge"]
