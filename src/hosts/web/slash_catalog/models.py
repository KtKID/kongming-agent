"""Web slash catalog DTO 和请求上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from hosts.web.protocol.conversation_references import ConversationReferenceTemplate

SlashCatalogDiagnosticSeverity = Literal["info", "warning", "error"]
SlashCatalogItemKind = Literal["workflow_strategy", "workflow_run", "command", "skill"]
SlashCatalogActionKind = Literal["insert_text", "bind_reference", "guide_payload", "open_viewer"]
SlashCatalogBackendKind = Literal["generic_chat", "claude_code", "codex"]


class SlashCatalogDiagnosticDTO(BaseModel):
    """Catalog provider 诊断信息，输入为 code/message，输出为前端可展示 DTO。"""

    code: str
    severity: SlashCatalogDiagnosticSeverity = "warning"
    message: str
    path: str | None = None


class SlashCatalogGroupDTO(BaseModel):
    """首层分组 DTO，输入为 provider 摘要，输出为 SlashMenu 首层数据。"""

    id: str
    title: str
    description: str = ""
    order: int = 0
    item_count: int = 0
    diagnostics: list[SlashCatalogDiagnosticDTO] = Field(default_factory=list)


class SlashCatalogItemDTO(BaseModel):
    """二层候选项 DTO，输入为注册源条目，输出为 SlashMenu 可选择项。"""

    id: str
    group_id: str
    kind: SlashCatalogItemKind
    title: str
    description: str = ""
    source_ref: str
    order: int = 0
    section_id: str | None = None
    slash: str | None = None
    insert_text: str | None = None
    action: SlashCatalogActionKind = "insert_text"
    reference_template: ConversationReferenceTemplate | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[SlashCatalogDiagnosticDTO] = Field(default_factory=list)


class SlashCatalogGroupsResponseDTO(BaseModel):
    """Catalog 首层响应 DTO，输入为 groups，输出为 REST response。"""

    groups: list[SlashCatalogGroupDTO]


class SlashCatalogGroupItemsResponseDTO(BaseModel):
    """Catalog 二层响应 DTO，输入为 group 和 items，输出为 REST response。"""

    group: SlashCatalogGroupDTO
    items: list[SlashCatalogItemDTO]


@dataclass(frozen=True)
class SlashCatalogProviderResult:
    """Provider result with items and diagnostics for manager aggregation."""

    items: tuple[SlashCatalogItemDTO, ...] = ()
    diagnostics: tuple[SlashCatalogDiagnosticDTO, ...] = ()


class SlashCatalogProvider(Protocol):
    """Slash catalog provider protocol used by the manager."""

    group_id: str
    title: str
    description: str
    order: int

    async def list_items(self, context: SlashCatalogContext) -> SlashCatalogProviderResult:
        """List provider items for a request context."""
        ...


@dataclass(frozen=True)
class SlashCatalogContext:
    """Catalog 请求上下文，输入为 Web app state，输出为 provider 读取依赖。"""

    home: Path
    workspace: Path
    config: Any | None = None
    thread_id: str | None = None
    backend_kind: SlashCatalogBackendKind | None = None


__all__ = [
    "SlashCatalogActionKind",
    "SlashCatalogBackendKind",
    "SlashCatalogContext",
    "SlashCatalogDiagnosticDTO",
    "SlashCatalogDiagnosticSeverity",
    "SlashCatalogGroupDTO",
    "SlashCatalogGroupItemsResponseDTO",
    "SlashCatalogGroupsResponseDTO",
    "SlashCatalogItemDTO",
    "SlashCatalogItemKind",
    "SlashCatalogProvider",
    "SlashCatalogProviderResult",
]
