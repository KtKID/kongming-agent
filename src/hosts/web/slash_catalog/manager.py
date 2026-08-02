"""Web slash catalog manager 门户。"""

from __future__ import annotations

from hosts.web.slash_catalog.models import (
    SlashCatalogContext,
    SlashCatalogDiagnosticDTO,
    SlashCatalogGroupDTO,
    SlashCatalogItemDTO,
    SlashCatalogProvider,
    SlashCatalogProviderResult,
)


class SlashCatalogGroupNotFound(ValueError):
    """未知 slash catalog group 错误，输入为 group_id，输出为可映射 Web 错误。"""

    def __init__(self, group_id: str, available_group_ids: tuple[str, ...]) -> None:
        self.group_id = group_id
        self.available_group_ids = available_group_ids
        super().__init__(
            f"unknown slash catalog group {group_id!r}; "
            f"available_group_ids={list(available_group_ids)!r}"
        )


class SlashCatalogManager:
    """聚合 slash catalog providers 的 Web 门户。"""

    def __init__(self, providers: tuple[SlashCatalogProvider, ...]) -> None:
        """初始化 manager，输入为 provider 列表，输出为可查询 catalog 门户。"""
        ordered = tuple(sorted(providers, key=lambda item: item.order))
        seen: set[str] = set()
        for provider in ordered:
            if provider.group_id in seen:
                raise ValueError(f"duplicate slash catalog group: {provider.group_id}")
            seen.add(provider.group_id)
        self._providers = ordered
        self._provider_by_group = {provider.group_id: provider for provider in ordered}

    async def list_groups(self, context: SlashCatalogContext) -> list[SlashCatalogGroupDTO]:
        """列出首层 groups，输入为请求上下文，输出为稳定顺序 group DTO。"""
        groups: list[SlashCatalogGroupDTO] = []
        for provider in self._providers:
            result = await self._safe_list_items(provider, context)
            groups.append(self._group_from_provider(provider, result))
        return groups

    async def list_group_items(
        self,
        group_id: str,
        context: SlashCatalogContext,
    ) -> tuple[SlashCatalogGroupDTO, list[SlashCatalogItemDTO]]:
        """列出指定 group 的 items，输入为 group_id 和 context，输出为 group/items。"""
        provider = self._provider_by_group.get(group_id)
        if provider is None:
            raise SlashCatalogGroupNotFound(group_id, self.group_ids)
        result = await self._safe_list_items(provider, context)
        return self._group_from_provider(provider, result), list(result.items)

    async def list_legacy_candidates(
        self,
        context: SlashCatalogContext,
    ) -> list[dict[str, str]]:
        """输出 legacy flat candidates，输入为 context，输出为 command+skill 兼容列表。"""
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for group_id in ("command", "skill"):
            provider = self._provider_by_group.get(group_id)
            if provider is None:
                continue
            result = await self._safe_list_items(provider, context)
            for item in result.items:
                if item.slash is None:
                    continue
                key = item.slash.lstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "slash": item.slash,
                        "title": item.title,
                        "description": item.description,
                        "source": item.kind,
                    }
                )
        return candidates

    @property
    def group_ids(self) -> tuple[str, ...]:
        """返回已注册 group ID，输入为 manager 状态，输出为稳定 ID 元组。"""
        return tuple(provider.group_id for provider in self._providers)

    async def _safe_list_items(
        self,
        provider: SlashCatalogProvider,
        context: SlashCatalogContext,
    ) -> SlashCatalogProviderResult:
        """安全调用 provider，输入为 provider/context，输出为 result 或错误诊断。"""
        try:
            return await provider.list_items(context)
        except Exception as exc:
            return SlashCatalogProviderResult(
                diagnostics=(
                    SlashCatalogDiagnosticDTO(
                        code=f"{provider.group_id}.load_failed",
                        severity="error",
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                )
            )

    @staticmethod
    def _group_from_provider(
        provider: SlashCatalogProvider,
        result: SlashCatalogProviderResult,
    ) -> SlashCatalogGroupDTO:
        """由 provider 和 result 生成 group，输入为 provider/result，输出为 DTO。"""
        return SlashCatalogGroupDTO(
            id=provider.group_id,
            title=provider.title,
            description=provider.description,
            order=provider.order,
            item_count=len(result.items),
            diagnostics=list(result.diagnostics),
        )


__all__ = [
    "SlashCatalogGroupNotFound",
    "SlashCatalogManager",
]
