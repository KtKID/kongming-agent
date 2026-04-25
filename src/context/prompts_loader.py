"""System prompt 三段模板装配器。

职责边界（对齐 `docs/prompt-modules-v0.1.3/README.md`）：

1. **启动物化**：首次启动时把 `src/prompts/templates/{AGENT,TOOLS,USER}.md`
   三份内置模板复制到 `<home>/prompts/` 下。已存在的文件**不覆盖**，用户手动
   编辑一律保留。
2. **读取 + 去注释 + 空白跳过**：读三份文件 → 剔除 HTML 注释（``<!-- ... -->``）
   → strip → 非空段用 ``\\n\\n`` 裸拼。不加外层 header，让用户自己在 md 里
   控制 markdown 结构。
3. **不设 Python 字符串 fallback**：启动时物化保证文件存在，这是唯一 fallback
   机制。物化失败（目录不可写等）时抛异常，CLI 启动明确失败比静默 degrade 更安全。

刻意不做的事：

- 不 emit events（装配器是纯函数，trace 归因交给上层 `InstructionLoader`
  的 origin 标注；event_sinks 参数会污染调用链）
- 不支持模板变量 / frontmatter / 跨文件引用
- 不按启用工具集动态筛选 TOOLS.md（用户按需自行维护文件内容）

依赖方向：

- 不 import `core`（本模块不需要）
- 只依赖 stdlib + `importlib.resources`
- 不 import 任何 sibling 模块（cli / safety / observability / memory 等）
- `context.prompts_loader` 结构上和 `context.instruction_loader` 同层，
  后者继续承接多来源合并（agent_spec / 外部文件 / env），本模块只负责
  生成 "agent_spec 基础文本"（供 InstructionLoader.load 的 agent_instructions 入参）
"""

from __future__ import annotations

import asyncio
import re
from importlib import resources
from pathlib import Path

# 三段模板文件名，顺序即装配顺序（AGENT → TOOLS → USER）。
TEMPLATE_FILENAMES: tuple[str, ...] = ("AGENT.md", "TOOLS.md", "USER.md")

# HTML 注释剔除正则。`[\s\S]*?` 跨行非贪婪匹配，见
# https://docs.python.org/3/library/re.html 关于 re.DOTALL 的等价写法。
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")


def _strip_html_comments(text: str) -> str:
    """剔除字符串中的所有 HTML 注释（``<!-- ... -->``，允许跨行）。

    Args:
        text: 原始文本。

    Returns:
        所有 HTML 注释被替换为空串后的文本；其他内容原样保留。

    规则：
    - 非贪婪匹配：``<!--`` 到最近一个 ``-->`` 之间的内容被剔除。
    - 不处理嵌套注释（HTML 本身也不允许嵌套）。
    - 对跨行注释有效（``[\\s\\S]`` 而非 ``.``）。
    """
    return _HTML_COMMENT_RE.sub("", text)


def _read_template_content(name: str) -> str:
    """从内置 ``prompts.templates`` package 读取指定模板文件。

    用 ``importlib.resources`` 定位，兼容 wheel / 源码两种部署方式。

    Args:
        name: 模板文件名，如 ``"AGENT.md"``。

    Returns:
        模板文件的 UTF-8 文本内容。

    Raises:
        FileNotFoundError: 模板文件不存在（通常意味着打包遗漏）。
    """
    # `files("prompts")` 返回 MultiplexedPath / PosixPath，`/ "templates" / name`
    # 拼出具体资源；`.read_text(encoding="utf-8")` 读取。
    template_root = resources.files("prompts") / "templates"
    resource = template_root / name
    return resource.read_text(encoding="utf-8")


def _materialize_template(target: Path, template_name: str) -> bool:
    """若 ``target`` 不存在，则把模板内容写入。同步版本，供 ``asyncio.to_thread`` 使用。

    Args:
        target: 用户目录下的目标文件路径。
        template_name: 模板文件名（用于从 package 读取源内容）。

    Returns:
        实际物化了返回 ``True``；目标已存在跳过返回 ``False``。
    """
    if target.exists():
        return False
    content = _read_template_content(template_name)
    target.write_text(content, encoding="utf-8")
    return True


def _read_and_clean_sync(target: Path) -> str:
    """同步读文件 + 剔除 HTML 注释 + strip。供 ``asyncio.to_thread`` 使用。

    非 UTF-8 文件会让 ``read_text`` 抛 :class:`UnicodeDecodeError`，按约定**冒泡**
    到调用方，避免把非法编码文件误当作空段吞掉。
    """
    raw = target.read_text(encoding="utf-8")
    return _strip_html_comments(raw).strip()


async def materialize_and_load_prompts(home: Path) -> str:
    """启动时物化模板 + 读取装配三段 system prompt 文本。

    完整流程：

    1. ``home / "prompts"`` 目录不存在 → 创建。
    2. 对 :data:`TEMPLATE_FILENAMES` 每个文件：目标不存在 → 从内置模板复制；
       存在 → 不覆盖（用户编辑永远胜出）。
    3. 读取每个目标文件 → 剔除 HTML 注释 → strip。
    4. 跳过 strip 后为空的段；其余用 ``"\\n\\n"`` 裸拼返回。

    Args:
        home: `.kongming/` 根目录的绝对路径（通常由
            ``config_loader.get_kongming_home()`` 产出）。

    Returns:
        装配好的 system prompt 基础文本；所有段都为空时返回空字符串。

    Raises:
        PermissionError / OSError: 目录不可写或文件读写失败——视为用户环境
            配置错误，**不静默 degrade**，让 CLI 启动明确失败便于排查。
        UnicodeDecodeError: 用户目录下的 md 文件不是合法 UTF-8。
        FileNotFoundError: 内置模板包加载失败（通常意味着 wheel 打包遗漏
            ``src/prompts/templates/*.md``）。
    """
    prompts_dir = home / "prompts"
    await asyncio.to_thread(prompts_dir.mkdir, parents=True, exist_ok=True)

    # 物化：缺失即复制
    for name in TEMPLATE_FILENAMES:
        target = prompts_dir / name
        await asyncio.to_thread(_materialize_template, target, name)

    # 读取 + 清洗
    sections: list[str] = []
    for name in TEMPLATE_FILENAMES:
        target = prompts_dir / name
        cleaned = await asyncio.to_thread(_read_and_clean_sync, target)
        if cleaned:
            sections.append(cleaned)

    return "\n\n".join(sections)


__all__ = [
    "TEMPLATE_FILENAMES",
    "materialize_and_load_prompts",
]
