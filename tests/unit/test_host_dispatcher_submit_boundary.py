"""HostDispatcher submit 边界门禁测试。"""

from __future__ import annotations

import ast
from pathlib import Path

from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.threads.manager import ThreadManager

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST_ENTRY_ROOTS = (
    _REPO_ROOT / "src" / "hosts" / "cli",
    _REPO_ROOT / "src" / "hosts" / "web",
)
_FORBIDDEN_HOST_CALL_ATTRS = frozenset(
    {
        "enqueue_text",
        "try_send_now_text",
        "_try_send_now_text",
        "_try_send_now",
        "_submit_in_background",
    }
)


def _python_files(root: Path) -> list[Path]:
    """列出指定目录下的 Python 文件。"""
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _forbidden_attribute_calls(path: Path) -> list[tuple[int, str]]:
    """扫描非法 HostDispatcher helper 调用，返回行号和属性名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_HOST_CALL_ATTRS:
            hits.append((node.lineno, node.attr))
    return hits


def test_cli_web_host_entries_only_use_submit_for_text_dispatch() -> None:
    """CLI/Web 宿主入口只能通过 HostDispatcher.submit 投递文本。"""
    violations: list[str] = []
    for root in _HOST_ENTRY_ROOTS:
        for path in _python_files(root):
            for lineno, attr in _forbidden_attribute_calls(path):
                rel = path.relative_to(_REPO_ROOT)
                violations.append(f"{rel}:{lineno}: illegal HostDispatcher.{attr} access")

    assert violations == []


def test_host_dispatcher_public_surface_locks_legacy_dispatch_helpers() -> None:
    """HostDispatcher 公共面不暴露旧的绕行投递 helper。"""
    assert hasattr(HostDispatcher, "submit")
    assert not hasattr(HostDispatcher, "enqueue_text")
    assert not hasattr(HostDispatcher, "try_send_now_text")


def test_thread_manager_public_surface_locks_direct_user_mail_enqueue() -> None:
    """ThreadManager 公共面不暴露绕过 pending queue 的直投入口。"""
    assert not hasattr(ThreadManager, "enqueue_user_mail")
