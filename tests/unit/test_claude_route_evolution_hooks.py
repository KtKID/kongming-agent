"""验证 claude_code/route.py evolution hook 的变量传递和 import 边界。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_route_does_not_import_evolution_internals() -> None:
    """D8: route.py 不 import evolution.state_store / reviewer_runtime / event_bus / store。"""
    route_src = Path("src/hosts/web/integrations/claude_code/route.py").read_text(encoding="utf-8")
    tree = ast.parse(route_src)

    forbidden = {
        "evolution.state_store",
        "evolution.reviewer_runtime",
        "evolution.event_bus",
        "evolution.store",
        "evolution.models",
        "evolution.evidence_selector",
    }

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    violations = imported_modules & forbidden
    assert not violations, f"route.py imports evolution internals: {violations}"


def test_route_only_imports_allowed_evolution_modules() -> None:
    """route.py 只应 import evolution.evolution_manager 和 evolution.claude_transcript_provider。"""
    route_src = Path("src/hosts/web/integrations/claude_code/route.py").read_text(encoding="utf-8")
    tree = ast.parse(route_src)

    allowed = {
        "evolution.evolution_manager",
        "evolution.claude_transcript_provider",
    }

    evolution_imports: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("evolution.")
        ):
            evolution_imports.add(node.module)

    unexpected = evolution_imports - allowed
    assert not unexpected, f"route.py imports unexpected evolution modules: {unexpected}"


def test_dispatch_has_evolution_params() -> None:
    """_dispatch 函数签名包含 evolution_manager / bound_cwd / bound_claude_tid 参数。"""
    route_src = Path("src/hosts/web/integrations/claude_code/route.py").read_text(encoding="utf-8")
    tree = ast.parse(route_src)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch":
            arg_names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            assert "evolution_manager" in arg_names
            assert "bound_cwd" in arg_names
            assert "bound_claude_tid" in arg_names
            return

    raise AssertionError("_dispatch function not found in route.py")
