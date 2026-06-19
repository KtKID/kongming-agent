"""Workflow prompt catalog 生成模块。

本脚本负责把 workflow strategy registry 中的完整 description 投影为短 system
prompt listing。作用是让父 LLM 在 system prompt 前段发现可用 workflow，同时把
payload schema、示例和风险提示留给 describe 工具按需披露。
关键执行流程：WorkflowPromptCatalogManager 读取 description → 规范化 entries →
WorkflowPromptListingFormatter 渲染固定模板并计算 hash。
关键函数：snapshot_default_workflows 读取默认 registry，snapshot_from_manager 读取运行期
manager，render 生成 workflow_catalog instruction 文本。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from application.agent_workflows.strategies.description import WorkflowStrategyDescription

if TYPE_CHECKING:
    from application.agent_workflows.manager import AgentWorkflowManager


WORKFLOW_PROMPT_SOURCE_VERSION = "workflow-prompt-catalog-source-v1"
WORKFLOW_PROMPT_TEMPLATE_VERSION = "workflow-prompt-catalog-template-v1"
WORKFLOW_PROMPT_ORIGIN = "workflow_catalog"


@dataclass(frozen=True)
class WorkflowPromptCatalogEntry:
    """单个 workflow prompt listing 条目。"""

    mode: str
    title: str
    usage_scenarios: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowPromptCatalogSnapshot:
    """workflow prompt catalog 的规范化快照。"""

    entries: tuple[WorkflowPromptCatalogEntry, ...]
    source_version: str
    registry_modes: tuple[str, ...]
    listing_hash: str


@dataclass(frozen=True)
class WorkflowPromptListingRender:
    """formatter 输出的可注入 system prompt 片段。"""

    text: str
    origin: str
    template_version: str
    listing_hash: str


@dataclass(frozen=True)
class WorkflowPromptCacheKey:
    """Web instructions cache 的 workflow prompt 语义键。"""

    base_instructions_hash: str
    workflow_template_version: str
    workflow_listing_hash: str


class WorkflowPromptCatalogManager:
    """workflow prompt catalog 的边界类。"""

    def snapshot_default_workflows(self) -> WorkflowPromptCatalogSnapshot:
        """读取默认 strategy registry，输入为空，输出 workflow prompt catalog 快照。"""
        from application.agent_workflows.manager import AgentWorkflowManager

        descriptions = AgentWorkflowManager.list_default_workflow_strategy_descriptions()
        return self.snapshot_from_descriptions(descriptions)

    def snapshot_from_manager(
        self,
        manager: AgentWorkflowManager,
    ) -> WorkflowPromptCatalogSnapshot:
        """读取运行期 manager，输入为 AgentWorkflowManager，输出 workflow prompt catalog 快照。"""
        descriptions = [
            manager.describe_workflow_strategy(entry.mode)
            for entry in manager.list_workflow_strategies()
        ]
        return self.snapshot_from_descriptions(descriptions)

    def snapshot_from_descriptions(
        self,
        descriptions: Iterable[WorkflowStrategyDescription],
    ) -> WorkflowPromptCatalogSnapshot:
        """从 description 列表生成快照，输入为策略说明，输出规范化快照。"""
        entries = tuple(
            sorted(
                (_entry_from_description(description) for description in descriptions),
                key=lambda entry: entry.mode,
            )
        )
        registry_modes = tuple(entry.mode for entry in entries)
        listing_hash = _stable_hash(
            {
                "source_version": WORKFLOW_PROMPT_SOURCE_VERSION,
                "entries": [_entry_payload(entry) for entry in entries],
            }
        )
        return WorkflowPromptCatalogSnapshot(
            entries=entries,
            source_version=WORKFLOW_PROMPT_SOURCE_VERSION,
            registry_modes=registry_modes,
            listing_hash=listing_hash,
        )


class WorkflowPromptListingFormatter:
    """workflow prompt listing 的稳定 formatter。"""

    template_version = WORKFLOW_PROMPT_TEMPLATE_VERSION
    origin = WORKFLOW_PROMPT_ORIGIN

    def render(self, snapshot: WorkflowPromptCatalogSnapshot) -> WorkflowPromptListingRender:
        """渲染 workflow catalog，输入为快照，输出可注入的 prompt 片段。"""
        if not snapshot.entries:
            return WorkflowPromptListingRender(
                text="",
                origin=self.origin,
                template_version=self.template_version,
                listing_hash=_stable_hash(
                    {"template_version": self.template_version, "entries": []}
                ),
            )

        lines = [
            "# workflow catalog",
            "你可以使用下列 workflow。需要具体调用参数格式时调用 "
            'describe_agent_workflow_strategy(mode="xx")；执行时调用 run_agent_workflow。',
            "",
        ]
        for entry in snapshot.entries:
            lines.append(f"- mode: {entry.mode}")
            lines.append(f"  title: {entry.title}")
            lines.append("  使用场景:")
            for scenario in entry.usage_scenarios:
                lines.append(f"    - {scenario}")
        text = "\n".join(lines).rstrip()
        listing_hash = _stable_hash(
            {
                "template_version": self.template_version,
                "entries": [_entry_payload(entry) for entry in snapshot.entries],
            }
        )
        return WorkflowPromptListingRender(
            text=text,
            origin=self.origin,
            template_version=self.template_version,
            listing_hash=listing_hash,
        )


def _entry_from_description(
    description: WorkflowStrategyDescription,
) -> WorkflowPromptCatalogEntry:
    """投影单个 description，输入为策略说明，输出 prompt catalog entry。"""
    mode = _required_text(description.mode, field="mode")
    title = _required_text(description.title, field=f"{mode}.title")
    usage_scenarios = tuple(
        scenario.strip() for scenario in description.when_to_use if scenario.strip()
    )
    if not usage_scenarios:
        raise ValueError(f"workflow strategy {mode!r} must define when_to_use")
    return WorkflowPromptCatalogEntry(
        mode=mode,
        title=title,
        usage_scenarios=usage_scenarios,
    )


def _required_text(value: str, *, field: str) -> str:
    """规范化必填文本，输入为字段值和字段名，输出去空白文本。"""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"workflow prompt catalog field {field} must be non-empty")
    return normalized


def _entry_payload(entry: WorkflowPromptCatalogEntry) -> dict[str, object]:
    """生成稳定 hash payload，输入为 entry，输出仅含 prompt 字段的 dict。"""
    return {
        "mode": entry.mode,
        "title": entry.title,
        "usage_scenarios": list(entry.usage_scenarios),
    }


def _stable_hash(payload: object) -> str:
    """计算稳定短 hash，输入为 JSON 可序列化 payload，输出 sha256 短值。"""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_default_workflow_prompt_listing() -> WorkflowPromptListingRender:
    """构建默认 workflow listing，输入为空，输出 formatter render 结果。"""
    manager = WorkflowPromptCatalogManager()
    formatter = WorkflowPromptListingFormatter()
    return formatter.render(manager.snapshot_default_workflows())


def build_workflow_prompt_cache_key(
    *,
    base_instructions_hash: str,
    render: WorkflowPromptListingRender,
) -> WorkflowPromptCacheKey:
    """构建 cache key，输入为基础 prompt hash 和 render，输出 workflow prompt cache key。"""
    return WorkflowPromptCacheKey(
        base_instructions_hash=base_instructions_hash,
        workflow_template_version=render.template_version,
        workflow_listing_hash=render.listing_hash,
    )


__all__ = [
    "WORKFLOW_PROMPT_ORIGIN",
    "WORKFLOW_PROMPT_SOURCE_VERSION",
    "WORKFLOW_PROMPT_TEMPLATE_VERSION",
    "WorkflowPromptCacheKey",
    "WorkflowPromptCatalogEntry",
    "WorkflowPromptCatalogManager",
    "WorkflowPromptCatalogSnapshot",
    "WorkflowPromptListingFormatter",
    "WorkflowPromptListingRender",
    "build_default_workflow_prompt_listing",
    "build_workflow_prompt_cache_key",
]
