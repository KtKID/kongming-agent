# config/

`kongming-agent` 的统一配置目录。

## 配置文件

- `setting.yaml`：主配置文件，覆盖当前仓库默认运行参数
- `model-providers.yaml`：Web 内置模型服务商 catalog，声明 provider endpoint/header/key env 和 provider 下的多模型列表
- `agent.toml`：内置 subagent role 配置，字段为 `id`、`nickname`、`model`、`role_desc`、`reasoning_effort`、`max_turns`
- `sitian.local.yaml`：司天主动扫描本地配置，配合 `SiTianRun.ps1` 和 `kongming-sitian` 使用

## 主配置加载顺序

1. CLI 参数 `--config <path>`
2. 环境变量 `KONGMING_CONFIG`
3. 仓库内默认配置文件

Web sidecar 在调用 `load_config()` 前会优先绑定用户级配置：

1. 启动参数 `--config <path>`
2. 环境变量 `KONGMING_CONFIG`
3. `<KONGMING_HOME>/setting.yaml`
4. `<KONGMING_HOME>/config/setting.yaml`
5. 仓库内 `config/setting.yaml`

## 模型配置读取顺序

| 配置 | 路径 / env | 说明 |
|------|------------|------|
| 全局默认选择 | 当前 `setting.yaml` 的 `model.preset_id/reasoning_effort` | 对应两个同名 `KONGMING_MODEL_*` env 可覆盖 |
| 内置 provider 模板 | `config/model-providers.yaml` | `KONGMING_MODEL_PROVIDER_CATALOG` 可显式指定其它 catalog 文件 |
| provider API key | `KONGMING_HOME/.env` + 真实进程 env | Web 管理页连接 provider 时写入 `.env`；真实进程 env 优先 |
| 用户自定义 catalog | `<KONGMING_HOME>/model-providers.yaml` | 同 provider ID 完整替换内置定义，preset ID 全局唯一 |

`model-providers.yaml` 的 `providers[*].models[*]` 是静态模型真源。Web、CLI、scheduler 与 agent runtime 均通过 `ModelCatalogManager` 解析同一份 immutable snapshot。

## 进化审查配置

`setting.yaml` 的 `evolution.learning` 同时控制自动与显式审查：

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 进化模块、公开 `request_evolution_review` Tool 与 after-run lifecycle 总开关 |
| `auto_trigger_enabled` | `true` | cadence 自动触发开关；设为 `false` 后保留显式 Tool |
| `every_n_runs` | `5` | 自动路径每 N 个主 run 触发一次 |
| `min_user_turns` | `3` | 自动路径最低用户轮数；显式请求直接进入 review plan |

对应环境变量 `KONGMING_EVOLUTION_LEARNING_AUTO_TRIGGER_ENABLED=false` 可开启“仅手动”模式。显式 Tool 只请求复盘当前 run，reviewer 生成候选 nutrient 后，用户继续选择 materialize 为 memory、skill 或忽略。

## SiTian 本地用法

### 直接运行

在仓库根目录执行：

```powershell
.\SiTianRun.ps1
```

默认会读取 `config/sitian.local.yaml`，执行一次扫描，然后打印当前状态和摘要。

### 指定动作

```powershell
.\SiTianRun.ps1 -Action scan
.\SiTianRun.ps1 -Action state
.\SiTianRun.ps1 -Action summary
.\SiTianRun.ps1 -Action loop
```

### 直接调用 CLI

```powershell
uv run kongming-sitian run-once --config config/sitian.local.yaml
uv run kongming-sitian state
```

### 记录目录

司天记录默认写到 `<kongming_home>/sitian/`，`kongming_home` 默认是 `Path.home() / ".kongming"`，也可由 `KONGMING_HOME` 显式覆盖。`--root-dir` 或脚本层 `SITIAN_ROOT` 会覆盖司天产物根目录。核心产物全部是 JSON / JSONL / Markdown：

- `observations.jsonl`
- `runtime_state.json`
- `workspace_state.json`
- `latest_suggestions.json`
- `latest_summary.md`

### 默认观察范围

`sitian.local.yaml` 默认观察当前工作区 `E:/xgt/proj/agent-proj`，并声明 3 类 source：

- `generic_channel`
- `claude_project`
- `codex_project`
