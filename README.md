# kongming-agent

<p align="center">
  <img src="assets/logo.png" width="320" alt="kongming-agent logo">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/status-Alpha-orange.svg" alt="Status"></a>
</p>

> async-first 的通用 agent 内核——单一 run loop 之上叠加 LLM 自选的多 agent workflow 编排，自带主动观察者（司天）、自我进化证据链和三层安全链。

- **不只是 coding agent**——内核与场景无关，工具层可接 file / shell / memory / skill / MCP server；通过 sidecar 接入桌面/移动宿主，computer use 开发中
- **LLM 自己选编排策略**——对话中调用 `run_agent_workflow`，在 parallel / map_reduce / deep_research / roundtable_review / task_flow 里挑一个，把目标拆给独立 session 的子 agent 协作
- **司天：agent 会主动观察**——不等你提问，后台扫描工作区，物化 `work_item` 和下一步建议
- **自我进化**——每轮对话结束后台复盘证据窗口，把 review 与进化养料（memory / workflow / error 三类）写回长期记忆
- **三层安全链 + thread 本子**——DangerGuard → 审批模式 → 按 thread 独立 allow/deny，cron task 还能声明自己的审批模式
- **模型无关，本地零成本起步**——OpenAI-compat / Anthropic 都接，本地 preset 自动跳过远端 key 校验
- **工程纪律可执行**——import-linter 强制架构边界、协议单一真源、`@override` 强制、commit+push 双层 hook

---

## 目录

