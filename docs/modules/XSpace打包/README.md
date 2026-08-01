# packaging/ + XSpace resources — XSpace 打包交付

Kongming Web 以 Windows sidecar 交付给 x-space：`kongming-web-backend.exe` 负责后端服务，`web/dist` 负责前端静态资源，x-space resources 内的专用 `config/setting.yaml` 负责桌面运行默认值。

## 设计理念

| 决策 | 理由 |
|------|------|
| exe、dist、runtime config 三件套交付 | x-space 只需要启动 sidecar、读 ready JSON、轮询 `/health`、把 `base_url` 交给 WebView |
| build yaml 独立放在 `packaging/` | 构建期参数和运行期业务配置分离，`kongming-web-backend.build.yaml` 只被构建脚本读取 |
| x-space 使用专用 runtime `setting.yaml` | 桌面包默认值由 x-space resources 控制，避免继承开发机模型、路径和交互配置 |
| 发布打包采用 copy 实体文件 | Tauri / NSIS / CI 能稳定收集 exe、dist 和配置；本地联调用 symlink 或 junction 提升迭代速度 |
| 资源配置保持无密钥 | API key / token / auth 状态由用户设置、环境变量或 app data 注入，安装包内只保存默认结构 |

## 核心流程

```text
kongming-agent
  npm --prefix web run build
  uv run python packaging\build_kongming_web_backend.py
  ↓
  dist/kongming-web-backend/kongming-web-backend.exe
  web/dist/
  ↓ copy
x-space/client/src-tauri/resources/kongming/
  kongming-web-backend.exe
  web/dist/
  config/setting.yaml
  ↓ spawn
kongming-web-backend.exe --host 127.0.0.1 --port 0 --home <app_data>/kongming --config <app_data>/kongming/setting.yaml --dist-dir <resource>/kongming/web/dist --print-ready-json
POST /api/xspace/runtime/init
  ↓
<home>/web/server.json + stdout ready JSON
  ↓
GET /health
  ↓
open_tab(ready.base_url)
```

## 交付物

| 来源 | x-space 目标路径 | 用途 |
|------|------------------|------|
| `dist/kongming-web-backend/kongming-web-backend.exe` | `client/src-tauri/resources/kongming/kongming-web-backend.exe` | Tauri sidecar / external binary |
| `web/dist/` | `client/src-tauri/resources/kongming/web/dist/` | Vite 前端静态资源 |
| `config/xspace/setting.yaml` | `client/src-tauri/resources/kongming/config/setting.yaml` | 桌面运行默认配置 |
| `config/xspace/agent.toml` | `client/src-tauri/resources/kongming/config/agent.toml` | 桌面内置 subagent role 模板 |

`packaging/kongming-web-backend.build.yaml` 留在 kongming-agent 仓库内，作用是生成 exe。x-space 产品默认配置真源是 `config/xspace/setting.yaml` 和 `config/xspace/agent.toml`，构建 exe 和同步 resources 都使用这两份文件。
`config/xspace/sync-policy.yaml` 是源码维护门禁文件，用于解释 XSpace profile 与主配置的差异；它不进入 x-space resources。

## 构建命令

```powershell
npm --prefix web run build
uv run python packaging\build_kongming_web_backend.py
```

构建完成后检查：

```text
dist/kongming-web-backend/kongming-web-backend.exe
web/dist/index.html
```

## x-space resources 布局

```text
client/src-tauri/resources/kongming/
  kongming-web-backend.exe
  web/
    dist/
      index.html
      assets/
      ...
  config/
    setting.yaml
    agent.toml
```

推荐同步策略：

| 场景 | 策略 | 说明 |
|------|------|------|
| 本地联调 | `web/dist` 可用 junction / symlink，exe 和 config 用 copy | 前端 rebuild 后 x-space 立即看到新 dist |
| 发布打包 | exe、dist、config 全部 copy 实体文件 | 安装包、签名、CI 收集路径稳定 |

推荐复制来源：

```text
dist/kongming-web-backend/kongming-web-backend.exe -> resources/kongming/kongming-web-backend.exe
web/dist/                                          -> resources/kongming/web/dist/
config/xspace/setting.yaml                        -> resources/kongming/config/setting.yaml
config/xspace/agent.toml                          -> resources/kongming/config/agent.toml
```

## runtime setting.yaml

x-space 打包使用专用 runtime 配置，源文件在：

```text
config/xspace/setting.yaml
```

复制到 x-space resources 后路径为：

