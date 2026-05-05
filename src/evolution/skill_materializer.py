"""Self evolution skill materializer.

把单条 accepted nutrient 落成 workspace skill，范围固定在
``<cwd>/.kongming/skills/<skill-name>/SKILL.md``。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from evolution.models import EvolutionNutrient

_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---\n?", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SKILL_ROOT = Path(".kongming") / "skills"
_SKILL_FILENAME = "SKILL.md"


MaterializationMode = Literal["create", "update"]
MaterializationStatus = Literal["written", "skipped"]


@dataclass(frozen=True)
class SkillMaterializationResult:
    source_nutrient_id: str
    skill_name: str
    mode: MaterializationMode
    status: MaterializationStatus
    path: str
    written: bool


class SkillMaterializer:
    """把 accepted nutrient promote 成 workspace skill。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self._skills_root = (self._workspace / _SKILL_ROOT).resolve(strict=False)
        self._lock = asyncio.Lock()

    async def apply(self, nutrient: EvolutionNutrient) -> SkillMaterializationResult:
        async with self._lock:
            return await asyncio.to_thread(self._apply_sync, nutrient)

    def _apply_sync(self, nutrient: EvolutionNutrient) -> SkillMaterializationResult:
        skill_name = _derive_skill_name(nutrient)
        existing_path = _find_existing_skill_path(self._skills_root, skill_name)
        target_path = existing_path or (self._skills_root / skill_name / _SKILL_FILENAME)
        mode: MaterializationMode = "update" if existing_path else "create"
        _ensure_path_under_root(self._skills_root, target_path.parent)

        current_text = _read_text(target_path)
        next_text = _render_skill_document(
            skill_name=skill_name,
            nutrient=nutrient,
            current_text=current_text,
        )
        if current_text == next_text:
            return SkillMaterializationResult(
                source_nutrient_id=nutrient.nutrient_id,
                skill_name=skill_name,
                mode=mode,
                status="skipped",
                path=str(target_path),
                written=False,
            )

        _atomic_write(target_path, next_text)
        return SkillMaterializationResult(
            source_nutrient_id=nutrient.nutrient_id,
            skill_name=skill_name,
            mode=mode,
            status="written",
            path=str(target_path),
            written=True,
        )


async def materialize_skill(
    workspace: Path,
    nutrient: EvolutionNutrient,
) -> SkillMaterializationResult:
    """便捷入口：materialize 单条 accepted nutrient。"""
    return await SkillMaterializer(workspace).apply(nutrient)


def _derive_skill_name(nutrient: EvolutionNutrient) -> str:
    candidates = [nutrient.title, *nutrient.tags, nutrient.summary, nutrient.nutrient_id]
    for candidate in candidates:
        slug = _slugify(candidate)
        if slug and slug != "skill":
            return slug
    digest = hashlib.sha1(nutrient.nutrient_id.encode("utf-8")).hexdigest()[:12]
    return f"skill-{digest}"


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_value).strip("-")
    if not slug:
        return ""
    return slug[:80].rstrip("-")


def _find_existing_skill_path(skills_root: Path, skill_name: str) -> Path | None:
    direct = skills_root / skill_name / _SKILL_FILENAME
    if direct.exists():
        return direct
    if not skills_root.exists():
        return None
    for path in sorted(skills_root.glob(f"*/{_SKILL_FILENAME}")):
        text = _read_text(path)
        if text is None:
            continue
        meta, _body = _split_frontmatter(text)
        existing_name = meta.get("name")
        if isinstance(existing_name, str) and existing_name.strip() == skill_name:
            return path
    return None


