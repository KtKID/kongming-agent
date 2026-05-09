# config/

`kongming-agent` 的统一配置目录。

## 配置文件

- `setting.yaml`：主配置文件，覆盖当前仓库默认运行参数
- `sitian.local.yaml`：司天主动扫描本地配置，配合 `SiTianRun.ps1` 和 `kongming-sitian` 使用

## 加载顺序

1. CLI 参数 `--config <path>`
2. 环境变量 `KONGMING_CONFIG`
3. 仓库内默认配置文件

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

司天记录默认写到 `~/.kongming/SiTian/`，核心产物全部是 JSON / JSONL / Markdown：

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