```text
client/src-tauri/resources/kongming/config/setting.yaml
```

维护规则：

| 规则 | 说明 |
|------|------|
| 主配置优先 | 新增 Config 字段先写入仓库根 `config/setting.yaml`，再选择是否同步到 XSpace profile |
| profile + policy 覆盖全部 Config leaf 字段 | `tests/unit/config/test_xspace_config_contract.py` 递归展开 `Config.model_fields`，要求主配置显式声明 leaf；XSpace 字段由 `config/xspace/setting.yaml` 或 `config/xspace/sync-policy.yaml` 决策覆盖 |
| 差异必须有决策 | XSpace 产品默认值与主配置不同使用 `xspace-keep`；只属于主配置的字段使用 `main-only`；继承主配置值可用 `sync-copy` |
| 产品默认值显式写入 | session、trace、tool、scheduler、web、workflow、sitian 等模块默认行为在 YAML 中可见 |
| 敏感值使用 env 或 app data 注入 | provider credential、`web.initial_password`、reviewer key 等不进入安装包 |
| `host.kind` 保持 `cli` | 当前 `HostConfig` schema 只允许 `cli`；Web sidecar 由 `hosts.web.run` 入口和 `web.enabled` 决定 |
| 运行数据路径归入 `kongming_home` | `.kongming/*` 字段运行期统一派生到 `--home` / `KONGMING_HOME` 指定的 `kongming_home` |
| resources 配置只作为模板 | XSpace 启动前把 `<resource>/kongming/config/setting.yaml` 复制或合并到 `<kongming_home>/setting.yaml`；sidecar 启动时读取 home 下的可写配置 |

## runtime agent.toml

`config/xspace/agent.toml` 是 XSpace 打包资源模板，构建后位于：

```text
client/src-tauri/resources/kongming/config/agent.toml
```

XSpace 宿主启动前把该模板复制或合并到 `<kongming_home>/agent.toml`。Kongming 运行期只读取 `<kongming_home>/agent.toml`；文件缺失时表示当前 home 没有内置 role 配置。运行期不把包内 resources 文件作为 fallback。

维护命令：

```bash
uv run python scripts/config-xspace-sync.py review
uv run python scripts/config-xspace-sync.py sync --path scheduler.default_max_turns
uv run python scripts/config-xspace-sync.py decision --path web.port --action xspace-keep --reason "XSpace 桌面默认端口使用产品值"
uv run python scripts/config-xspace-sync.py decision --path mcp.servers --action main-only --reason "XSpace 安装包默认不携带第三方 MCP server 列表"
```

policy 缺省字段表示 XSpace YAML 已显式写出同值。XSpace 缺失字段使用 `main-only`
记录原因，XSpace 保留差异值使用 `xspace-keep` 记录原因。sidecar 启动时读取 home
下的 XSpace 配置模板副本和 Config schema 默认值，主配置只参与维护期 review 与
`sync-copy`。

### safety 默认规则组合

XSpace 包内 `config/xspace/setting.yaml` 只写产品可分发配置和用户策略增量。`safety.*` 空数组表示没有额外追加规则，运行时仍会装配代码内置安全基线。

| 来源 | 生产入口 | 作用 |
|---|---|---|
| `config/xspace/setting.yaml` | XSpace 宿主启动前用于创建或补齐 `<kongming_home>/setting.yaml` | XSpace 产品默认值；`safety.*` 只表达用户策略增量 |
| `src/safety/approval/default_rules.py` | `safety.approval.chain.build_safety_chain()` 装配 `HardBlockGuard` / `DestructiveForceAskGuard` / `ConsentResolver` | hard deny、敏感路径、强制审批、trusted workdir、silent tool 的内置基线 |
| `src/safety/auto_approval/default_rules.yaml` | `safety.auto_approval.AutoApprovalManager.build()` 首启物化到 `<kongming_home>/web/auto_approval/rules.yaml` | Web/CLI 智能审批的 24 条默认阻断规则和倒计时策略 |

打包链路会把 `src/safety/auto_approval/default_rules.yaml` 作为数据文件放进 sidecar 包，同时把 `config/xspace/setting.yaml` 复制成包内 `config/setting.yaml`。`config/xspace/setting.yaml` 中的 `hard_deny_commands: []`、`sensitive_paths: []`、`allow_tools_silent: []` 不清空默认规则；默认规则在装配时按“内置规则 + YAML 追加项”组合。

配置节选：