def _ensure_path_under_root(skills_root: Path, path: Path) -> None:
    real_root = skills_root.resolve(strict=False)
    real_path = path.resolve(strict=False)
    try:
        real_path.relative_to(real_root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace skills root: {path}") from exc


def _read_text(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    body = text[match.end() :].lstrip("\n")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, body
    return (meta if isinstance(meta, dict) else {}), body


def _render_skill_document(
    *,
    skill_name: str,
    nutrient: EvolutionNutrient,
    current_text: str | None,
) -> str:
    existing_meta: dict[str, Any] = {}
    existing_body = ""
    if current_text:
        existing_meta, existing_body = _split_frontmatter(current_text)

    description = _build_description(nutrient)
    when_to_use = _build_when_to_use(nutrient)
    frontmatter = _merge_frontmatter(
        existing_meta=existing_meta,
        skill_name=skill_name,
        description=description,
        when_to_use=when_to_use,
    )
    managed_block = _render_managed_block(nutrient)
    if existing_body.strip():
        body = _upsert_managed_block(existing_body, nutrient.nutrient_id, managed_block)
    else:
        body = _render_initial_body(
            skill_name=skill_name,
            description=description,
            when_to_use=when_to_use,
            managed_block=managed_block,
        )
    return f"---\n{frontmatter}---\n\n{body.rstrip()}\n"


def _build_description(nutrient: EvolutionNutrient) -> str:
    candidates = [nutrient.summary, nutrient.title, nutrient.content]
    for candidate in candidates:
        line = _collapse_whitespace(candidate)
        if line:
            return _truncate(line, 180)
    return "Accepted evolution skill"


def _build_when_to_use(nutrient: EvolutionNutrient) -> str:
    summary = _collapse_whitespace(nutrient.summary)
    if summary:
        return _truncate(summary, 220)
    title = _collapse_whitespace(nutrient.title)
    if title:
        return _truncate(f"Use when the task matches {title}.", 220)
    return "Use when the task matches this accepted evolution workflow."


def _merge_frontmatter(
    *,
    existing_meta: dict[str, Any],
    skill_name: str,
    description: str,
    when_to_use: str,
) -> str:
    allowed_tools = existing_meta.get("allowed-tools")
    if allowed_tools is None:
        allowed_tools = existing_meta.get("allowed_tools")
    paths = existing_meta.get("paths")
    merged_paths = [f".kongming/skills/{skill_name}"]
    if isinstance(paths, list):
        for item in paths:
            if isinstance(item, str) and item not in merged_paths:
                merged_paths.append(item)

    payload: dict[str, Any] = {
        "name": existing_meta.get("name")
        if isinstance(existing_meta.get("name"), str) and existing_meta.get("name", "").strip()
        else skill_name,
        "description": existing_meta.get("description")
        if isinstance(existing_meta.get("description"), str)
        and existing_meta.get("description", "").strip()
        else description,
        "when-to-use": existing_meta.get("when-to-use")
        if isinstance(existing_meta.get("when-to-use"), str)
        and existing_meta.get("when-to-use", "").strip()
        else (
            existing_meta.get("when_to_use")
            if isinstance(existing_meta.get("when_to_use"), str)
            and existing_meta.get("when_to_use", "").strip()
            else when_to_use
        ),
        "paths": merged_paths,
    }
    if isinstance(allowed_tools, list):
        payload["allowed-tools"] = [str(item) for item in allowed_tools]
    return cast(str, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def _render_initial_body(
    *,
    skill_name: str,
    description: str,
    when_to_use: str,
    managed_block: str,
) -> str:
    return (
        f"# {skill_name}\n\n"
        f"## Purpose\n{description}\n\n"
        f"## When To Use\n{when_to_use}\n\n"
        "## Managed evolution nutrients\n\n"
        f"{managed_block}\n"
    )


def _upsert_managed_block(body: str, nutrient_id: str, block: str) -> str:
    start_marker = _marker("start", nutrient_id)
    end_marker = _marker("end", nutrient_id)
    pattern = re.compile(
        rf"\n?{re.escape(start_marker)}.*?{re.escape(end_marker)}\n?",
        re.DOTALL,
    )
    if pattern.search(body):
        updated = pattern.sub(f"\n{block}\n", body)
        return updated.strip() + "\n"
    if "## Managed evolution nutrients" not in body:
        suffix = "## Managed evolution nutrients\n\n" + block
    else:
        suffix = block
    trimmed = body.rstrip()
    if trimmed:
        return f"{trimmed}\n\n{suffix}\n"
    return f"{suffix}\n"


def _render_managed_block(nutrient: EvolutionNutrient) -> str:
    turns = ", ".join(str(turn) for turn in nutrient.evidence_turns) or "n/a"
    tags = ", ".join(nutrient.tags) or "none"
    steps = _render_steps(nutrient.content)
    return (
        f"{_marker('start', nutrient.nutrient_id)}\n"
        f"### Nutrient `{nutrient.nutrient_id}`\n\n"
        f"- title: {nutrient.title}\n"
        f"- kind: {nutrient.kind}\n"
        f"- confidence: {nutrient.confidence:.2f}\n"
        f"- evidence_turns: {turns}\n"
        f"- source_run_id: {nutrient.source_run_id}\n"
        f"- source_session_id: {nutrient.source_session_id}\n"
        f"- tags: {tags}\n\n"
        "#### Summary\n"
        f"{nutrient.summary.strip()}\n\n"
        "#### Workflow\n"
        f"{steps}\n"
        f"{_marker('end', nutrient.nutrient_id)}"
    )


def _render_steps(content: str) -> str:
    items = _split_steps(content)
    if not items:
        return "- Keep the accepted workflow as written."
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _split_steps(content: str) -> list[str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    lines = [_strip_bullet_prefix(line) for line in normalized.splitlines() if line.strip()]
    if len(lines) > 1:
        return [_truncate(_collapse_whitespace(line), 400) for line in lines]
    fragments = re.split(r"[。！？!?；;]\s*|\.\s+", normalized)
    items = [_collapse_whitespace(fragment) for fragment in fragments if fragment.strip()]
    if len(items) > 1:
        return [_truncate(item, 400) for item in items]
    return [_truncate(_collapse_whitespace(normalized), 400)]


def _strip_bullet_prefix(value: str) -> str:
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", value).strip()


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _marker(kind: Literal["start", "end"], nutrient_id: str) -> str:
    return f"<!-- kongming:evolution:{nutrient_id}:{kind} -->"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


__all__ = [
    "SkillMaterializationResult",
    "SkillMaterializer",
    "materialize_skill",
]
