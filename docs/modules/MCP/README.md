# MCP

Kongming MCP 模块把用户配置的 stdio MCP server 接入现有 ToolRegistry，并把外部 MCP tools 变成可审批、可观测、可关闭的 Kongming Tool。

## 设计理念

| 决策 | 理由 |
|------|------|
| MCP client 放在 `src/infrastructure/mcp/` | MCP transport、JSON-RPC、子进程生命周期属于基础设施，不进入 core runner。 |
| MCP tool 适配放在 `src/tools/mcp/` | 外部工具最终只暴露为 `core.contracts.Tool`，复用现有工具协议和审批链。 |
| runtime 注册由 `McpRuntimeRegistrationManager` 收口 | Web 和 CLI 共享同一套启动、注册、诊断、关闭流程，避免各宿主复制装配逻辑。 |
| 通用搜索能力独立成 `application.web_search` | `web_search` 是应用能力，不和 MiniMax 或 DeepResearch 绑定；任何用户搜索工具都可接入。 |
| DeepResearch 只消费 `ResearchSourceProvider` | 调研 workflow 不感知 MCP、MiniMax 或具体搜索工具，保持来源 provider 可替换。 |
| `web_search` alias 由 wrapper 注册 | 保留 `mcp__<server>__<tool>` canonical tool，同时把选中的搜索工具包装为稳定入口。 |
| 配置 env 是默认值，进程环境优先 | 本地 `.env` / 部署环境可覆盖地域、key 等敏感或机器相关配置，代码库配置保持可共享。 |
| 插件开关只保存 per-tool bool | MCP Tool 保留在全局 `ToolRegistry`，Web 插件开关只影响新建 `SessionEngine` 的工具白名单。 |

## 核心流程

```text
Config(mcp.servers, web_search)
  -> McpRuntimeRegistrationManager
  -> McpManager 启动 stdio server + initialize + tools/list
  -> McpToolAdapterManager 生成 mcp__server__tool
  -> ToolRegistry 注册 canonical / alias tools
  -> PluginManagementManager 同步 source=mcp 的工具元数据和 enabled bool
  -> Web runtime factory 创建新 SessionEngine 时读取 enabled bool
  -> WebSearchManager 包装搜索 tool 为 web_search，输出 title/url/domain/published_date/snippet/score
  -> build_default_registry 提供 builtin web_fetch
  -> WebResearchSourceProviderFactory 注入 DeepResearch
  -> workflow artifacts 写入 URL / snippet / fetched content / diagnostics
```

## 插件工具开关语义

Web 管理页的插件开关以 `<kongming_home>/web/plugin-tools.json` 为状态真源，只记录 MCP 工具的 `enabled` 布尔值和展示元数据。开关切换不会删除 `ToolRegistry` 里的 Tool 对象，也不会改已经创建的 `SessionEngine`；新 thread/session 创建时按当前 bool 生成 `enabled_tool_names` 快照。

`SessionEngine` 保存工具名到 Tool 对象的查找快照，Tool 对象本身保持引用共享。这样新 session 能看到最新 enabled 状态，老 session 按创建时快照继续运行，避免运行中的上下文和工具缓存被中途改写。

Runner 每轮请求 LLM 时只通过 provider tools schema 下发可用工具清单，不改写 user message。模型调用关闭或已卸载工具时，runner 回填一条 tool result，内容包含“工具不可用”，同时保留 `tool '<name>' not registered` 诊断字段供旧审计和测试消费。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `src/infrastructure/mcp/manager.py` | `McpManager` | stdio MCP server 生命周期、JSON-RPC initialize / tools/list / tools/call、跨 event loop 调用和关闭。 |
| `src/infrastructure/mcp/models.py` | `McpToolDescriptor` / `McpCallResult` | MCP descriptor 和调用结果的基础设施层结构。 |
| `src/tools/mcp/adapter.py` | `McpToolAdapterManager` / `McpToolAdapter` | MCP descriptor 到 Kongming Tool 的注册计划、canonical 命名、alias 冲突和 ToolResult 映射。 |
| `src/hosts/shared/mcp_runtime_registration.py` | `McpRuntimeRegistrationManager` | 宿主共享的 MCP/WebSearch 装配门户，负责注册 tools、发事件、持有关闭钩子。 |
| `src/application/web_search/manager.py` | `WebSearchManager` / `build_web_search_tool` | 把任意搜索 Tool 归一化为 `web_search`，支持多种搜索返回结构和 provider 错误透传。 |
| `src/application/web_search/models.py` | `WebSearchResult` / `WebSearchResponse` | 通用 Web Search 的应用层返回模型。 |
| `src/tools/builtin/web_fetch_tool.py` | `WebFetchTool` / `build_web_fetch_tool` | 内置 URL 正文读取工具，负责 URL 安全、DNS IP 校验、redirect 复查、markdown 抽取、关键词窗口和分页。 |
| `src/hosts/web/research_source_provider.py` | `WebResearchSourceProviderFactory` / `UserToolResearchSourceProviderAdapter` | 把 Web runtime 中的搜索/抓取工具适配为 DeepResearch 的 `ResearchSourceProvider`。 |
| `src/hosts/web/plugin_management/` | `PluginManagementManager` / `PluginToolStateStore` | Web 插件工具状态门户，保存 MCP 工具 enabled bool，供新 session 创建时过滤工具白名单。 |
| `src/infrastructure/config/models.py` | `McpConfig` / `WebSearchConfig` / `WebDeepResearchSourceProviderConfig` | MCP、通用搜索和 DeepResearch 来源 provider 的配置模型。 |
| `src/infrastructure/config/loader.py` | `KONGMING_WEB_SEARCH_*` env 覆盖 | web_search provider 和 DeepResearch provider 配置的 env 覆盖入口。 |
| `src/hosts/web/run.py` | Web runtime factory 集成 | Web 启动时注册 MCP tools、绑定 DeepResearch source provider、保存关闭引用。 |
| `src/hosts/cli/main.py` | CLI runtime 集成 | CLI 启动时注册 MCP tools，关闭时释放 MCP 子进程。 |
| `tests/fixtures/mcp/fake_mcp_server.py` | fake stdio MCP server | 覆盖 initialize、tools/list、tools/call、超时和错误路径。 |

