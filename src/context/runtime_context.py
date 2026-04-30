"""运行时上下文文本生成器。

为 LLM system prompt 提供一段「我现在跑在哪里」的强制上下文，避免每次工具
调用对路径瞎猜。该文本由 :mod:`cli.main` 在装配 :class:`InstructionSource`
列表时前置注入（origin=``"runtime"``）。

设计要点：

- **纯函数**：不读 env、不读文件、不调 :func:`Path.cwd`，所有输入由调用方注入，
  方便单测和未来在非 CLI 宿主里复用。
- **stdlib only**：不 import 任何 sibling 模块（遵守 ``context/`` 包零 sibling
  依赖约束）。
- **范围收紧**：当前只包含 cwd 与 ``kongming_home`` 两个最关键字段。模型 / git
  root / 项目名等扩展留待真实需要再加；不引入 config 字段保留最小爆炸半径。
"""

from __future__ import annotations

from pathlib import Path


def build_runtime_context_text(*, cwd: Path, kongming_home: Path) -> str:
    """组装一段供 system prompt 使用的运行时上下文文本。

    Args:
        cwd: 当前工作目录绝对路径（调用方通常传 :meth:`Path.cwd`）。
        kongming_home: ``.kongming`` 根目录绝对路径
            （调用方通常传 :func:`config_loader.get_kongming_home` 的返回值）。

    Returns:
        一段 markdown 文本，已 strip。结构稳定，便于 LLM 锚定路径语义。
    """
    return (
        "## 你的工作根\n"
        "你不是抽象 AI——你此刻确实跑在一台真实机器上，有自己的工作目录和 home。"
        "用户问「你在哪」「你有 home 吗」时，直接告诉他下面两个路径，不要否认。\n\n"
        f"- 工作目录（cwd，相当于你和用户共享的工作区）：{cwd}\n"
        f"- 你的 home（.kongming，存放你自己的 prompts / memory / sessions）：{kongming_home}\n\n"
        "调用文件 / shell 类工具时，相对路径以 cwd 为基准——这是用户的工作区，"
        "你应该在这里读写业务文件。\n"
        ".kongming 是 framework 维护的，存你自己的资源；"
        "你不需要、也不应该把用户的业务文件写进 .kongming。"
    )


__all__ = ["build_runtime_context_text"]