```yaml
# XSpace 打包用 Kongming Web runtime 默认配置。
# 本文件覆盖 Config 全部 leaf 字段；新增字段由契约测试提醒维护。

model:
  preset_id: local-gemma-4-e4b-it
  reasoning_effort:

host:
  kind: cli

approval:
  mode: interactive

safety:
  # 空数组表示没有 XSpace 额外追加项；内置安全基线见
  # src/safety/approval/default_rules.py 和 src/safety/auto_approval/default_rules.yaml。
  hard_deny_commands: []
  approval_required_commands: []
  sensitive_paths: []
  skill_call_rules: []
  trusted_workdirs: []
  allow_writes: []
  allow_tools_silent: []
  log_silent_reads: false

session:
  backend: file
  store_path: .kongming/sessions.db
  file_store_path: .kongming/sessions

trace:
  output_path: .kongming/trace.jsonl

evolution:
  memory:
    root_path: .kongming/memory
  learning:
    root_path: .kongming/evolution

scheduler:
  enabled: true
  home: null

workflow:
  enabled: true
  home: null

sitian:
  output_subdir: null

web:
  enabled: true
  host: 127.0.0.1
  port: 60000
  dev_mode: false
  full_log:
    enabled: false
    path: .kongming/logs/full_log.jsonl
```

完整内容以 `config/xspace/setting.yaml` 为准。`model.preset_id` 指向随 sidecar 打包的 `config/model-providers.yaml`；用户自定义 provider 写入 `<kongming_home>/model-providers.yaml`。远端 credential 由用户态 app data 写入 catalog 声明的 provider-specific env。

运行数据字段解析规则：

| 配置字段 | x-space 默认值 | 运行期落点 |
|---|---|---|
| `session.store_path` | `.kongming/sessions.db` | `<kongming_home>/sessions.db` |
| `session.file_store_path` | `.kongming/sessions` | `<kongming_home>/sessions` |
| `trace.output_path` | `.kongming/trace.jsonl` | `<kongming_home>/trace.jsonl` |
| `evolution.memory.root_path` | `.kongming/memory` | `<kongming_home>/memory` |
| `evolution.learning.root_path` | `.kongming/evolution` | `<kongming_home>/evolution` |
| `scheduler.home` | `null` | `<kongming_home>/cron` |
| `workflow.home` | `null` | `<kongming_home>/workflows` |
| `web.full_log.path` | `.kongming/logs/full_log.jsonl` | `<kongming_home>/logs/full_log.jsonl` |
| `sitian.output_subdir` | `null` | `<kongming_home>/sitian` |

`kongming_home` 在 x-space 启动时由 `--home <app_data>/kongming` 显式指定；`KONGMING_HOME` 是等价环境变量入口；默认 `Path.home()/.kongming` 只用于没有传 `--home` 的开发场景。

## 启动参数

x-space 启动 sidecar 时固定传入显式路径：

```powershell
kongming-web-backend.exe `
  --host 127.0.0.1 `
  --port 0 `
  --home <app_data>\kongming `
  --config <app_data>\kongming\setting.yaml `
  --dist-dir <resource>\kongming\web\dist `
  --print-ready-json
```

拿到 ready JSON 并确认 `/health` 为 200 后，XSpace native 调用
`POST /api/xspace/runtime/init`，再加载 WebView。详细对接见
[XSpace Runtime Init](./xspace-runtime-init.md)。

参数职责：

| 参数 | x-space 取值 | 说明 |
|------|--------------|------|
| `--host` | `127.0.0.1` | 只绑定本机 loopback |
| `--port` | `0` 或资源配置端口 | `0` 由 OS 分配空闲端口；固定端口可用 `60000` |
| `--home` | app data 下的 `kongming` | 所有运行时写入根目录 |
| `--config` | app data 下的 `setting.yaml` | 可写用户配置；启动加载会执行 schema 迁移和缺失字段补齐 |
| `--dist-dir` | resources 下的 `web/dist` | 前端静态资源 |
| `--print-ready-json` | 固定传入 | stdout 输出一次 ready JSON |

运行期优先级：

```text
host: CLI --host > KONGMING_WEB_HOST > config.web.host > 127.0.0.1
port: CLI --port > KONGMING_WEB_PORT > config.web.port > Config 默认值
home: CLI --home > KONGMING_HOME > Path.home()/.kongming
config: CLI --config > KONGMING_CONFIG > <home>/setting.yaml > <home>/config/setting.yaml > config/setting.yaml
dist-dir: CLI --dist-dir > KONGMING_WEB_DIST > 包内默认 dist > web/dist
host-environment: runtime init > CLI --host-environment > KONGMING_WEB_HOST_ENVIRONMENT > browser
```

