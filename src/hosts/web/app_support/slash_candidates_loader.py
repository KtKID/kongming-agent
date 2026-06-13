"""Slash candidates 加载器 — lifespan 调用，构建 commands + skills 合并列表。"""

from __future__ import annotations

from pathlib import Path


async def load_slash_candidates() -> list[dict[str, str]]:
    from commands.registry import build_builtin_registry
    from infrastructure.config.paths import get_kongming_home
    from prompting.skills.skill_loader import load_skill_specs

    home = get_kongming_home()
    skill_specs_list = await load_skill_specs(home, workspace=Path.cwd(), event_sinks=[])
    builtin_commands = build_builtin_registry().list_commands("web")

    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for cmd in builtin_commands:
        key = cmd.slash.lstrip("/").lower()
        seen.add(key)
        candidates.append(
            {
                "slash": cmd.slash,
                "title": cmd.title,
                "description": cmd.description,
                "source": "command",
            }
        )
    for spec in skill_specs_list:
        key = spec.name.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "slash": f"/{spec.name}",
                "title": spec.name,
                "description": spec.description,
                "source": "skill",
            }
        )
    return candidates
