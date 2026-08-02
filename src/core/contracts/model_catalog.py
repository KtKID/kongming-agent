"""模型目录解析门户的跨模块结构协议。"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from core.contracts.llm_provider import ReasoningEffort

SelectionT_contra = TypeVar("SelectionT_contra", contravariant=True)
RuntimeT = TypeVar("RuntimeT")
CredentialT_co = TypeVar("CredentialT_co", covariant=True)


@runtime_checkable
class ModelCatalogResolver(Protocol[SelectionT_contra, RuntimeT, CredentialT_co]):
    """跨模块解析 selection、runtime snapshot 与 credential 的稳定入口。"""

    def resolve_runtime(
        self,
        selection: SelectionT_contra,
        *,
        preset_id: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> RuntimeT:
        """解析单次运行的 immutable 模型快照。"""
        ...

    def resolve_credential(self, runtime: RuntimeT) -> CredentialT_co:
        """在 provider 构造边界解析 credential。"""
        ...


__all__ = ["ModelCatalogResolver"]
