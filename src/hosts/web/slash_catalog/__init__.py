"""Web slash catalog 公共入口。"""

from hosts.web.slash_catalog.manager import (
    SlashCatalogGroupNotFound,
    SlashCatalogManager,
)
from hosts.web.slash_catalog.models import (
    SlashCatalogBackendKind,
    SlashCatalogContext,
    SlashCatalogDiagnosticDTO,
    SlashCatalogGroupDTO,
    SlashCatalogGroupItemsResponseDTO,
    SlashCatalogGroupsResponseDTO,
    SlashCatalogItemDTO,
)

__all__ = [
    "SlashCatalogBackendKind",
    "SlashCatalogContext",
    "SlashCatalogDiagnosticDTO",
    "SlashCatalogGroupDTO",
    "SlashCatalogGroupItemsResponseDTO",
    "SlashCatalogGroupNotFound",
    "SlashCatalogGroupsResponseDTO",
    "SlashCatalogItemDTO",
    "SlashCatalogManager",
]
