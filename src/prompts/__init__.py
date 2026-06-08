"""kongming-agent 内置 system prompt 模板。

本 package 存放 AGENT / TOOLS / USER 三段默认模板；启动时由
`prompting.prompts_loader.materialize_and_load_prompts()` 复制到用户目录
`.kongming/prompts/` 下。用户可编辑自己目录下的文件；项目内模板仅作 fallback 源。

严格约束：
- 本 package 不导出任何 Python 符号；模板通过 `importlib.resources` 读取
- 不 import 任何 sibling 模块（core / sessions / prompting / config_loader 等）
"""

from __future__ import annotations

__all__: list[str] = []
