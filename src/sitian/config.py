"""SiTian 配置模型。

把 ``SiTianConfig`` / ``SiTianSourceConfig`` 放在 ``sitian`` 包内，
让 ``config_loader.models`` 不再传递性依赖 ``core``——避免 web 应用层
通过 ``config_loader`` 触达 core 触发架构合约失败
（``web app shell must not import core``）。

设计原则：
- core 只放跨模块共享协议（Session / Tool / ApprovalProvider 等），
  业务配置模型属于业务模块自己的事；
- SiTianConfig 仅被 ``config_loader.models`` 和 ``sitian.*`` 使用，
  住在 sitian 包里语义最清；
- 运行时状态模型 ``SiTianSourceRuntimeState`` 是 ``sitian.models`` 里的
  frozen dataclass，跟这里的配置 schema 不同（一个是配置入参，
  一个是运行时持久化数据），不要混淆。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SiTianSourceConfig(BaseModel):
    """单个 SiTian source 声明。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["generic_channel", "claude_project", "codex_project", "claude_workspace"]
    path: str
    scan_interval_sec: int | None = Field(default=None, gt=0)
    top_n: int | None = Field(default=None, gt=0)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id", "path")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source id/path must not be empty")
        return stripped

    def resolved_scan_interval_sec(self, default_scan_interval_sec: int) -> int:
        """返回该 source 的实际扫描间隔（source 级覆盖优先）。"""
        return self.scan_interval_sec or default_scan_interval_sec


class SiTianConfig(BaseModel):
    """顶层 SiTian 配置 section。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal["v1"] = "v1"
    default_scan_interval_sec: int = Field(default=300, gt=0)
    idle_sleep_sec: int = Field(default=30, gt=0)
    sources: list[SiTianSourceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_source_ids(self) -> SiTianConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(
                    f"sitian.sources contains duplicate id={source.id!r}; source id must be unique"
                )
            seen.add(source.id)
        return self


__all__ = [
    "SiTianConfig",
    "SiTianSourceConfig",
]
