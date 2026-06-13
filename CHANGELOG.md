# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号采用 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)。

## [0.5.0] – 2026-06-13 *(tag: `v0.5`)*

本版本是 v0.1 后的公开仓库追平版本，把 5 月到 6 月沉淀在
`private-main` 的公开白名单代码合并到 GitHub `main`。核心变化来自
[#21]，并吸收了此前已合入的 Web baseline、NetworkManager、agent workflow、
CLI 审批管理器、CI gate、MiniMax M3 等 PR。

### Added

- **Web UI 完整基线**（[#12]）
  - 公开仓库补齐 React/Vite 前端、聊天运行时、频道 provider、白板、文件抽屉、
    审批 inbox、auto-approval 控件、工作流看板和配套测试。
  - 前端新增 ChatManager / ChatRenderAdapter / ChatTimelineStore，把 Claude、
    Codex、generic_chat 三类频道统一到事件化渲染链路。
- **generic_chat 走 NetworkManager**（[#13]）
  - generic 频道接入 NetworkManager、协议日志、history frame 测试基线和
    WebSocket 诊断日志。
  - 新增日志查看相关后端模块与前端入口，便于排查频道帧、连接状态和历史回放。
- **Agent workflow 编排基础设施**（[#14]）
  - 新增 agent workflow / subagent / strategy 管理主链路，支持父 agent 派发子
    agent、汇总结果、回流事件和工作流上下文。
  - 引入 workflow strategy 注册体系、map_reduce 契约校验、并行策略和子 agent
    scoped permission。
- **CLI 审批管理器与共享自动审批规则**（[#15]）
  - CLI 接入 ApprovalManager，审批事件通过统一 sink 输出。
  - 自动审批规则沉淀到共享 policy/config store，CLI 与 Web 复用同一套规则语义。
- **目录级架构重组与模块门户**（[#21]）
  - 新增 `src/application/`、`src/hosts/`、`src/infrastructure/`、
    `src/prompting/`、`src/runtime_assembly/`、`src/sessions/` 等模块。
  - `core.contracts` 从单文件拆为包级协议真源，approval / event sink /
    LLM provider / prompt assembly / session / streaming / tool runtime 分文件维护。
  - Web host 拆出 routers、integrations、approvals、threads、uploads、usage、
    workflow_viewer、workspace 等子域。
- **Agent roles 与 roundtable review**（[#21]）
  - 新增 `agent_roles` 管理器和 `agent_role_tool`。
  - 新增 roundtable_review strategy、评审角色 presets、发言板、合同模型与单元测试。
- **map_reduce workflow**（[#21]）
  - 新增 planner / mapper / reducer / validator / artifacts / input materializer。
  - 支持 inline output、mapper manifest、artifact 落盘和 CLI fixture 回放测试。
- **deep_research workflow 雏形**（[#21]）
  - 新增 deep_research contracts、source provider、dedupe、fact board、jury、
    task log 与 live smoke / integration / unit 测试。
- **XSpace / 打包支持**（[#21]）
  - 新增 `packaging/` 下 Kongming Web backend sidecar 构建脚本、PyInstaller spec
    和 XSpace runtime 配置。
- **模型与 provider 配置增强**（[#11], [#21]）
  - 默认 MiniMax 路线升级到 MiniMax M3。
  - 新增 model provider 管理页面、环境变量 preset 合同测试和 Web API。
- **任务进度与 workflow viewer**（[#21]）
  - 新增 thread task progress 后端、LLM tool、前端 popover。
  - 新增 workflow viewer 后端投影与前端详情页，覆盖 map_reduce、parallel、
    roundtable_review 和 unknown strategy。

### Changed

- **架构边界重写**（[#21]）
  - CLI 从 `src/cli/` 迁移到 `src/hosts/cli/`。
  - Web 从 `src/web/` 迁移到 `src/hosts/web/`，保留宿主边界和 integrations
    子域。
  - LLM provider 从 `src/executors/llm/` 迁移到
    `src/infrastructure/llm_providers/`。
  - prompt assembly / compaction / instruction loading / skill loading 从
    `src/context/` 迁移到 `src/prompting/`。
  - session store / discovery / bootstrap / file session 迁移到 `src/sessions/`。
- **工具模块分层**（[#21]）
  - builtin tools 与 runtime registry 分离，`tools.runtime` 持有 ToolRuntime
    基础合同，`tools.builtin` 持有 file / shell / memory / schedule /
    skill / task_progress 等实现。
- **Safety 模块拆域**（[#15], [#21]）
  - approval chain、decision engine、rules、types 迁移到 `safety.approval`。
  - boundary / grants / inbox / policies 拆为独立子域。
  - auto_approval 新增 Manager 门户，统一 Web 与 CLI 的规则读取和匹配路径。
- **CI 与 publish 流程**（[#20], [#21]）
  - pre-push 测试选择逻辑独立到脚本，push 事件跑 targeted unit tests。
  - `.publish` 同步脚本进入公开白名单，后续公开发布按 feature branch 增量同步。
- **公开仓库发布方式**（[#21]）
  - 使用最终状态 PR 一次性追平公开 `main`，替代 #16/#17/#18/#19 的过期拆分 PR。

### Fixed

- **审批体验与安全规则**（[#15], [#21]）
  - 修复 CLI 审批倒计时自动同意、终端默认拒绝、审批元数据对齐和 approval rules
    manager 边界问题。
  - generic_chat 与 CLI 共享自动审批规则，减少通道间行为漂移。
- **Workflow / map_reduce 回归**（[#14], [#21]）
  - 修复 inline map-reduce output 支持和 CLI fixture 回放。
  - 补齐 agent workflow manager、strategy manager、map_reduce、roundtable review、
    deep_research 的单元与集成测试。
- **Web 运行时与频道稳定性**（[#12], [#13], [#21]）
  - 修复 generic 首条消息 thread、WebSocket preset refresh、thread preset 更新、
    workspace dock、composer 控件、日志读取、run sidecar options 等回归点。
- **测试与类型检查**（[#20], [#21]）
  - 大规模迁移后补齐 import、DTO、fixture、路径和测试目录重命名。
  - GitHub CI 在 #21 上完成 fmt / lint / typecheck / targeted unit tests。

### Notes

- v0.5 的公开 release tag 指向 #21 合并后的 `main`：`770d079a`。
- #16 / #17 / #18 / #19 已由 #21 覆盖并关闭。
- LLM PR review 对 #21 因 GitHub diff 文件数超过 300 个返回 `PullRequest.diff too_large`，
  未执行实际代码审查；CI 结果为主要自动验证依据。

## [0.1.3] – 2026-04-25

本版本引入**长期记忆系统**与**可观测的 prompt 装配**两条新主线，并把 LLM
provider 升级到流式 + reasoning + raw dump 三件套。

### Added

- **Memory Snapshot v0.1.3**：跨会话长期记忆基础模块
  - `src/memory/` — `MemoryStore`（多文件冻结快照 + 活态条目）、`safety_write`
    （内容扫描 + 原子写入 + 条目级去重）
  - `src/tools/memory_tool.py` — Memory tool（`view` / `add` / `replace` /
    `remove`），写入走 capability + approval chain，对模型透出 `usage` 百分比
  - `src/host/memory_refresh_sink.py` — 监听 `history.compact` 自动重新加载
    memory，emit `memory.snapshot.refreshed`
  - `Config.evolution.memory` — `enabled` / `root_path` / `inject_prompt` /
    `read_max_chars` / `view_max_chars`，5 个 `KONGMING_EVOLUTION_MEMORY_*`
    env 覆盖
  - `safety/capability_policy.py` — 新增 `memory` capability，按 `add` /
    `replace` / `remove` 区分写权限
- **Prompts Package**：AGENT / TOOLS / USER 三段 system prompt 模板化
  - `src/prompts/templates/` — 内置三段模板，启动时物化到
    `<KONGMING_HOME>/prompts/`，用户可改不丢更新
  - `src/context/prompts_loader.py` — 读取 + 剔除 HTML 注释 + `\n\n` 裸拼
- **LLM 流式输出**（`SupportsLLMStream` 正交 Protocol）
  - `LLMStreamChunk` 契约：`reasoning.delta` / `content.delta` /
    `tool_call.start|arguments.delta|end` / `message.done`
  - `src/executors/llm/sse_reader.py` — provider-agnostic SSE 行解析
  - `src/executors/llm/openai_compat_stream_parser.py` — OpenAI 兼容流解析
  - Runner emit `content.delta` / `reasoning.delta` / `llm.chunk.first` /
    `llm.stream.end` 事件，便于度量 TTFT
- **Reasoning Adapter**（provider-agnostic 思考深度映射）
  - `src/executors/llm/reasoning.py` — `effort` → `ResolvedReasoningPlan` →
    payload patch，三种 adapter：`none` / `glm_thinking_budget` /
    `anthropic_compatible_reasoning`
  - 配置 `model.reasoning_profiles` 按模型名/前缀声明能力，CLI `--reasoning-effort`
    覆盖配置
- **Prompt Debug Dump**：每轮 system prompt + 完整 history 落盘 JSON
  - `src/observability/prompt_debug_dump.py`
  - CLI `--debug` flag 启用，输出到 `<KONGMING_HOME>/debug/`
- **Anthropic Messages API 原生 provider**
  - `src/executors/llm/anthropic_messages.py` — `system` 字段提至顶层、tool 格式
    适配、reasoning 映射
  - `Config.model.provider="anthropic"` 走原生路径
- **File session backend**：append-only JSONL 持久化
  - `Config.session.backend="file"` + `file_store_path`
  - 每个 session 一个目录，跨进程恢复
- **httpx client 复用**：`BaseLLMProvider`
  - `src/executors/llm/base.py` — 单进程内复用 httpx.AsyncClient + 统一重试
  - 子类 OpenAI / Anthropic 仅写 endpoint + payload 转换
- **Raw LLM dump**：调试用完整 request/response 落盘
  - `Config.trace.raw_llm` / `KONGMING_TRACE_RAW_LLM=1`
  - 输出到 `<KONGMING_HOME>/debug/raw-llm-*.json`
- **统一 home 目录**：`get_kongming_home()`
  - `src/config_loader/paths.py` — 所有 `.kongming/` 路径单一来源
- **`.env` 自动加载**：`load_config()` 默认读取项目根 `.env`
  - 不覆盖已有 env，支持 CI / 容器场景
- **配置层增强**：env 覆盖项扩到 36 个，按 `KONGMING_<SECTION>_<FIELD>` 命名
- **CLI / 脚本**
  - `--debug` flag 保存 prompt debug
  - `scripts/cli-dump.sh` / `scripts/smoke-dump.sh` 调试入口
- **新增资源**：`assets/logo*.png`
- **测试**：unit + e2e 覆盖到 611 用例（v0.1.2 时约 100 用例）
  - 含 `tests/unit/streaming/` 流式 fixture、reasoning adapter、SSE reader、
    memory store / safety_write / tool、prompts loader、prompt debug dump、
    file session、capability policy 等

### Changed

- **Runner 接入 InputAssembler**：system prompt 注入 + compact 由 assembler
  统一接管，`Runner._seed_messages` 不再双重注入
- **Compactor 默认关闭**：`Config.compactor.enabled=false`
  - 避免短对话场景被误压缩；要开启显式设为 `true`
- **system prompt 物化**：`AgentSpec.instructions` 仍可用，但 CLI 装配改用
  `prompts/` 模板物化路径
- **架构边界白名单**：`.importlinter` 加入 `memory` 包，分层与 `tools | context |
  executors | observability` 同层

### Fixed

针对本次 v0.1.3 自发的 CR 审查报告（[reports/cr/](./reports/cr/)）：

- **CLI 启动崩溃**：非空 memory snapshot 时 `Event(data=...)` 抛 TypeError，
  改为 `payload=`
- **Memory add 去重**：从子串包含改为 entry-level exact match（按
  `ENTRY_DELIMITER` 拆分），`"hello"` 不再被 `"hello world"` 错误吞掉
- **scan_content 误伤**：`system:` 越权正则放宽，`"macOS system: Darwin 24.6"`
  等环境事实通过；越权关键词（ignore/override/forget/...）仍拦截
- **Memory 私有 API 泄露**：`memory/store.py` 导出公开常量
  `MEMORY_MAX_CHARS` / `USER_MAX_CHARS` 与公开方法 `MemoryStore.read_target()`，
  `tools/memory_tool` 不再访问下划线符号
- **死代码清理**：`memory/safety_write._reload_target()` 删除，避免与 docstring
  描述矛盾
- **Memory capability P0**：MemoryTool 写入未在 capability policy 登记的修复
- **测试静态检查**：`tests/unit/test_memory_*.py` 补 `from pathlib import Path`，
  消除 ruff F821 历史遗留

### Notes

- v0.1.3 不含 LearningLoop / 外部 memory provider / 语义搜索 / 语义去重，
  这些留给后续版本
- Memory tool 写入仍走 approval chain，默认 `auto_allow` 在 capability=memory 时
  仍要求 permission 检查

---

## [0.1.2] – 2026-04-22

### Added

- **Pre-commit 软编译钩子**：commit 前自动跑 ruff / lint-imports / mypy /
  pytest-unit
- **统一 config**：YAML 多文件合并到 `config/setting.yaml`，所有可调参数集中
- **Anthropic Messages API 原生 provider**：早期版本（v0.1.3 进一步完善）
- **Raw LLM dump**：调试用 request/response 落盘（基础版）
- **架构边界合约**：`.importlinter` 强制 layered dependency direction，
  Tool Runtime 不直连 safety policy

### Changed

- File session backend 第一版（v0.1.3 完善 fsync / 跨进程恢复）

---

## [0.1.0] – 2026-04 *(tag: `v0.1`)*

首次公开发布。

### Added

- 核心运行内核
  - `core/runner.py` 唯一 turn 推进入口
  - `core/contracts.py` 跨模块共享协议（Session / Tool / ApprovalProvider /
    LLMProvider / EventSink / MessageCompactor / ToolLookup）
- Tool registry + 内置工具（read_file / write_file / shell）
- 三种 ApprovalProvider（interactive / auto_allow / auto_deny）+ Capability /
  Permission policy + SafetyGatedApproval
- InMemorySession + SQLiteSession（context 模块）+ HistoryCompactor +
  InstructionLoader + InputAssembler
- OpenAI Chat Completions provider（v0.1.3 拆出 `BaseLLMProvider`）
- click + prompt_toolkit CLI 入口
- JsonlTraceSink 事件落盘

[0.5.0]: https://github.com/KtKID/kongming-agent/releases/tag/v0.5
[0.1.3]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1.3
[0.1.2]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1.2
[0.1.0]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1
[#21]: https://github.com/KtKID/kongming-agent/pull/21
[#20]: https://github.com/KtKID/kongming-agent/pull/20
[#15]: https://github.com/KtKID/kongming-agent/pull/15
[#14]: https://github.com/KtKID/kongming-agent/pull/14
[#13]: https://github.com/KtKID/kongming-agent/pull/13
[#12]: https://github.com/KtKID/kongming-agent/pull/12
[#11]: https://github.com/KtKID/kongming-agent/pull/11
