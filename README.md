# kongming-agent

<p align="center">
  <img src="assets/logo.png" width="320" alt="kongming-agent logo">
</p>

> 通用 agent 框架——`core` 内核可接入任意产品，本仓库提供完整的 agent 实现参考。

## 项目定位

**kongming-agent** 的长期目标是构建一个通用 agent：能调用任意类型的工具、能操作计算机、能接入 macOS 桌面、游戏引擎或其他 App——不局限于 coding 场景。

核心运行语义封装在 **`src/core/`**：

- 只定义 agent loop、turn 推进、tool orchestration、run state 和跨模块协议（`contracts.py`）
- 不依赖任何具体宿主、模型厂商或工具
- CLI、macOS 桌面、游戏、HTTP 服务等宿主的差异落在各自的 `host/*_adapter`

本仓库基于 core 构建了第一个完整实现：接入 OpenAI-compatible 模型、内置 file / shell 工具、三层安全链、CLI 宿主、SQLite 会话持久化。既是接入 core 的实现参考，也可以直接使用。

一条闭环：**用户输入 → agent loop → 模型响应 → tool call → permission → approval → tool 执行 → 结果回填 → 最终输出**。

---

## 如何使用

### 0. 环境要求

- macOS / Linux（Windows 下 `run_shell` 工具未测）
- [uv](https://docs.astral.sh/uv/) 用来管理 Python 环境（会自动下载 Python 3.11）
  ```bash
  brew install uv
  # 或 curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 1. 安装依赖

```bash
cd /path/to/kongming-agent
uv sync --all-extras
```

首次会自动下载 Python 3.11、34 个包，几十秒完成。

也可以直接用统一入口脚本：

```bash
./start.sh install
```

PowerShell on Windows:
```powershell
.\start.cmd install
.\start.cmd web restart
.\start.ps1 install
.\start.ps1 web restart
```

### 2. 启动本地模型服务（任选其一）

CLI 默认连 `http://127.0.0.1:1234`，模型名 `gemma-4-e4b-it`。

**LM Studio**（最省事）
1. 下载 [LM Studio](https://lmstudio.ai)
2. 搜索下载一个模型（比如 `gemma-3-4b-it`）
3. 开 Developer → Start Server，默认就 `127.0.0.1:1234`

**Ollama**（端口是 11434，要改配置）
```bash
brew install ollama
ollama serve &
ollama pull llama3.2
# 启动时用 env 覆盖 base_url 和模型名，见下文
```

**远端 OpenAI**：见"换模型/换后端"一节。

### 3. 冒烟验证（装配 + 真请求最小探测）

```bash
uv run python -m cli.main --smoke
```

期望输出：
```
[smoke] ok status=completed reply='ok'
```

看到这行说明全链路（配置加载 → provider → runner → session → approval）跑通。如果本地模型没起，会看到连接失败或 HTTP 503——先解决模型服务。

### 4. 进入交互式对话

```bash
uv run python -m cli.main
```

```
kongming-agent · model=gemma-4-e4b-it · session=cli-a1b2c3d4
Ctrl+D 退出。
kongming > 你好
<模型回复>
kongming > 帮我读 pyproject.toml 前 30 行
命中审批：tool=read_file args={'path': 'pyproject.toml', 'max_bytes': 2000}
允许？[y/N] y
<模型调用 tool 拿到内容后的回答>
kongming >
```

按 `Ctrl+D` 退出；`Ctrl+C` 中断当前输入。

统一入口写法：

```bash
./start.sh cli
./start.sh cli --verbose
./start.sh cli-file-session --session-id demo
```

### 5. 看到中间发生了什么

```bash
uv run python -m cli.main --verbose
```

每轮都会打 `turn.start` / `tool.call.start` / `approval.decision` / `llm.response` 等事件进度，排障友好。

### 6. 密钥管理（.env）

**远端模型的 API key 绝不写进 `config/default.yaml`**——那样会 commit 进 Git。
项目用 `python-dotenv` 自动读取项目根的 `.env`：

```bash
cp .env.example .env
# 编辑 .env 填入真实 key：
#   KONGMING_MODEL_API_KEY=sk-your-real-key
```

`.env` 已被 `.gitignore` 排除，不会进仓库。`load_config()` 在读 YAML 前会先
`load_dotenv()` 把 `.env` 注入 `os.environ`，再走 `KONGMING_*` 覆盖链接入
Config——所以填进 `.env` 的变量和直接 `export` 同效。

生产/CI 部署不需要 `.env` 文件，直接从 secret store 注入 env 变量即可（真实
env 优先于 `.env`，`.env` 永远不覆盖已设值）。

push gate 始终强制设置 `KONGMING_SKIP_DOTENV=1`，即使本机环境里设过
`KONGMING_SKIP_DOTENV=0` 也会被覆盖；`load_config()` 会跳过项目 `.env`，
确保单元测试只使用隔离环境和测试内显式设置的变量。

### 7. 换模型 / 换后端

**不改 YAML，用环境变量覆盖**（16 个统一配置项，见 `config/README.md`）：

```bash
# 切到 Ollama
KONGMING_MODEL_BASE_URL=http://127.0.0.1:11434 \
KONGMING_MODEL_NAME=llama3.2 \
uv run python -m cli.main

# 切到远端 OpenAI
KONGMING_MODEL_BASE_URL=https://api.openai.com \
KONGMING_MODEL_NAME=gpt-4o-mini \
KONGMING_MODEL_API_KEY=sk-xxx \
uv run python -m cli.main
```

**或者指定一份自己的配置文件**：

```bash
uv run python -m cli.main --config /path/to/my.yaml
# 或通过 env
KONGMING_CONFIG=/path/to/my.yaml uv run python -m cli.main
```

### 7. 持久化会话（跨进程保留历史）

默认 session 是内存版，进程结束就丢。要跨进程：

```bash
KONGMING_SESSION_BACKEND=sqlite \
uv run python -m cli.main --session-id my-project
```

下次用同一个 `--session-id` 启动，会从 `.kongming/sessions.db` 恢复历史。

### 8. 审批模式（对 tool call 的放行策略）

默认 `interactive`，每次 tool call 问你一次 `[y/N]`。三种模式：

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `interactive` | 人工确认（默认） | 日常使用 |
| `auto_allow` | 自动放行 | 自动化脚本、无人值守（**注意 shell tool 风险**） |
| `auto_deny` | 自动拒绝 | 压测 deny 分支、模型纯聊天不碰工具 |

```bash
KONGMING_APPROVAL_MODE=auto_allow uv run python -m cli.main
```

### 9. 关闭某类工具

```bash
# 只保留文件工具，关掉 shell
KONGMING_TOOL_SHELL_ENABLED=false uv run python -m cli.main

# 反过来
KONGMING_TOOL_FILE_ENABLED=false uv run python -m cli.main
```

### 10. 常见问题速查

| 症状 | 排查 |
|---|---|
| `HTTP 503` / 连不上 | `curl http://127.0.0.1:1234/v1/models` 看服务在不在；LM Studio 有时 idle 会卸载模型，需要重新 load |
| 模型名不匹配（报 404） | 上面那个 curl 返回的 `id` 才是真实模型名，填到配置 |
| RuntimeWarning 类 | 如果看到 `runpy` 警告，更新到最新版本即可 |
| 输出乱码 | 终端 encoding 不是 utf-8，`export LANG=zh_CN.UTF-8` |

---

### 11. 运行 SiTian 主动扫描

仓库内已经提供现成配置和一键脚本：

```powershell
.\SiTianRun.ps1
```

这条命令会依次执行：
- `kongming-sitian run-once`
- `kongming-sitian state`
- 打印 `latest_summary.md`

常用写法：

```powershell
.\SiTianRun.ps1 -Action scan
.\SiTianRun.ps1 -Action state
.\SiTianRun.ps1 -Action summary
.\SiTianRun.ps1 -Action loop
```

默认配置文件是 [config/sitian.local.yaml](/E:/xgt/proj/agent-proj/kongming-agent/config/sitian.local.yaml)，默认记录目录是 `~/.kongming/SiTian/`。司天产物全部使用 JSON / JSONL / Markdown：
- `observations.jsonl`
- `runtime_state.json`
- `workspace_state.json`
- `latest_suggestions.json`
- `latest_summary.md`

## 开发命令

也可以用 `make` 入口：

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

也可以用统一入口脚本：

```bash
./start.sh help
./start.sh lint
./start.sh typecheck
./start.sh test-unit
./start.sh test-e2e
./start.sh smoke
```

### 检查分层

| 时机 | 触发方式 | 检查内容 | 速度 | 真实 key |
|---|---|---|---|---|
| commit 前 | `git commit` | `ruff check --fix`、`ruff format`、`lint-imports`、`mypy src` | 快 | 无 |
| push 前 | `git push` / `make prepush-test` | 隔离环境下的受影响 `tests/unit`，清理真实 `KONGMING_*`，使用 `.kongming/prepush-home` | 快，目标 1-3 分钟 | 无 |
| PR CI | GitHub Actions | `fmt`、`lint`、`typecheck`、隔离环境下的受影响 `tests/unit` | 快，目标 1-3 分钟 | 无 |
| 本地 nightly | `make nightly-local` | `tests/integration`、`tests/e2e`、`tests/smoke`，读取 `.env.e2e.local` | 慢，适合夜间 | 需要 |
| 手动真模型验证 | 单条 `KONGMING_E2E_REAL_MODEL=1 uv run pytest ...` | 指定 live/e2e 场景 | 慢，按用例计费 | 需要 |

`make install-hooks` 会安装 commit 和 push 两层 hook。push gate 只跑稳定 unit 测试，适合作为提交前最后一道快速拦截；真实模型、真实 web server、packaging smoke 放在本地 nightly 或手动验证里。

本地 nightly 默认固定使用 `KONGMING_WEB_PORT=60999` 和 `.kongming/nightly`：

```bash
cp .env.example .env.e2e.local
# 编辑 .env.e2e.local，填入真实模型 provider / base_url / api_key
make nightly-local
```

`.env.e2e.local` 由 `.gitignore` 排除。夜间定时任务可以用 macOS `launchd` 或其他本机 scheduler 调用 `make nightly-local`。

### 验证当前仓库健康状态

```bash
make install && make lint && make typecheck && make test-unit
```

完整本地验证可以按需追加：

```bash
make test-e2e
make nightly-local
```

### Opt-in 真模型 e2e

默认跳过，需要真模型服务起来才跑：

```bash
KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_local_model_config.py::test_local_model_real_request_roundtrip -v
```

### 看 LLM provider 原始 request / response 全貌

开 env 开关后，每次 provider 调用会在 `.kongming/debug/raw-llm-<timestamp>.json`
落下一份完整 JSON —— 含 request payload、request headers（Authorization 脱敏）、
response status、response headers、**完整 response body**（gzip 自动解压、结构化）。

```bash
# 跑 CLI 时开启
KONGMING_TRACE_RAW_LLM=1 uv run python -m cli.main

# 看最近一次调用的完整响应
ls -t .kongming/debug/raw-llm-*.json | head -1 | xargs jq '.response.body'

# 看 tool_calls 的原始格式（id / arguments / function）
ls -t .kongming/debug/raw-llm-*.json | head -1 \
  | xargs jq '.response.body.choices[0].message.tool_calls'
```

默认关；不开不落盘（对生产 / 隐私友好）。**Authorization header 始终脱敏**，
即便 dump 文件被意外 share 也不会泄 API key。

---

## 功能概览

| 能力 | 说明 |
|---|---|
| 单 agent run loop | 唯一 `core.runner.Runner`，async-first，turn 推进 + tool 回填 + 停止条件收口 |
| OpenAI-compatible provider | `executors/llm/openai_responses.py`，兼容 LM Studio / Ollama / vLLM / OpenAI 官方 |
| 内置工具 | `read_file` / `write_file` / `list_dir` / `run_shell`（全 async subprocess，带超时） |
| 三层安全链 | CapabilityPolicy → PermissionPolicy → ApprovalProvider（装配在 `SafetyGatedApproval`） |
| Session 工程化 | `InMemorySession` 或 `SQLiteSession`（跨进程恢复），可配置切换 |
| Trace 落盘 | `JsonlTraceSink` 把 run/tool/approval 事件 append 到 JSONL，后续可派生 usage/audit |
| 统一配置入口 | YAML + 16 个 `KONGMING_*` 环境变量覆盖，本地模型可无 api_key |
| CLI 宿主 | `HostAdapter` + `SessionBridge` + `CLIAdapter`，click + prompt_toolkit |
| 架构边界强制 | import-linter 3 contracts + pytest 架构合约测试 |

---

## 架构一览

```
src/cli/                        ← 第一个真实宿主入口（click + prompt_toolkit）
 └─▶ src/host/                  ← 宿主抽象与桥接层
      └─▶ src/safety/           ← capability → permission → approval 安全链
           └─▶ src/executors/   ← OpenAI-compat provider + NativeRuntime 装配层
                └─▶ src/core/   ← 宿主无关 agent 运行语义（runner + contracts 真源）

src/tools/ src/context/ src/observability/ src/config_loader/  ← core 之上的横切/实现层
```

**依赖方向（import-linter 强制）**：
- 所有模块都能 import `core`
- `core` 不能 import 任何 sibling 模块
- 跨模块共享协议（`Session` / `Tool` / `ApprovalProvider` / `LLMProvider` / `EventSink`）**单一真源** `core.contracts`

---

## 目录结构

```
src/
  core/                宿主无关运行语义 + 协议真源
  tools/               ToolRegistry + builtin tools + 三种 ApprovalProvider
  context/             SQLiteSession + HistoryCompactor + InputAssembler
  executors/
    llm/               BaseLLMProvider + OpenAIResponsesProvider
    agent_runtime/     NativeRuntime 装配层（build/run）
  host/                HostAdapter + SessionBridge + CLIAdapter
  cli/                 click 入口 + prompt_toolkit REPL
  safety/              CapabilityPolicy + PermissionPolicy + SafetyGatedApproval
  observability/       JsonlTraceSink
  config_loader/       pydantic Config + YAML + env 覆盖
config/
  default.yaml         默认配置
  local-model.yaml     本地模型基线
tests/
  unit/                单元测试
  e2e/                 e2e 测试（含 1 opt-in 真模型）
Makefile               统一命令入口
pyproject.toml         hatchling + ruff + mypy + pytest + coverage
```

---

## 范围与边界

当前版本是 **`v0.1`**。

**`v0.1` 当前范围包含**：
- 单 agent、单 run loop、单宿主验证路径
- 最小 tool runtime（file / shell）
- 最小 session（memory + sqlite 并存）
- 一个模型 provider 落地（OpenAI-compatible）
- 一个真实宿主（CLI）
- 最小安全链（capability → permission → approval）
- 最小观测（单个 JSONL sink）
- 工程底座（ruff / mypy / pytest / import-linter / CI）

**`v0.1` 当前范围外的能力**：
- `guardrails.py` / `usage_meter.py` / `audit_log.py`
- 长期 memory、自动记忆抽取
- MCP / plugins / coordinator / subagent
- 多 provider 并行、智能路由
- macOS API / computer use / 游戏 adapter / http adapter 的正式实现

---

## 设计原则

1. **宿主无关**：`core/` 不依赖任何具体宿主 SDK，换宿主只改 `host/` + `cli/`
2. **协议单一真源**：跨模块 Protocol 只在 `core.contracts` 定义，其他模块 `from core.contracts import ...`
3. **async-first**：所有核心接口 async，CLI 外壳同步但内部 `asyncio.run`
4. **装配与执行分离**：`NativeRuntime.build()` 装配，`run()` 执行；不写第二个 run loop
5. **安全链是高层接口**：Tool Runtime 只消费装配后的 `ApprovalProvider`，不直连 `CapabilityPolicy` / `PermissionPolicy`
6. **Event fan-out**：runner 持有 `list[EventSink]`，v0.2+ 追加 UsageSink/AuditSink 是并列注册，不新增协议
7. **本地优先**：默认配置指向本地 OpenAI-compat 服务，零云账号成本起步

---

## 近期优先级

1. 补测试用例，继续加密 `core` 主链路和关键边界
2. 建立最小 `skill` 机制，形成稳定扩展入口
3. 完善 `context` 链路，打通 instruction / assembly / compaction
4. 在 `core` 稳定后扩展更多宿主和运行时能力

### 当前约定

- `skill` 第一版范围：本地 skill registry、skill prompt 注入、开关控制
- `context` 第一版目标：让 `InstructionLoader`、`InputAssembler`、`HistoryCompactor` 真正进入主链，先稳定模型最终输入
- 测试补强优先面：`runner turn loop`、`tool roundtrip`、`approval/safety chain`、`session persistence`、`context assembly`

---

## 许可证

MIT