## 客户端环境能力

XSpace runtime init 固定声明运行宿主：

```http
POST /api/xspace/runtime/init
```

Web 前端通过现有客户端配置端点读取宿主环境和能力：

```http
GET /api/config/client
```

响应中的新增字段：

```json
{
  "host_environment": "xspace",
  "capabilities": {
    "xspace_host": true,
    "native_file_dialog": true
  }
}
```

`/api/config/client` 只用于浏览器侧读取非敏感运行参数。XSpace 需要在运行中控制后端、触发文件桥接、handoff 或设备配对时，另按 `/api/xspace/...` 设计专用宿主 API。

## Ready JSON

启动成功后，sidecar 同时写入 stdout 单行 JSON 和 `<home>/web/server.json`：

```json
{
  "type": "kongming_web_ready",
  "host": "127.0.0.1",
  "port": 60000,
  "base_url": "http://127.0.0.1:60000",
  "health_url": "http://127.0.0.1:60000/health",
  "pid": 12345,
  "home": "C:/Users/me/AppData/Roaming/XSpace/kongming",
  "server_json": "C:/Users/me/AppData/Roaming/XSpace/kongming/web/server.json",
  "dist_dir": "C:/Program Files/XSpace/resources/kongming/web/dist",
  "started_at": "2026-06-09T12:00:00Z"
}
```

x-space 读取规则：

| 字段 | 用途 |
|------|------|
| `type` | 必须等于 `kongming_web_ready` |
| `base_url` | 交给现有 `open_tab` / WebView |
| `health_url` | 启动健康检查 |
| `pid` | 进程管理和退出清理 |
| `server_json` | 重启、状态恢复和诊断 |
| `dist_dir` | 验证 resources 路径 |

## 健康检查

x-space 启动后轮询：

```http
GET /health
```

成功响应：

```json
{"status":"ok"}
```

兼容端点：

```http
GET /api/health
```

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `packaging/kongming-web-backend.build.yaml` | artifact / entrypoint / pyinstaller / platforms | 构建期配置，带中文注释 |
| `config/xspace/setting.yaml` | x-space runtime defaults | 桌面产品默认配置真源 |
| `packaging/build_kongming_web_backend.py` | `main` / `load_build_config` / `validate_build_inputs` / `build_command` | 调用 PyInstaller 产出 exe |
| `packaging/kongming-web-backend.spec` | PyInstaller Analysis / EXE 配置 | 收集 hidden imports、data files、dist |
| `packaging/kongming_web_backend_entry.py` | exe 入口 | 调用 `hosts.web.run:main` |
| `src/hosts/web/run.py` | `WebRuntimeOptions` / `main` | 参数解析、端口绑定、ready JSON、`server.json` |
| `src/hosts/web/static.py` | `install_static` | `--dist-dir` / `KONGMING_WEB_DIST` / PyInstaller dist 解析 |
| `src/hosts/web/routers/health.py` | health routes | `/health` 和 `/api/health` |
| `tests/smoke/test_kongming_web_backend_exe.py` | exe smoke | 真实启动 exe、读 ready JSON、访问 `/health` |
| `docs/xspace-tauri-kongming-migration.md` | 宿主契约 | 稳定参数、ready JSON、health、运行时目录契约 |

## 验收命令

```powershell
npm --prefix web run build
uv run python packaging\build_kongming_web_backend.py
uv run pytest tests/unit/config/test_xspace_config_contract.py -v
uv run pytest tests/smoke/test_kongming_web_backend_exe.py -v
```

完整任务验证记录见：

```text
dev-pipeline/tasks/xspace-kongming-web-backend-sidecar-v0.1/dev-report.md
dev-pipeline/tasks/xspace-kongming-web-backend-sidecar-v0.1/reports/qa-gate/
```

## 已知问题 / 待完成

| 项 | 状态 |
|----|------|
| x-space backend manager | x-space 侧另开 task：分配端口、启动 sidecar、读 ready JSON、轮询 health、打开 tab |
| macOS / Linux sidecar | build yaml 已预留字段，当前 Windows 为验收平台 |
| startup 失败诊断 | 可继续增强 `<home>/web/startup.json` 的 phase 信息 |
| `_MEIPASS` fallback 单测 | 可补更细的 PyInstaller 解包路径覆盖 |
