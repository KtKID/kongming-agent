# src/commands/ — 命令系统

宿主无关的 `/slash` 命令解析、注册与执行服务层，供 CLI 和 Web 两端共享同一套命令定义与分发逻辑。

## 设计理念

| 决策 | 理由 |
|------|------|
| 命令定义与执行分离 | `CommandDefinition` 是纯数据描述（id / slash / kind / visibility），执行逻辑由 `CommandService` 根据 `kind` 分发，不写在定义里。便于注册扩展而不改执行路径。 |
| 宿主可见性过滤 | `host_visibility` 字段（`cli` / `web` / `both`）让同一份注册表按宿主类型裁剪，CLI 和 Web 不需要各维护一份命令列表。 |
| `prompt` 类型命令复用 runtime 对话链路 | `kind="prompt"` 的命令直接把 `description`（或用户追加的 `args_text`）交给 `runtime_delegate` 走正常 LLM turn，零额外基础设施成本。 |
| Web action 命令在宿主控制面执行 | `/evolve` 只对 generic_chat thread 展示，由 WebSocket 在普通输入投递前消费并直接调用 `EvolutionManager`，当前 thread 主 LLM 不接收命令文本。 |
| 输入解析独立为纯函数 | `parse_input()` 不依赖 registry、不做 IO，只做正则匹配和结构拆分，方便单元测试。 |
| 通过 `RuntimeDelegate` 接入 | `CommandService` 不自己持有 runtime 引用，而是接收 `RuntimeDelegate`（`Callable[[str, str \| None], Awaitable[Result]]`）；CLI 装配期注入 `HostDispatcher.run_text`，保持命令层与 runner 的解耦。 |

## 核心概念

```
用户输入 → parse_input()
             │
             ├─ kind="text"  → RuntimeDelegate → Runner.run()（正常对话）
             │
             └─ kind="command"
                  │
                  ├─ registry.lookup() → None → CommandResult(status="failed", "Unknown command")
                  │
                  └─ registry.lookup() → CommandDefinition
                       │
                       ├─ kind="prompt" → RuntimeDelegate → Runner.run()（以命令描述/参数为 prompt）
                       └─ kind="action"
                            ├─ Web /evolve → EvolutionManager → child reviewer
                            └─ 其他 → CommandService 返回 "Unsupported command kind"
```

- **CommandKind 三态**：`prompt`（转为 LLM prompt 执行）、`action`（宿主侧直接执行，当前实现 Web `/evolve`）、`interactive`（需多轮交互，待实现）。
- **ParsedInput**：解析产物，区分普通文本（`kind="text"`）与命令调用（`kind="command"`），携带 `slash` / `command_name` / `args_text`。
- **CommandResult**：命令执行结果，`status` 四态：`completed` / `failed` / `delegated_to_runtime` / `waiting_input`。
- **CommandExecutionContext**：每次调用的上下文快照，含 `session_id` / `cwd` / `host_kind` / `reasoning_effort`。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `__init__.py` | `CommandDefinition` / `CommandExecutionContext` / `CommandResult` / `CommandService` / `build_default_command_service` | 包门面，给下游一个稳定的 `from commands import X` 入口。 |
| `models.py` | `CommandKind` / `HostVisibility` / `ParsedInputKind` / `CommandResultStatus` / `CommandDefinition` / `ParsedInput` / `CommandInvocation` / `CommandExecutionContext` / `CommandResult` | 纯数据结构层。所有 dataclass 均 `frozen=True`。`CommandInvocation` 目前未被消费，预留给 `action` / `interactive` 类型命令使用。 |
| `parser.py` | `parse_input()` | 纯函数。将原始字符串解析为 `ParsedInput`；命令名必须匹配 `^[a-z][a-z0-9-]*$`，不含嵌套斜杠。 |
| `registry.py` | `CommandRegistry` / `build_builtin_registry()` | 注册表。构造时校验 slash 去重；`list_commands(host_kind)` 按可见性过滤；`lookup()` 大小写不敏感匹配。`build_builtin_registry()` 从 `builtins.py` 加载内建命令。 |
| `builtins.py` | `BUILTIN_COMMANDS` | 内建命令定义元组。包含 Web `/evolve` action 与 `/hello` 测试 prompt。 |
| `service.py` | `CommandService` / `build_default_command_service()` / `build_execution_context()` / `RuntimeDelegate` | 核心服务。`handle_command()` 只处理 slash command；prompt command 展开成文本后走 delegate。普通 text 输入由 `CLIInteractiveLoop` 分流到 `HostDispatcher`。`build_default_command_service()` 是装配 helper，由 CLI main 注入 `HostDispatcher.run_text`。`_infer_host_kind()` 通过 adapter 类名/模块名推断 `cli` 或 `web`。 |

## 已知问题 / 待完成

- **通用 action executor 尚未进入 `CommandService`**：Web `/evolve` 由宿主控制面执行；其余非 `prompt` 类型在共享 service 中返回 `"Unsupported command kind"`。
- **`CommandInvocation` 未被消费**：`models.py` 定义了 `CommandInvocation`（含 `invocation_id` / `cwd`），但当前 `service.py` 未生成该对象，预留给未来需要审计/回放的命令类型。
- **宿主推断基于类名字符串匹配**：`_infer_host_kind()` 通过 `"web" in class_name` 判断宿主类型，不够健壮；如果未来 adapter 命名不含 `web` 关键字会回退为 `cli`。