## 配置

| 配置 | 说明 |
|------|------|
| `mcp.servers[].server_id` | server 命名空间，只允许字母、数字、下划线和短横线。canonical tool 名使用该值。 |
| `mcp.servers[].command` / `args` | stdio MCP server 启动命令和参数，例如 `uvx minimax-coding-plan-mcp -y`。 |
| `mcp.servers[].env` | 传给子进程的默认 env；同名进程环境变量优先。 |
| `mcp.servers[].secret_env_keys` | 从进程环境透传的敏感变量名，例如 `MINIMAX_API_KEY`。 |
| `mcp.servers[].aliases` | 把 MCP 原始 tool 暴露为额外 Kongming tool 名；`web_search` 被通用搜索 wrapper 保留。 |
| `web_search.search_tool_name` | 显式指定被包装为 `web_search` 的底层 tool，例如 `mcp__minimax__web_search`。 |
| `web_search.search_tool_names` | 自动探测候选底层搜索 tool，按顺序命中第一个存在的工具。 |
| `src/tools/builtin/web_fetch_tool.py` 顶部常量 | `web_fetch` 当前调参入口，包含 token 预算、字符换算、HTTP timeout、User-Agent、内网访问开关、垃圾正文阈值和关键词窗口。 |
| `web.deep_research_source_provider.*` | Web DeepResearch 来源 provider 的搜索/抓取工具探测配置。 |
| `<kongming_home>/web/plugin-tools.json` | Web 插件工具状态文件，保存 MCP 工具 `enabled`、`available`、server/tool 名和展示信息。 |

## 接入方式

| 方式 | 适用场景 | 入口 | 结果 |
|------|----------|------|------|
| MCP canonical tool | 任意 MCP tool 直接给 LLM 使用 | 配置 `mcp.servers[]` | 注册 `mcp__<server_id>__<tool_name>`。 |
| MCP 显式 alias | 给某个 MCP tool 起稳定业务名 | `mcp.servers[].aliases` | 额外注册 alias；冲突时跳过并写 diagnostics。 |
| 通用 `web_search` | 任意搜索工具接入搜索能力 | `web_search.search_tool_name(s)` | 注册稳定 `web_search` Tool，返回 `title/url/domain/published_date/snippet/score`；底层搜索工具缺失时返回工具缺失。 |
| 内置 `web_fetch` | URL 正文读取和分页 | `build_default_registry(web_fetch_enabled=True)` | 默认注册 `web_fetch` Tool，成功返回 `status=ok`、`content`、`total_chars`、`has_more`、`next_offset`；失败返回 `status=blocked/error`、`reason`、`suggestion`。 |
| DeepResearch search-only | 搜索工具返回 URL + snippet，暂无 fetch 工具 | `web.deep_research_source_provider.search_tool_name(s)` | 写入 `fetched/weak` 来源记录，正文来自搜索摘要。 |
| DeepResearch search + fetch | 搜索工具给 URL，fetch 工具抓正文 | `search_tool_name` + `fetch_tool_name` | 写入 `fetched/strong` 来源记录。 |
| 用户自带搜索工具 | MCP 关闭或无 server，registry 中已有搜索工具 | `web_search.search_tool_names` | runtime 仍可把该工具包装为 `web_search`。 |

完整配置示例见 [mcp-client-v0.1/06-integration-guide.md](../../spec/modules/MCP/mcp-client-v0.1/06-integration-guide.md)。

## Spec 导航

| Spec | 状态 | 说明 |
|------|------|------|
| [mcp-client-v0.1](../../spec/modules/MCP/mcp-client-v0.1/) | 已实现 | stdio MCP client、tool adapter、runtime 注册、通用 Web Search 注入、安全诊断和验证路径 |

## 已知问题 / 待完成

| 项目 | 状态 | 说明 |
|------|------|------|
| HTTP/SSE transport | 待实现 | 当前 `McpManager` 只支持 stdio。 |
| MCP resources/prompts/sampling | 待实现 | v0.1 只实现 tools/list 和 tools/call。 |
| Web 诊断面板 | 待实现 | diagnostics 已有结构化数据，前端展示还未产品化。 |
| per-tool 审批策略 | 待细化 | 当前走现有 Tool 审批链，后续可加 server/tool 级 allowlist。 |
