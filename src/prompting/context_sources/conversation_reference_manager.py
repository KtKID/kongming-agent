"""Conversation reference prompt context manager.

本模块把 Web 提交的 ``conversation_references`` metadata 解析成 prompt
assembly 可注入的上下文片段。关键流程：

1. 从最新 user message metadata 读取结构化 reference 列表。
2. 对 skill reference 使用服务端 ``home/workspace`` 重新加载 SkillSpec。
3. 取匹配 ``SKILL.md`` 的规范路径并渲染为 Codex 同款 Markdown 链接。
4. 输出可前置到 user message content 的引用文本。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from core.message import Message
from prompting.skills.skill_loader import SkillSpec, load_skill_specs

SkillSpecLoader = Callable[[Path, Path | None], Awaitable[Sequence[SkillSpec]]]


@dataclass(frozen=True)
class ConversationReferenceContext:
    """服务端解析上下文。"""

    home: Path
    workspace: Path | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class ResolvedConversationReference:
    """已解析的 reference 片段。"""

    reference_id: str
    kind: str
    label: str
    content: str
    source_ref: str | None = None


class ConversationReferenceManager:
    """解析 conversation references 并生成 prompt context。"""

    def __init__(
        self,
        context: ConversationReferenceContext,
        *,
        skill_loader: SkillSpecLoader | None = None,
    ) -> None:
        """初始化 manager，输入为服务端上下文，输出为可复用解析门户。"""
        self._context = context
        self._skill_loader = skill_loader or _default_skill_loader

    async def resolve_for_prompt(self, message: Message) -> list[ResolvedConversationReference]:
        """解析最新 user message 的 references，输入为消息，输出为 prompt 片段。"""
        references = self._extract_references(message)
        if not references:
            return []

        skill_refs = [
            ref
            for ref in references
            if ref.get("kind") == "skill" and ref.get("activation") == "inject_context"
        ]
        if not skill_refs:
            return []

        specs = await self._skill_loader(self._context.home, self._context.workspace)
        specs_by_name = {spec.name: spec for spec in specs}
        resolved: list[ResolvedConversationReference] = []
        for ref in skill_refs:
            skill_name = _skill_name_from_ref(ref)
            if not skill_name:
                resolved.append(_diagnostic(ref, "missing skill name"))
                continue
            spec = specs_by_name.get(skill_name)
            if spec is None:
                resolved.append(_diagnostic(ref, f"skill not found: {skill_name}"))
                continue
            resolved.append(_selected_skill(ref, spec))
        return resolved

    def render_prompt_context(self, resolved: Sequence[ResolvedConversationReference]) -> str:
        """渲染 prompt context，输入为解析结果，输出为可前置到 user content 的文本。"""
        blocks = [item.content for item in resolved if item.content.strip()]
        return "\n\n".join(blocks)

    @staticmethod
    def _extract_references(message: Message) -> list[dict[str, Any]]:
        """从 Message.metadata 提取 reference dict 列表。"""
        refs = (message.metadata or {}).get("conversation_references")
        if not isinstance(refs, list):
            return []
        return [ref for ref in refs if isinstance(ref, dict)]


async def _default_skill_loader(home: Path, workspace: Path | None) -> Sequence[SkillSpec]:
    """调用真实 skill loader，输入为 home/workspace，输出为 SkillSpec 序列。"""
    return await load_skill_specs(home, workspace=workspace, event_sinks=[])


def _skill_name_from_ref(ref: dict[str, Any]) -> str:
    """从 reference 中提取 skill 名。"""
    raw_ref = ref.get("ref")
    if isinstance(raw_ref, str) and raw_ref.startswith("skill:"):
        return raw_ref.split(":", 1)[1].strip()
    metadata = ref.get("metadata")
    if isinstance(metadata, dict):
        raw_name = metadata.get("name")
        if isinstance(raw_name, str):
            return raw_name.strip()
    return ""


def _selected_skill(
    ref: dict[str, Any],
    spec: SkillSpec,
) -> ResolvedConversationReference:
    """渲染 selected skill Markdown 链接。"""
    label = _label(ref, fallback=spec.name)
    source_ref = _source_ref(ref)
    path = str(spec.body_path)
    content = f"[${spec.name}]({path})"
    return ResolvedConversationReference(
        reference_id=str(ref.get("id") or ""),
        kind="skill",
        label=label,
        content=content,
        source_ref=source_ref,
    )


def _diagnostic(ref: dict[str, Any], message: str) -> ResolvedConversationReference:
    """渲染解析失败诊断片段。"""
    label = _label(ref, fallback="unknown")
    reference_id = str(ref.get("id") or "")
    source_ref = _source_ref(ref)
    content = (
        f'<selected_reference_error id="{escape(reference_id)}" '
        f'label="{escape(label)}">{escape(message)}</selected_reference_error>'
    )
    return ResolvedConversationReference(
        reference_id=reference_id,
        kind=str(ref.get("kind") or "unknown"),
        label=label,
        content=content,
        source_ref=source_ref,
    )


def _label(ref: dict[str, Any], *, fallback: str) -> str:
    """提取 UI label。"""
    raw = ref.get("label")
    return raw.strip() if isinstance(raw, str) and raw.strip() else fallback


def _source_ref(ref: dict[str, Any]) -> str | None:
    """提取 source_ref。"""
    raw = ref.get("source_ref")
    return raw if isinstance(raw, str) and raw else None


__all__ = [
    "ConversationReferenceContext",
    "ConversationReferenceManager",
    "ResolvedConversationReference",
]
