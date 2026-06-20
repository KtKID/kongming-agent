"""文档 harness 链接静态检查。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_EXTERNAL_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "app://",
    "plugin://",
)


def _docs_harness_files() -> list[Path]:
    """返回 docs harness 必须保持本地链接有效的文档集合。"""
    module_readmes = sorted((REPO_ROOT / "docs" / "modules").glob("*/README.md"))
    return [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "AGENTS.md",
        *module_readmes,
    ]


def _iter_local_markdown_links(path: Path) -> list[tuple[int, str]]:
    """提取单个 Markdown 文件里的本地链接，返回行号和目标。"""
    links: list[tuple[int, str]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _MARKDOWN_LINK_RE.finditer(line):
            target = match.group(1).strip()
            if not target or target.startswith("#"):
                continue
            if target.startswith(_EXTERNAL_PREFIXES):
                continue
            if " " in target and not target.startswith("<"):
                target = target.split(" ", 1)[0]
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            local_target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if local_target:
                links.append((line_no, local_target))
    return links


@pytest.mark.unit
def test_docs_harness_local_links_resolve() -> None:
    """CLAUDE、AGENTS 和模块 README 中的本地 Markdown 链接必须指向现存路径。"""
    missing: list[str] = []
    for path in _docs_harness_files():
        for line_no, target in _iter_local_markdown_links(path):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{path.relative_to(REPO_ROOT)}:{line_no} -> {target}")

    assert missing == [], "\n".join(missing)
