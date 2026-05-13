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


class SiTianScannerConfig(BaseModel):
    """Scanner 行为旋钮。"""

    model_config = ConfigDict(extra="forbid")

    recent_session_window_days: int = Field(default=3, ge=0)
    """最近活动窗口（天）。0 = 不过滤窗口、读所有 session。"""

    session_recent_user_messages: int = Field(default=1, ge=0)
    """每个 session 取最后 N 条 user 消息。0 = 不取。"""

    session_recent_assistant_messages: int = Field(default=1, ge=0)
    """每个 session 取最后 N 条 assistant 消息。0 = 不取。"""

    session_message_max_chars: int = Field(default=500, ge=0)
    """每条消息最大字符数；超出末尾加 "…"。0 = 不截断。"""


class SiTianConfig(BaseModel):
    """顶层 SiTian 配置 section。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal["v1"] = "v1"
    default_scan_interval_sec: int = Field(default=300, gt=0)
    idle_sleep_sec: int = Field(default=30, gt=0)
    output_subdir: str | None = Field(default=None)
    """产物落子目录名（相对 SiTianRecords root_dir）。

    例：``output_subdir="claude"`` → 所有产物落到 ``<root>/claude/`` 下。
    设 ``None`` 时直接落 ``<root>/``（向后兼容）。
    用于多 kind 共存时隔离产物（``claude/`` / ``codex/`` / ``general/``）。
    """
    scanner: SiTianScannerConfig = Field(default_factory=SiTianScannerConfig)
    sources: list[SiTianSourceConfig] = Field(default_factory=list)

    @field_validator("output_subdir")
    @classmethod
    def _output_subdir_clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip().strip("/")
        if not stripped:
            raise ValueError("output_subdir must not be empty / only slashes")
        if ".." in stripped.split("/"):
            raise ValueError("output_subdir must not contain '..'")
        return stripped

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
    "SiTianScannerConfig",
    "SiTianSourceConfig",
]
