"""
司天配置模型——pydantic 校验，住在 sitian 包内而非 config_loader。

Role:
    定义 SiTianConfig / SiTianSourceConfig / SiTianAnalyzerConfig 等 pydantic 模型，
    供 YAML 加载和 CLI 参数校验。刻意放在 sitian 包内而非 config_loader，
    切断 ``web → config_loader → core`` 传递依赖。

Owns:
    - source kind 枚举（generic_channel / claude_project / codex_project / claude_workspace）
    - output_subdir 路径验证
    - analyzer 配置（interests / prompt / LLM 参数）
    - scanner 配置（include / exclude / mtime 窗口）

Does not own:
    - 运行时状态模型（models.py 的 frozen dataclass）
    - 配置加载/解析（config_loader 或 cli.py）
    - 扫描逻辑（scanners.py）

Called by:
    - sitian 内几乎所有模块（作为配置入参）
    - config_loader.models（类型引用）
    - cli.py（构造默认配置）

Key outputs:
    - SiTianConfig（顶层配置）
    - SiTianSourceConfig（单个 source 配置）
    - SiTianScannerConfig / SiTianAnalyzerConfig（子配置）

Change risks:
    - 字段增删需同步 YAML 配置文件和 config_loader 引用
    - kind 枚举变化需同步 scanners.py 分派逻辑
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


class SiTianAnalyzerConfig(BaseModel):
    """LLM 分析层配置。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False)
    model_name: str = Field(default="")
    base_url: str = Field(default="")
    api_key_env: str = Field(default="")
    max_tokens: int = Field(default=2048, gt=0)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    timeout: int = Field(default=30, gt=0)
    max_context_chars: int = Field(default=50000, gt=0)
    skip_if_unchanged: bool = Field(default=True)
    full_log_enabled: bool = Field(default=False)
    """记录完整 LLM 提示词 + 回复到 $SITIAN_ROOT/full-log/（审计日志）。"""


class SiTianInterestsConfig(BaseModel):
    """用户兴趣配置，注入 LLM system prompt。"""

    model_config = ConfigDict(extra="forbid")

    projects: list[str] = Field(default_factory=list)
    focus: str = Field(default="")


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
    analyzer: SiTianAnalyzerConfig = Field(default_factory=SiTianAnalyzerConfig)
    interests: SiTianInterestsConfig = Field(default_factory=SiTianInterestsConfig)
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
    "SiTianAnalyzerConfig",
    "SiTianConfig",
    "SiTianInterestsConfig",
    "SiTianScannerConfig",
    "SiTianSourceConfig",
]
