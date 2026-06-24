"""unit：运行数据 home 路径的静态防回归检查。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_CWD_KONGMING_PATTERNS = (
    re.compile(r"Path\.cwd\(\)\s*/\s*['\"]\.kongming['\"]"),
    re.compile(r"Path\.cwd\(\)\.joinpath\(\s*['\"]\.kongming['\"]"),
)

_CURRENT_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "config" / "README.md",
    REPO_ROOT / "config" / "sitian.local.yaml",
    REPO_ROOT / "sitian.sh",
    REPO_ROOT / "docs" / "operations" / "worktree-development.md",
    REPO_ROOT / "docs" / "integrations" / "xspace" / "tauri-kongming-migration.md",
    REPO_ROOT / "docs" / "modules" / "配置加载" / "README.md",
    REPO_ROOT / "docs" / "modules" / "命令行" / "README.md",
    REPO_ROOT / "docs" / "modules" / "XSpace打包" / "README.md",
    REPO_ROOT / "docs" / "modules" / "司天" / "README.md",
    REPO_ROOT / "docs" / "modules" / "进化" / "README.md",
    REPO_ROOT / "docs" / "modules" / "网络" / "README.md",
    REPO_ROOT / "docs" / "modules" / "Web前端" / "README.md",
    REPO_ROOT / "docs" / "spec" / "kongming-agent-v1-minimal" / "11-v1-file-layout.md",
)

_RESOLVER_CONSUMERS = (
    REPO_ROOT / "src" / "sessions" / "session_store.py",
    REPO_ROOT / "src" / "infrastructure" / "tracing" / "trace_sink.py",
    REPO_ROOT / "src" / "infrastructure" / "tracing" / "prompt_debug_dump.py",
    REPO_ROOT / "src" / "infrastructure" / "llm_providers" / "raw_dump.py",
    REPO_ROOT / "src" / "memory" / "store.py",
    REPO_ROOT / "src" / "evolution" / "store.py",
    REPO_ROOT / "src" / "sitian" / "store.py",
    REPO_ROOT / "src" / "tools" / "__init__.py",
    REPO_ROOT / "src" / "hosts" / "cli" / "main.py",
    REPO_ROOT / "src" / "hosts" / "web" / "app.py",
    REPO_ROOT / "src" / "hosts" / "web" / "run.py",
    REPO_ROOT / "src" / "hosts" / "web" / "dashboard" / "logs" / "registry.py",
    REPO_ROOT / "src" / "devtools" / "full_logger.py",
)


def _read(path: Path) -> str:
    """读取 UTF-8 文本，输出给静态断言使用。"""
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_src_does_not_define_cwd_kongming_runtime_root() -> None:
    offenders: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = _read(path)
        for pattern in _CWD_KONGMING_PATTERNS:
            if pattern.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break

    assert offenders == []


@pytest.mark.unit
def test_current_docs_do_not_describe_cwd_as_kongming_home_default() -> None:
    offenders: list[str] = []
    forbidden = (
        'Path.cwd() / ".kongming"',
        "Path.cwd()/.kongming",
        "cwd/.kongming",
        "cwd/.kongming/sitian",
        "~/.kongming/SiTian",
        "/tmp/sitian-records",
        "KONGMING_CONFIG=<xspace-app-data>/kongming/config/setting.yaml",
        "KONGMING_WEB_DIST=<bundle-resource>/web/dist",
    )
    for path in _CURRENT_DOCS:
        if not path.exists():
            continue
        text = _read(path)
        if any(item in text for item in forbidden):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


@pytest.mark.unit
def test_runtime_data_consumers_use_kongming_path_resolver() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _RESOLVER_CONSUMERS
        if "resolve_kongming_path" not in _read(path)
    ]

    assert offenders == []
