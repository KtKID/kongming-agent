# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号采用 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)。

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

[0.1.3]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1.3
[0.1.2]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1.2
[0.1.0]: https://github.com/KtKID/kongming-agent/releases/tag/v0.1
