from __future__ import annotations

import ast
from pathlib import Path


def test_evolution_store_construction_stays_inside_manager() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    allowed = {repo_root / "src" / "evolution" / "evolution_manager.py"}
    offenders: list[str] = []

    for path in (repo_root / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            attr = func.attr if isinstance(func, ast.Attribute) else None
            if name not in {"EvolutionStore", "EvolutionStateStore"} and attr not in {
                "EvolutionStore",
                "EvolutionStateStore",
            }:
                continue
            if path not in allowed:
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == []
