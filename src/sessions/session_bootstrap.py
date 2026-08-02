"""Session bootstrap 元数据。

:class:`SessionBootstrap` 在 CLI 阶段产出，承载"整个 session 生命周期内保持稳定"的信息。
首次 ``materialize()`` 时写入 ``manifest.json``，恢复时从 manifest 重建。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionBootstrap:
    """CLI 阶段产出的初始化元数据包。

    Attributes:
        agent_name: 本次运行的 agent 名称。
        model_name: 本次会话模型。
        instruction_sources: 指令来源列表（origin 字符串，不含 content）。
        instruction_text_hash: InstructionLoader.render(...) 最终结果的 sha256 哈希。
        instruction_text: InstructionLoader.render(...) 最终结果文本，供会话记录保留完整指令。
        created_at: session 逻辑创建时间（time.time() 浮点秒）。
        cwd: 启动目录。
        app_version: 当前程序版本号，可选。
    """

    agent_name: str
    model_name: str
    instruction_sources: list[str]
    instruction_text_hash: str
    created_at: float
    cwd: str
    instruction_text: str | None = None
    app_version: str | None = None