- [Quick Start](#quick-start)
- [独特能力](#独特能力)
- [架构一览](#架构一览)
- [功能概览](#功能概览)
- [开发命令](#开发命令)
- [设计原则](#设计原则)
- [范围与现状](#范围与现状)
- [Roadmap](#roadmap)
- [许可证](#许可证)

---

## Quick Start

### 1. 安装依赖

```bash
cd /path/to/kongming-agent
uv sync --all-extras
```

或用统一入口脚本：

```bash
./start.sh install        # macOS / Linux
```

> Windows 暂无等价入口脚本，请用 `uv sync --all-extras`。

### 2. 配模型 key

模型通过 **preset catalog**（`config/model-providers.yaml`）选择，不再用裸 base_url + 模型名拼装。**默认 preset 是 `local-gemma-4-e4b-it`（本地）**——开箱即用需要先起一个监听 `127.0.0.1:62000` 的 OpenAI 兼容服务（LM Studio / Ollama / vLLM 均可），本地 preset 自动跳过远端 key 校验。想用远端模型时切到 `minimax-m3` / `deepseek` / `bigmodel-glm5-1m` 等 preset 并配上对应的 provider key。

```bash
cp .env.example ~/.kongming/.env
# 编辑 ~/.kongming/.env 填入真实 key（用远端 preset 时才需要）：
#   MINIMAX_API_KEY=...
#   GLM_API_KEY=...
#   DEEPSEEK_API_KEY=...
```

API key 绝不写进 `config/setting.yaml`（会进 Git），只放运行时 home 的 `.env`（默认 `~/.kongming/.env`），由 catalog 的 provider 通过 `key env` 引用。真实进程 env 优先、`.env` 永不覆盖已设值。生产 / CI 可不用 `.env`，直接从 secret store 注入同名 env。

### 3. 跑起来

```bash
./start.sh cli                            # 默认 local-gemma-4-e4b-it preset（本地模型）
./start.sh cli --model-preset minimax-m3  # 切到远端 minimax-m3（需 MINIMAX_API_KEY）
make smoke                                # 冒烟验证全链路
```

> **CLI 现状**：CLI 宿主（`src/hosts/cli/`，基于 click + prompt_toolkit）目前维护投入较少，近期计划重构。它只是同步外壳——agent loop、工具、安全链、session 全在内核和 `application/`，CLI 重构不影响核心能力与 Web 宿主。**生产 / 长期使用建议优先 Web 宿主**（FastAPI + React + WS，多 thread 聊天 + 全局审批 inbox）。下文所有命令行示例仍以 CLI 为主，因为它是验证内核最快的方式。

`make smoke` 跑两步：`--workflow-smoke`（不连模型，验证 workflow 入口、审批链、map_reduce planner）+ `--smoke`（装配 + 真实模型请求一轮）。注意 smoke 脚本硬编码 `--model-preset minimax-m3`，所以 `--smoke` 这一步**需要 `MINIMAX_API_KEY`**；无 key 时可单独跑 `bash scripts/smoke.sh` 只验证不连模型的部分。看到 `[smoke] ok status=completed` 说明配置加载 → catalog 解析 → provider → runner → session → approval 全链路通。

常用 flag：

```bash
./start.sh cli --verbose                  # 打印 turn/tool/approval 事件进度
./start.sh cli --session-id demo          # 复用会话
./start.sh cli --show-reasoning           # 每轮打印模型思考内容
./start.sh cli --reasoning-effort high    # 思考深度 none/low/medium/high/max
./start.sh cli --list-sessions            # 列出已有 session
./start.sh cli --resume-last              # 恢复最近活跃 session
```

### 切换模型

切换模型 = 切 preset（endpoint 与 key 已在 catalog 声明）：

```bash
./start.sh cli --model-preset deepseek
KONGMING_MODEL_PRESET_ID=bigmodel-glm5-1m ./start.sh cli   # GLM provider 默认 preset（glm-5.2）
```

自定义 provider / 模型：把内置 catalog 复制到 `<kongming_home>/model-providers.yaml`（默认 `~/.kongming/`）后编辑，同名 provider 完整替换内置定义。详见 [config/README.md](config/README.md)。

### 审批模式

默认 `interactive`，每次 tool call 问 `[y/N]`：

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `interactive` | 人工确认（默认） | 日常使用 |
| `auto_allow` | 自动放行 | 自动化脚本、无人值守（**注意 shell tool 风险**） |
| `auto_deny` | 自动拒绝 | 压测 deny 分支、模型纯聊天不碰工具 |

```bash
KONGMING_APPROVAL_MODE=auto_allow ./start.sh cli
```

> cron 定时任务有独立的 task 级 `approval_mode`（`trust` / `fail_closed`），与 CLI 交互审批是两套，详见 [docs/modules/定时任务/](docs/modules/定时任务/README.md)。

### 常见问题速查

| 症状 | 排查 |
|---|---|
| 连不上 / 鉴权失败 | 远端 preset 检查 `~/.kongming/.env` 的 provider key 是否填了；本地 preset 确认 OpenAI 兼容服务在 `127.0.0.1:62000` 监听 |
| 模型 / preset 不存在 | `model-providers.yaml` 里的 `preset_id` 才是真源，拼写要对上；自定义 catalog 放 `~/.kongming/model-providers.yaml` |
| `HTTP 4xx` 鉴权 | provider-specific key 未注入或过期；本地模型（127.x / localhost）会跳过远端 key 校验 |
| 输出乱码 | 终端 encoding 不是 utf-8，`export LANG=zh_CN.UTF-8` |

---

## 独特能力

下面四个方向是 kongming-agent 跟同类项目真正拉开差距的地方。

### 司天：agent 会主动观察

大多数 agent 是**被动**的——你提问，它回答。司天（`src/sitian/`）让 agent 在你不在场时也工作：按配置主动扫描工作区，物化全局状态，并基于状态给出下一步建议，可选调 LLM 做分析。当前需手动调用（`run-once` / `loop`），自动触发机制见下文 [Roadmap](#roadmap)。

- **4 种 source kind**：generic_channel / claude_project / codex_project / claude_workspace，覆盖本仓库与外部 agent 工程目录
- **三层产物**：`observations.jsonl`（append-only 原始观察）→ `workspace_state.json`（覆盖写的归并状态）→ `latest_suggestions.json` + `latest_summary.md`（下一步建议）
- **work_item 归并**：把零散 observation 归并成可执行的 work_item，避免每次扫描都从零开始
- **三种触发**：CLI `run-once` / `loop` / `state`，封装脚本 `./sitian.sh`，或配置文件 `config/sitian.local.yaml`

两个平台入口等价，都是 `uv run kongming-sitian {run-once|loop|state}` 的封装（默认配置 `config/sitian.local.yaml`，产物根 `<kongming_home>/sitian/`）：

```bash
# macOS / Linux（sitian.sh，含 scan/state/loop/summary/clean，clean 带防误删保护）
./sitian.sh scan             # run-once + state + summary
./sitian.sh loop             # 持续扫描，Ctrl+C 退出
./sitian.sh clean            # 清空产物目录
```

```powershell
# Windows（SiTianRun.ps1）
.\SiTianRun.ps1              # run-once + state + 打印 latest_summary.md
.\SiTianRun.ps1 -Action loop # 持续扫描
```

### 多 agent workflow：LLM 自己选编排

`src/application/` 把编排策略做成工具，主 agent 在对话中调用 `run_agent_workflow`，选一个策略把目标拆给多个子 agent 协作完成。每个子 agent 拿到独立 session、独立模型快照和收紧过的工具权限，产出落到 `agent-workflows/<workflow_id>/`（含审计日志、子报告、最终 `result.json`）。

| 策略 mode | 名称 | 适用场景 |
|---|---|---|
| `parallel` | 并行子任务 | 多个互不依赖的子任务同时派发，全部返回后汇总报告 |
| `map_reduce` | Map-Reduce 代码分析 | 大工程代码分析拆成稳定分片，mapper 子 agent 分析、确定性 reducer 合并 `code_findings` |
| `deep_research` | Deep Research 研究工作流 | 围绕研究问题规划搜索线、收集来源、抽取事实、交叉检查、生成带引用报告 |
| `roundtable_review` | 多 Agent 圆桌评审 | 按角色并行审查代码模块设计，通过共享 ReviewBoard 质询、仲裁，输出共识 / 分歧 / 风险 |
| `task_flow` | 任务流 Task Flow | 通用计划执行：把目标拆成可视化步骤逐步完成，支持多方案向用户确认，是上述策略无法精准覆盖时的承载层 |

关键边界：

- **复用唯一 run loop**：子 agent 也走 `core.Runner.run()`，不另起编排引擎；workflow 策略只负责拆分、派发和汇总
- **工具能力单调收紧**：子 agent 实际可用工具 = `父级实际工具 ∩ 请求工具 ∩ scope 允许工具`，缺省继承父集合，显式空集合保持零工具
- **生命周期可取消**：`ActiveWorkflowHandle` 持有底层 `asyncio.Task`，可单独 `cancel_workflow(id)` 而不必连带取消整个父 run

策略目录、参数说明和示例 payload 可由 LLM 通过同一工具的 `list` / `describe` 操作读取，新增策略时只需注册一个 `WorkflowStrategy` 实现即可挂入。除 workflow 外，主 agent 也能通过 `spawn_subagent` 直接派生单个子 agent（如 code review / research），走同一个 `AgentManager.spawn()` 门户。

### 自我进化：每轮对话后复盘

`src/evolution/` 让 agent 在主对话结束后做后台复盘：裁剪终态证据窗口，fork 一个 child reviewer 做复盘，把 review record 和进化养料写回长期记忆，供下次对话使用。

- **三类进化养料**：`memory`（值得长期记住的事实）/ `workflow`（编排可改进点）/ `error`（踩过的坑）
- **三种触发**：cadence 自动触发、主 agent 显式调用 `request_evolution_review` 工具、Web `/evolve` 命令
- **五层产物**：`reviews/` + `evolution-nutrients.jsonl` + `decisions/` + `apply-jobs/` + `evolution.state.json`，全部落在 `<kongming_home>/evolution/`
- **唯一写入口**：私有写工具 `evolution_write`，结构化落盘，不绕过

### 三层安全链 + thread 本子

`src/safety/` 把一次工具调用的审批收敛为统一决策链，是工具执行的权限边界。

```
DangerGuard（写死危险集兜底）
  → 审批模式（user / llm / full_trust，可配置）
    → thread permissions 本子（每个 thread 独立 allow/deny）
```

- **DangerGuard**：写死的危险操作集合，任何模式下都不可绕过
- **审批模式**：`user`（人工确认）/ `llm`（LLM 评审）/ `full_trust`（自动放行），按 cwd 可配置
- **thread 本子**：`PermissionsManager` 按 thread 独立保存 allow/deny 到 `<kongming_home>/safety/thread_permissions/<sha256(thread_id)>.json`；子 agent 继承 root thread_id
- **task 级审批**：cron 定时任务可声明自己的 `approval_mode`（`trust` / `fail_closed`），未声明走全局默认 `trust`
- **三层证据**：审批决策、权限读写、审计事件分别落盘，可派生 usage / audit

### 接入 Claude Code / Codex SDK（实验性）

Web 宿主在 generic_chat 主线之外，平级接入了两条外部 agent 通道，验证内核的 provider / transport 扩展边界——同一个 thread、审批 inbox、状态机、历史归一化和用量统计框架，可以挂载完全不同的 agent 后端：

- **Claude Code 通道**：`src/hosts/web/integrations/claude_code/`，通过 `claude_agent_sdk` **同进程**调起，走 `/ws/claude-code` WebSocket，把 SDK 流式消息归一化成 `NormalizedMessage`，并把它的 `can_use_tool` 回调桥接到通用审批 inbox
- **Codex 通道**：`src/hosts/web/integrations/codex/`，spawn `codex exec --json` **子进程**驱动，走 `/ws/codex` WebSocket，有完整的 pydantic wire frame（`CodexCommandFrame` / `CodexC2S` / `CodexS2C` union）

前端有对应的三元 `ChatProvider` 注册表（`generic` / `claude` / `codex`），按 thread 的 `backend_kind` 路由到不同主视图（`ClaudeCodeView` / `CodexView` / 通用 MessageList），归一化消息复用同一套 `ChatEvent` 渲染。

> **现状**：底层接入（后端 WS 通道 + 前端 provider + 视图）已完整，可作为「在统一框架内驱动外部 agent」的参考实现。但常驻 tab 切换 UI（`LeftSidebarTabs.tsx`）写好了尚未挂到主界面，目前通过新建会话时选 `backend_kind` 切换；这一块整体维护投入有限，定位为**实验性能力**，后续会随通道协议收口一起完善。

---

## 架构一览

```
src/core/                  ← agent 运行语义 + 跨模块协议真源 + 唯一 runner（所有能力的底座）
src/application/           ← 应用编排：workflow / 子 agent 树 / 定时任务执行桥 / agent 角色
src/tools/                 ← ToolRegistry + builtin tools + memory / skill / schedule / mcp 工具
src/sessions/              ← memory / sqlite / file 三后端 session + 历史压缩 + 输入装配
src/prompting/             ← 提示组装二级域（assembly / instructions / compaction / skills / context_sources）
src/safety/                ← DangerGuard → approval mode → thread permissions 三层安全链
src/infrastructure/        ← llm_providers（OpenAI/Anthropic + reasoning adapter + 流式）+ mcp + config + tracing
src/runtime_assembly/      ← SessionEngine 全局 composition root，组装 provider/tools/session/safety/runner
src/scheduler/             ← cron 定时任务（domain + store + ticker + execution_bridge + parser）
src/memory/  src/evolution/← 长期记忆冻结快照 / 自我进化 after-run 证据链
src/sitian/                ← 司天工作区观察者
src/hosts/cli/             ← CLI 宿主（click + prompt_toolkit）
src/hosts/web/  + web/     ← FastAPI + React + WS 多 thread 聊天 + 全局审批 inbox
src/hosts/shared/          ← 宿主适配 + HostDispatcher 投递门户
```

**依赖方向（import-linter 强制）**：

- 所有模块都能 import `core`
- `core` 不能 import 任何 sibling 模块
- 跨模块共享协议（`Session` / `Tool` / `ApprovalProvider` / `LLMProvider` / `EventSink` 等）**单一真源** `core.contracts`
- 模块间只调门户（`*Manager` / `Protocol` / `core.contracts`），内部 helper 不跨模块

### 一次 CLI 对话的数据流

```
cli/main.py           解析 --config / env → load_config(cfg)
  ├─ build_session(cfg, sid, bootstrap)         ← session 三后端
  ├─ JsonlTraceSink(cfg.trace.output_path)      ← trace 落盘
  ├─ _assemble_instructions(cfg, files)         ← 多来源 system prompt
  ├─ registry.register(build_memory_tool(...))  ← memory / skill 工具
  └─ SessionEngine.build(...)                   ← 装配 provider / tools / session / safety / runner
        ├─ provider 分派: openai_compatible → OpenAIResponsesProvider
        │                 anthropic        → AnthropicMessagesProvider
        ├─ SafetyGatedApproval(DangerGuard → approval mode → thread permissions → Consent)
        └─ InputAssembler(compactor) 接管 system 注入 + compact

CLIInteractiveLoop(host_dispatcher, command_service).run_loop()
  ↓ user_input → host_dispatcher.submit(QUEUE) → mailbox → agent_loop
  runtime.run(mail_text, session_id=sid)
    ↓
    core.Runner.run(...)
      ├─ run_index = session.advance_run_index()  →  run_id = f"{sid}-{run_index}"
      ├─ _seed_messages           user 入 session
      ├─ while turn < max_turns:
      │    assembled = input_assembler.assemble(history, instruction_sources)
      │    resp = llm.complete(llm_request)
      │    if resp.tool_calls:
      │        for call in tool_calls:
      │            decision = approval.decide(call)   ← Danger→mode→thread permissions→Consent
      │            if approved: result = tool.execute()
      │            session.append(tool_result)
      │    else: break                               ← 终态
      └─ emit events → list[EventSink]               ← 所有 Event 含 run_id
```

### 编排链路

```text
主 agent tool call (run_agent_workflow)
  └─ AgentWorkflowManager              ← workflow 门面：目录 / 描述 / 执行分发
       ├─ AgentWorkflowStrategyManager ← 策略注册表（按 mode 分发）
       │    └─ Parallel / MapReduce / DeepResearch / RoundtableReview / TaskFlow
       └─ 策略 run() 借用 WorkflowRuntime 能力：
            ├─ run_subagent_task(...)
            │    └─ AgentManager.spawn(SpawnAgentRequest)
            │         ├─ 父子 AgentCell 挂到 AgentTree，每个 child 一个 mailbox
            │         ├─ clip_child_tool_snapshot()：父工具 ∩ 请求 ∩ scope
            │         ├─ ModelCatalogResolver 为 child 解析独立 immutable snapshot
            │         └─ SessionEngine.run() → Runner.run()（复用唯一 run loop）
            ├─ write_workflow_manifest / write_result   ← 审计与终态收口
            └─ SessionTaskProgressManager               ← 可选 Progress 弹窗
  → AgentWorkflowResult（runs / reports / reports/index.json / result.json）
```

更深入的依赖方向和模块职责见 [AGENTS.md · 代码地图](AGENTS.md) 与 [docs/modules/](docs/modules/) 下各模块文档。

---

## 功能概览

| 能力 | 说明 |
|---|---|
| 单 agent run loop | 唯一 `core.Runner`，async-first，turn 推进 + tool 回填 + 停止条件收口 |
| 多 agent workflow 编排 | `AgentWorkflowManager` 暴露 5 种策略，LLM 通过 `run_agent_workflow` 工具选择并执行 |
| 子 agent 树 | `AgentManager` + `TaskRegistry` 统一管理 spawn 与 workflow child，工具能力按父级单调收紧，每 child 独立 session 与模型快照 |
| Agent 角色 preset | 内置与用户自建角色，供 workflow 与普通 spawn 复用 |
| 多 provider 接入 | `infrastructure/llm_providers/`，OpenAI-compatible（LM Studio / Ollama / vLLM / OpenAI 官方）+ Anthropic，reasoning adapter 统一映射 |
| 内置工具 | `read_file` / `write_file` / `list_dir` / `run_shell` / `memory` / `skill` / `schedule` / `web_fetch`，全 async；`web_search` 走 provider 官方 MCP 工具（minimax/glm），非内核内置 |
| MCP 工具接入 | stdio MCP client 把外部 MCP server 注册为 Kongming Tool |
| 三层安全链 | DangerGuard → approval mode → thread permissions（装配在 `SafetyGatedApproval`），task 级 `approval_mode` |
| Session 工程化 | memory / sqlite / file 三后端，可配置切换、跨进程恢复、历史压缩 |
| Trace 落盘 | `JsonlTraceSink` 把 run/tool/approval 事件 append 到 JSONL，可派生 usage / audit；raw LLM dump 含 key 脱敏 |
| 定时任务 | cron 模块（domain + store + ticker + execution_bridge），task 级审批、并发准入与真实取消 |
| 长期记忆 & 自我进化 | `memory` 模块冻结快照 + 活态条目；`evolution` 模块 after-run evidence + child reviewer + 进化产物 |
| 司天观察者 | 主动扫描 4 种 source kind，物化 observations / work_item / suggestions |
| 统一配置入口 | YAML + 多组 `KONGMING_*` 环境变量覆盖，本地模型可无 api_key |
| 双宿主 | CLI（click + prompt_toolkit）与 Web（FastAPI + React + WS 多 thread 聊天 + 全局审批 inbox）；Web 还平级接入 Claude Code SDK / Codex SDK 两条实验性通道 |
| 架构边界强制 | import-linter contracts + pytest 架构合约测试 |

### 看 LLM provider 原始 request / response 全貌

开 env 开关后，每次 provider 调用会在 `.kongming/debug/raw-llm-<timestamp>.json` 落一份完整 JSON——含 request payload、request headers（Authorization 脱敏）、response status、response headers、**完整 response body**（gzip 自动解压、结构化）。

```bash
KONGMING_TRACE_RAW_LLM=1 uv run python -m hosts.cli.main
ls -t .kongming/debug/raw-llm-*.json | head -1 | xargs jq '.response.body'
```

默认关；不开不落盘（对生产 / 隐私友好）。**Authorization header 始终脱敏**，即便 dump 文件被意外 share 也不会泄 API key。

### 持久化会话

session 三后端 `memory` / `sqlite` / `file`，默认 `file`（跨进程）：

```bash
KONGMING_SESSION_BACKEND=sqlite ./start.sh cli --session-id my-project
```

下次用同一个 `--session-id` 启动即恢复历史；运行数据派生到 `<kongming_home>`（默认 `~/.kongming`）下。

---

## 开发命令

```bash
make install        # uv sync --all-extras
make install-hooks  # 启用 commit/push 两层 hook（首次 clone 后跑一次）
make cli            # 启动 CLI（本地模型基线）
make smoke          # 冒烟验证
make fmt            # ruff format
make lint           # ruff check + lint-imports（架构边界）
make typecheck      # mypy
make precommit      # 手动跑一次 pre-commit 全仓扫描
make prepush-test   # 手动跑 push 前隔离 unit 快速门禁
make test-unit      # 全量 unit 测试
make test-e2e       # e2e 测试（真实模型用例默认 skip）
make nightly-local  # 本地真实 e2e nightly，默认端口 60999
make test           # unit + e2e 都跑
make clean          # 清缓存
```

或用统一入口脚本：`./start.sh help` / `lint` / `typecheck` / `test-unit` / `test-e2e` / `smoke`。

### 检查分层

| 时机 | 触发方式 | 检查内容 | 速度 | 真实 key |
|---|---|---|---|---|
| commit 前 | `git commit` | `ruff check --fix`、`ruff format`、`lint-imports`、`mypy src` | 快 | 无 |
| push 前 | `git push` / `make prepush-test` | 隔离环境下的受影响 `tests/unit`，清理真实 `KONGMING_*`，使用 `.kongming/prepush-home` | 快，目标 1-3 分钟 | 无 |
| PR CI | GitHub Actions | `fmt`、`lint`、`typecheck`、隔离环境下的受影响 `tests/unit` | 快，目标 1-3 分钟 | 无 |
| 本地 nightly | `make nightly-local` | `tests/integration`、`tests/e2e`、`tests/smoke`，读取 `.env.e2e.local` | 慢，适合夜间 | 需要 |
| 手动真模型验证 | 单条 `KONGMING_E2E_REAL_MODEL=1 uv run pytest ...` | 指定 live/e2e 场景 | 慢，按用例计费 | 需要 |

`make install-hooks` 安装 commit 和 push 两层 hook。push gate 只跑稳定 unit 测试，作为提交前最后一道快速拦截；真实模型、真实 web server、packaging smoke 放本地 nightly 或手动验证。

```bash
# 本地 nightly
cp .env.example .env.e2e.local
# 编辑 .env.e2e.local，填入真实模型 provider / base_url / api_key
make nightly-local
```

`.env.e2e.local` 由 `.gitignore` 排除。

### Opt-in 真模型 e2e

默认跳过，需要真模型服务起来才跑：

```bash
KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_local_model_config.py::test_local_model_real_request_roundtrip -v
```

### 验证当前仓库健康状态

```bash
make install && make lint && make typecheck && make test-unit
```

---

## 设计原则

1. **单 agent 内核为底座**：`core/` 定义 agent loop、turn 推进、协议真源，多 agent 编排在内核之上由 `application/` 的 workflow 与子 agent 机制实现，不另起 run loop
2. **模块化与门户**：每个目录级模块有清晰职责，对外门户统一命名为 `<Domain>Manager`；模块间只调门户、`Protocol` 或 `core.contracts`，内部 helper 不跨模块
3. **协议单一真源**：跨模块 Protocol 只在 `core.contracts` 定义，其他模块 `from core.contracts import ...`
4. **async-first**：所有核心接口 async，CLI 外壳同步但内部 `asyncio.run`
5. **装配与执行分离**：`SessionEngine.build()` 装配，`run()` 执行
6. **安全链是高层接口**：Tool Runtime 只消费装配后的 `ApprovalProvider`，不直连底层 policy
7. **Event fan-out**：runner 持有 `list[EventSink]`，追加 UsageSink / AuditSink 是并列注册，不新增协议
8. **本地优先**：默认配置指向本地 OpenAI-compat 服务，零云账号成本起步
9. **显式类型优先**：函数签名显式写出真实类型，收敛 `Any`；有限取值用 `StrEnum`，不用裸字符串

更完整的工程约束（约束清单、必需测试、工程陷阱）见 [AGENTS.md](AGENTS.md)。

---

## 范围与现状

仓库当前实现覆盖：

- **单 agent 内核**：唯一 `core.Runner` run loop、跨模块协议真源、async-first 接口
- **多 agent 编排**：5 种 workflow 策略、子 agent 树、`spawn_subagent`、agent 角色 preset
- **工具与扩展**：file / shell / memory / skill / schedule / web_fetch 内置工具，stdio MCP 接入；web_search 通过 provider 官方 MCP（minimax/glm）接入
- **模型接入**：OpenAI-compatible + Anthropic provider，reasoning effort 统一映射
- **会话与提示**：memory / sqlite / file 三后端，历史压缩、输入装配、skill 装载
- **安全**：DangerGuard → approval mode → thread permissions 三层链，task 级 `approval_mode`
- **观测**：JSONL trace、raw LLM dump（key 脱敏）、用量与审计事件
- **定时任务**：cron 模块、execution_bridge、并发准入与真实取消
- **长期能力**：长期记忆、自我进化证据链、司天工作区观察者
- **宿主**：CLI（click + prompt_toolkit）、Web（FastAPI + React + WS 多 thread 聊天 + 全局审批 inbox）
- **工程底座**：ruff / mypy（含 `@override` 校验）/ pytest / import-linter / commit+push 双层 hook / CI

---

## Roadmap

以下是正在演进或计划中的方向，按主题归组。演进节点见 `docs/spec/` 下各设计文档。

**主动性（优化中）**
- **司天自动触发**：当前只能手动调用（`run-once` / `loop`），自动调度（cron / 事件驱动）优化中
- **自我进化 cadence**：进化复盘的自动 cadence 触发优化中

**模型 eval（探索中）**
- **runtime eval**：[`evals/harness-runtime-v0.1/`](evals/harness-runtime-v0.1/) 在真实 SessionEngine + Runner 闭环内跑 12 题 / 7 类别（instruction / coding / repo_fix / tool_execution / long_context / tau_tool_state），fixture 模式（伪 LLM，CI 可重复）+ preset 模式（真实模型）。每次 run 采集 token 用量与成本账（[`evals/src/metrics.py`](evals/src/metrics.py) 三层聚合，配 pricing 块可换算金额），用于衡量不同 preset / 策略下模型运行成本与能力。
- **regression eval**：[`evals/regression-v0.1/`](evals/regression-v0.1/) 取材自项目历史 bug / fix report，测「在本代码库上的真实工程能力」，5 种题型设计（含复用 `lint-imports` 的架构边界题），MVP 3 道题已选定，题目待入库。

**能力扩展**
- **computer use**（开发中）：让 agent 操作桌面应用，不局限于命令行工具
- **跨平台桌面/移动宿主**：kongming 通过 sidecar 模式接入桌面端——本仓库提供 [`packaging/`](packaging/)（`kongming-web-backend` sidecar 构建 + web dist + runtime config）和 [`config/xspace/`](config/xspace/)（桌面运行配置样例）供下游桌面客户端消费；移动端通过 [`src/hosts/web/xspace_mobile/`](src/hosts/web/xspace_mobile/) 提供设备配对与认证后端（Android 已接入，iOS 待接入）。桌面客户端本身（XSpace，基于 Tauri）为独立项目，不在此仓库
- 更多 workflow 策略、agent 角色 preset 丰富化
- 多 provider 智能路由（按成本 / 延迟 / 能力自动选择）

**工程治理**
- guardrails / usage_meter / audit_log 的独立模块化
- usage_meter 落地后与上述成本 eval 共享用量数据

---

## 许可证

MIT
