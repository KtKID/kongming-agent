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

### 5. 看到中间发生了什么

```bash
uv run python -m cli.main --verbose
```

每轮都会打 `turn.start` / `tool.call.start` / `approval.decision` / `llm.response` 等事件进度，排障友好。

### 6. 换模型 / 换后端

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

## 开发命令

也可以用 `make` 入口：

```bash
make install      # uv sync --all-extras
make cli          # 启动 CLI（本地模型基线）
make smoke        # 冒烟验证
make fmt          # ruff format
make lint         # ruff check + lint-imports（架构边界）
make typecheck    # mypy
make test-unit    # 98 个单元测试
make test-e2e     # 40 个 e2e（含 1 个 opt-in 真模型用例，默认 skip）
make test         # unit + e2e 都跑
make clean        # 清缓存
```

### 验证当前仓库健康状态

```bash
make install && make lint && make typecheck && make test
```

当前（2026-04-20）：全绿 — 137 passed, 1 skipped；其中 98 unit + 40 e2e，3 import-linter contracts kept，mypy `Success: no issues found in 40 source files`。

### Opt-in 真模型 e2e

默认跳过，需要真模型服务起来才跑：

```bash
KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_local_model_config.py::test_local_model_real_request_roundtrip -v
```

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
