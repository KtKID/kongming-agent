"""Build the Web slash catalog manager during app assembly."""

from __future__ import annotations

from hosts.web.slash_catalog.manager import SlashCatalogManager
from hosts.web.slash_catalog.providers import build_default_providers


def build_default_slash_catalog_manager() -> SlashCatalogManager:
    """Build the default slash catalog manager for FastAPI app state."""
    return SlashCatalogManager(providers=build_default_providers())


__all__ = ["build_default_slash_catalog_manager"]
