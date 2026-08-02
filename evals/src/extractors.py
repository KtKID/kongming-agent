"""Harness Eval 文本提取工具。

# 从模型输出中提取 JSON、Python 代码、unified diff 等结构化内容。
# 纯文本处理，无外部依赖。
"""

from __future__ import annotations

import json
import re
from typing import Any


def strip_code_fence(text: str) -> str:
    """去除单层 Markdown code fence，输入模型文本，输出内部正文。"""

    stripped = text.strip()
    match = re.fullmatch(r"```(?:[a-zA-Z0-9_-]+)?\s*(.*?)\s*```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def extract_json(text: str) -> Any:
    """从模型输出中提取 JSON，输入文本，输出解析后的 JSON 值。"""

    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def extract_python_code(text: str) -> str:
    """从模型输出中提取 Python 代码，输入文本，输出代码字符串。"""

    cleaned = text.strip()
    match = re.search(r"```python\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    match = re.search(r"```\s*(.*?)\s*```", cleaned, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return cleaned + ("\n" if cleaned else "")


def extract_diff(text: str) -> str:
    """从模型输出中提取 unified diff，输入文本，输出适合 git apply 的 patch 字符串。

    支持三种来源：```diff fenced block、裸 code fence、整段裸文本；
    并把首个 `diff --git` / `--- ` 之前的自然语言前缀剥掉，保证 git apply 不被污染。
    """

    match = re.search(r"```(?:diff|patch)\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    patch = match.group(1) if match else strip_code_fence(text)
    lines = patch.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            start = index
            break
    patch_body = "\n".join(lines[start:]).strip("\n")
    return patch_body + "\n" if patch_body else ""
