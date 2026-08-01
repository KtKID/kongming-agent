# src/infrastructure/config/ — 配置加载

kongming-agent 的唯一配置入口：读 YAML、叠加环境变量覆盖、交给 pydantic 校验，输出一份 `Config`。

## 设计理念

| 决策 | 理由 |
|------|------|
| 独立顶层包，不放在 `core/` 下 | 配置加载本质是 I/O（读文件 / 环境变量），属于"把 agent 跑起来"所需的基础设施，和 provider / tool / observability 并列；塞进 core 会让 core 变成事实上的应用层 |
| pydantic v2 模型即 schema 真源 | 默认值、取值范围、跨字段约束都集中在 `models.py`；业务代码不允许再写死 `timeout=60` / `max_turns=10` 这类可调参数 |
| 环境变量用**显式白名单**，不反射扫描 | 字段名本身含下划线（`api_key` / `base_url` / `max_turns`），反射切割的歧义难排除；当前由 `_ENV_FIELD_PATHS` 和 scheduler 专用解析共同维护 |
| 不做多层 YAML overlay | 覆写要么改文件、要么换文件、要么靠环境变量对单字段覆盖；避免配置语义早期过度复杂化 |
| 配置 schema 版本写入 YAML 顶部 | `config_schema_version` 是持久化配置结构版本。历史无版本文件按 `v0` 处理，当前版本为 `v0.6`，启动加载时按版本清单迁移 |
| 启动前自动加载 `KONGMING_HOME/.env` | 本地可写密钥统一落在 runtime home；真实 env 优先于 home `.env`，CI / 容器可直接用 env 覆盖本地文件；把 API key 从 YAML 代码库剥离 |
| 模型状态物理分离 | `setting.model` 只保存 `preset_id/reasoning_effort`；catalog 保存 endpoint、协议、请求默认值、context 与 reasoning capability；secret 保存于 provider-specific env |
| reasoning 能力绑定 catalog model | 每个 `models[*]` 直接声明 adapter、支持档位、默认档位和关闭能力，运行时由 `ModelCatalogManager` 解析 |
| 错误统一继承 `core.errors.ConfigError` | 调用方 `except ConfigError` 就能覆盖配置层所有失败分支，不必感知本包内部细分 |

## 核心流程

### `load_config(path)` 加载路径优先级

1. CLI 参数 `--config <path>` → 显式 `path` 形参（最高优先级）
2. 环境变量 `KONGMING_CONFIG` → 指向某个 YAML
3. 项目根 `config/setting.yaml`（兜底，由 `_REPO_ROOT` 定位）

开发态默认配置文件是 `config/setting.yaml`；CLI 层仍可通过 `--config` 传入其它 YAML，`load_config` 本身只认上述三级路径优先级。

Web sidecar 在进入 `load_config()` 之前会先解析 runtime 配置路径：

1. `KONGMING_CONFIG` 已存在时使用该路径。
2. `<kongming_home>/setting.yaml` 存在时使用该用户级配置。
3. `<kongming_home>/config/setting.yaml` 存在时作为历史布局兼容读取。
4. 以上均不存在时回落到仓库 `config/setting.yaml`。

### 模型配置路径与读取顺序

当前模型配置分成三类真源：

| 真源 | 路径 / 入口 | 内容 | 读取顺序 |
|------|-------------|------|----------|
| 运行默认选择 | 当前 `setting.yaml` 的 `model.preset_id/reasoning_effort` | 默认 preset 与 nullable effort | `KONGMING_MODEL_PRESET_ID`、`KONGMING_MODEL_REASONING_EFFORT` 可覆盖 |
| 静态模型定义 | 内置 `config/model-providers.yaml` + 用户 `<KONGMING_HOME>/model-providers.yaml` | provider、模型、endpoint、auth 引用、请求默认值、context、reasoning capability | 用户同 provider ID 执行完整替换，最终 preset ID 全局唯一 |
| 用户密钥 env | `KONGMING_HOME/.env` + 真实进程 env | `MINIMAX_API_KEY`、`GLM_API_KEY`、`DEEPSEEK_API_KEY` 等 catalog 声明的 credential | `load_config()` 加载 home `.env`，真实进程 env 保持最高优先级 |

CLI `--model-preset <id>` 的解析顺序：

1. `KONGMING_MODEL_PRESET_ID`。
2. `setting.model.preset_id`。
3. `ModelCatalogManager` 在内置与用户 catalog 的合并快照中解析 preset。

Web Composer 模型下拉的读取顺序：

1. 前端调用 `GET /api/model-providers/model-families`。
2. 后端从同一 catalog 快照投影已连接 provider 的 model family、reasoning 档位与 context。
3. 每条模型生成一个 `ConnectedModelFamilyDTO`，前端按 `familyId` 渲染列表、点击后提交 `presetId`。

XSpace 启动流程由宿主先准备 `<kongming_home>/setting.yaml`，再用
`--config <kongming_home>/setting.yaml` 拉起 sidecar。ready/health 成功后，XSpace 调用
`POST /api/xspace/runtime/init` 把当前进程标记为 `xspace`。后续 `/api/config/client` 和
前端能力过滤消费这个内存运行态。普通 Web 启动默认保持 `browser`，避免持久化 setting
里的旧值覆盖启动态。`--host-environment xspace` 和 `KONGMING_WEB_HOST_ENVIRONMENT=xspace`
保留为调试覆盖入口。

### 主配置与 XSpace profile 同步

配置字段新增顺序固定为：

1. 在 `src/infrastructure/config/models.py` 增加或调整 pydantic 字段。
2. 在仓库根 `config/setting.yaml` 显式写入该 leaf 字段和注释。
3. 运行 `uv run python scripts/config-xspace-sync.py review` 查看 XSpace profile 待决策项。
4. 按字段语义选择：
   - `sync-copy`：把主配置值复制到 `config/xspace/setting.yaml`。
   - `xspace-keep`：XSpace 使用产品默认值，原因写入 `config/xspace/sync-policy.yaml`。
   - `main-only`：字段只属于主配置，XSpace profile 保持省略，原因写入 policy。
5. 运行 `uv run pytest tests/unit/config/test_xspace_config_contract.py tests/unit/config/test_config_profile_manager.py -v`。

`ConfigProfileManager` 是维护期 profile 同步入口。它读取主配置、XSpace profile 和
sync policy，展开 Config leaf，校验主配置显式字段、XSpace 差异决策和
`source_hash`。运行期仍读取单个 YAML；profile 同步机制只约束源码维护流程。

policy 缺省只适用于 XSpace YAML 已显式声明且值等于主配置的字段。缺失字段使用
`main-only` 决策，差异值使用 `xspace-keep` 决策。运行期读取当前传入 YAML 和
Config schema 默认值；主配置文件参与维护期 review 和 `sync-copy`。

### 加载 → 迁移 → 覆盖 → 校验五段

1. `_maybe_load_env_file()`：尝试读取 `KONGMING_HOME/.env` 并注入进程环境（`python-dotenv` 未装或无 `.env` 时静默跳过）
2. `migrate_config_if_needed(path)`：缺 `config_schema_version` 的历史 YAML 视为 `v0`，通过 ruamel round-trip 写入版本号并按显式清单补齐字段
3. `_load_yaml(path)`：`yaml.safe_load`，空文件 → `{}`，非 mapping → `ConfigLoadError`
4. `_apply_env_overrides(data)`：遍历显式白名单路径，把命中的环境变量**字符串值**按嵌套 dict 路径写入；scheduler 字段由 `_apply_scheduler_env_overrides()` 单独解析；不提前 cast 类型
5. `Config.model_validate(merged)`：pydantic v2 接手——`"true"` / `"1"` / `"60"` 等强转由它负责；`ValidationError` 包成 `ConfigValidationError` 抛出

测试可传 `load_config(load_env_file=False)` 跳过 .env 注入，断言"纯 YAML 默认值"行为；writer 保存临时文件时传 `migrate=False`，只做 pydantic 校验，避免校验步骤扩大写回 diff。

### schema version 与迁移

当前持久化配置版本为 `v0.6`，字段名为顶层 `config_schema_version`。历史无版本 `setting.yaml` 按 `v0` 处理。迁移入口是 `infrastructure.config.migrations.migrate_config_if_needed(yaml_path)`，由 `load_config()` 和 `ConfigManager.read_raw()` 统一调用。

迁移规则：

- `v0/v0.5 -> v0.6` 按协议、归一化 endpoint、remote model 与 auth header 匹配内置 preset。
- 自定义与本地模型转换成用户 catalog 的完整 provider/model 定义，preset ID 使用稳定内容哈希。
- setting 与用户 catalog 通过 marker、双备份、fsync 和原子替换组成双文件事务；启动发现 marker 时恢复迁移前状态。
- 迁移删除旧静态 `model.*` 与 `web.llm_presets`，保留 `model.preset_id/reasoning_effort`。
- 已完成 v0.6 再次迁移保持零写入；未知版本返回 `ConfigLoadError`。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `infrastructure/config/__init__.py` | `load_config` + `ConfigManager` + 迁移入口 + 所有模型 + 两个 Error | 本包对外符号的单一入口 |
| `infrastructure/config/models.py` | `Config` 与各模块配置模型；`SafetyConfig.approval.llm` 保存 LLM 审批复核器；`SchedulerApprovalConfig` 保持 task-level cron 合同 | pydantic v2 模型，`ConfigDict(extra="forbid")` 严控未知字段；默认值、取值范围、跨字段约束全在这里。 |
| `infrastructure/config/loader.py` | `load_config(path=None, *, load_env_file=True, migrate=True)` + 私有 helper | `KONGMING_HOME/.env` 注入 / 路径解析 / YAML 迁移 / YAML 读取 / 环境变量覆盖 / pydantic 校验。`_MODULE_YAML_MAP` 目前为空（per-module YAML 已合并到单文件） |
| `infrastructure/config/model_provider_catalog.py` | catalog v2 frozen schema、loader、`ResolvedModelConfig` / `ResolvedModelCredential` | `ModelCatalogManager` 的内部实现；校验 provider、model、reasoning capability 与全局 preset 唯一性 |
| `infrastructure/config/model_catalog_manager.py` | `ModelCatalogManager` | 模型目录统一门户：合并 catalog、查询 preset、解析 immutable runtime snapshot、credential 与 reasoning plan |
| `infrastructure/config/migrations.py` | `MigrationResult` / `migrate_config_if_needed` | v0.6 双文件事务迁移与恢复 |
| `infrastructure/config/manager.py` | `ConfigManager` + schema/effective/raw/save/env DTO | 宿主无关配置写回门户；模型选择只允许 `preset_id/reasoning_effort`，provider credential 通过 `.env` 写入 |
| `infrastructure/config/profile_manager.py` | `ConfigProfileManager` + `ProfileReview` / `ProfileReviewIssue` | 维护期 profile 同步门户。负责 `config/setting.yaml`、`config/xspace/setting.yaml` 与 `config/xspace/sync-policy.yaml` 的 leaf diff、source hash、decision 校验和 sync-copy 写回 |
| `infrastructure/config/schema.py` | `FieldMeta` / `list_field_metas` / `list_groups` / `get_field_meta` | 配置字段元数据、展示分组和 `restart_required` 标识。Web 配置页面消费这里，不在 Web 目录维护第二份 schema |
| `infrastructure/config/writer.py` | `round_trip_update` / `PatchItem` / `ConflictError` / `ValidationFailedError` | ruamel.yaml round-trip 写回底座，带 sidecar lock、mtime 冲突检测、原子 replace 和 pydantic 校验 |
| `infrastructure/config/env_writer.py` | `write_env_values` / `EnvWriteResult` / `EnvWriterError` | `.env` 写回底座。已有 key 原地替换，新增 key 追加，写入后同步当前进程 `os.environ` |
| `infrastructure/config/errors.py` | `ConfigLoadError` / `ConfigValidationError` | 都继承 `core.errors.ConfigError`；前者对应路径/解析失败，后者对应字段校验失败 |
| `infrastructure/config/paths.py` | `get_kongming_home` / `resolve_kongming_path` | 统一 `.kongming/` 运行数据根入口。`KONGMING_HOME` 显式覆盖 `kongming_home`（支持绝对路径、相对路径、`~`），默认回退到 `Path.home() / ".kongming"`。`resolve_kongming_path()` 负责把配置中的 `.kongming/*` 派生到 `kongming_home`，只返回 Path，不创建目录。 |

### 配置管理门户

`ConfigManager` 是写回入口，构造时接收 `yaml_path` 和可选 `env_path`。它不包含 Web restart 行为；Web restart 由 `src/hosts/web/dashboard/config/restart.py` 作为宿主适配层处理。

公开方法分三类：

- 通用读取：`read_schema()`、`read_effective()`、`read_raw()`
- 通用写回：`save_patch(patch, expected_mtime)`
- 专用写回：`write_env_values()`

Web 配置页面的 FastAPI router 只做 HTTP 参数解析和异常翻译，运行时从 `app.state.config_manager` 取得 `ConfigManager`。

## 配置

### YAML 字段清单（默认值）

| 路径 | 类型 | 默认 | 约束 |
|------|------|------|------|
| `config_schema_version` | `Literal["v0.6"]` | `"v0.6"` | 持久化配置 schema 版本；历史 v0/v0.5 自动迁移 |
| `model.preset_id` | `str` | 必填 | 指向合并 catalog 中的全局唯一 preset |
| `model.reasoning_effort` | `Literal["none","low","medium","high","max"] \| None` | `None` | 运行默认档位；`none` 表示显式关闭，`None` 回落 catalog 默认 |
| `runner.max_turns` | `int` | `50` | `> 0` |
| `session.backend` | `Literal["memory","sqlite","file"]` | `"memory"` | 交给 `sessions.session_store.build_session` 分派 |
| `session.store_path` | `str` | `".kongming/sessions.db"` | `SQLiteSession` db 路径；`.kongming/*` 派生到 `kongming_home` |
| `session.file_store_path` | `str` | `".kongming/sessions"` | `FileSession` 目录父路径；`.kongming/*` 派生到 `kongming_home` |
| `trace.output_path` | `str` | `".kongming/trace.jsonl"` | `JsonlTraceSink` append-only 写入；`.kongming/*` 派生到 `kongming_home` |
| `trace.auto_flush` | `bool` | `true` | 每次 emit 是否立即 flush |
| `trace.raw_llm` | `bool` | `false` | 是否 dump 每次 LLM HTTP request/response 到 `.kongming/debug/raw-llm-*.json`（调试用，默认关） |
| `logging.level` | `Literal["DEBUG","INFO","WARNING","ERROR"]` | `"INFO"` | |
| `cli.show_reasoning` | `bool` | `false` | 每轮 llm.response 后是否在终端打印 reasoning_content（仅模型返回时才输出） |
| `host.kind` | `Literal["cli"]` | `"cli"` | 第一版仅支持 cli |
| `approval.mode` | `Literal["interactive","auto_allow","auto_deny"]` | `"interactive"` | `auto_*` 给自动化测试 |
| `compactor.enabled` | `bool` | `false` | **默认关**；关闭时 `SessionEngine.build` 装 `_NOOP_COMPACTOR` |
| `compactor.max_messages` / `keep_recent` / `keep_system` / `tool_result_max_chars` | `int` / `int` / `bool` / `int` | `50` / `20` / `true` / `2000` | 仅 `enabled=true` 时生效 |
| `retry.max_retries` | `int` | `3` | `>= 0`；传给 `BaseLLMProvider.max_retries` |
| `retry.retry_backoff` | `float` | `1.0` | `> 0`；传给 `BaseLLMProvider.retry_backoff` |
| `tool.shell.enabled` / `timeout_seconds` / `max_stream_bytes` / `terminate_grace_seconds` | `bool` / `float` / `int` / `float` | `true` / `30.0` / `8000` / `2.0` | 全部传给 `build_shell_tool` |
| `tool.file.enabled` / `read_max_bytes` | `bool` / `int` | `true` / `65536` | 全部传给 `build_file_tools` |
| `evolution.memory.enabled` | `bool` | `true` | CLI 按此决定是否加载 memory、注入 prompt 并注册 MemoryTool；工具调用仍经过统一 SafetyDecisionEngine。 |
| `evolution.memory.root_path` | `str` | `".kongming/memory"` | 支持 `~` 展开；`.kongming/*` 派生到 `kongming_home`；CLI 解析成绝对 `memory_dir` |
| `evolution.memory.inject_prompt` | `bool` | `true` | false 时仍加载活态 entries，但不向 system prompt 追加 memory 段 |
| `evolution.memory.read_max_chars` | `int` | `65536` | 单文件读取上限 |
| `evolution.memory.view_max_chars` | `int` | `8000` | `MemoryTool.view` 返回上限 |
| `evolution.learning.enabled` | `bool` | `false` | 进化模块总开关；控制公开 Tool、after-run lifecycle 与 cadence |
| `evolution.learning.auto_trigger_enabled` | `bool` | `true` | cadence 自动触发开关；false 时保留 `request_evolution_review` 手动路径 |
| `evolution.learning.every_n_runs` / `min_user_turns` | `int` / `int` | `5` / `3` | 自动触发 cadence 与冷启动门槛；手动请求跳过两项 |
| `safety.approval.llm` | `SafetyApprovalLlmConfig \| None` | `None` | LLM 复核器的 provider、model、base_url、api_key、timeout；只由 per-cwd `llm` 模式消费。 |

thread allow/deny 不进入 config schema，独立保存到 `<kongming_home>/safety/thread_permissions/<sha256(thread_id)>.json`。处置模式独立保存到 `<kongming_home>/web/auto_approval/<cwd_hash>.json`；loader 对旧全局 `safety.approval_mode` / `safety.auto_judge` 返回定向错误，要求在聊天页按 cwd 设置模式。

`model.preset_id` 是最小必填字段；其它顶层子节通过 `default_factory` 补齐。

### 环境变量覆盖清单

`KONGMING_<SECTION>_<FIELD>` 全大写，显式白名单由 `_ENV_FIELD_PATHS` + `_apply_scheduler_env_overrides` 维护，按用途分组：

- `KONGMING_MODEL_{PRESET_ID,REASONING_EFFORT}`
- `KONGMING_RUNNER_MAX_TURNS`
- `KONGMING_SESSION_{BACKEND,STORE_PATH,FILE_STORE_PATH}`
- `KONGMING_TRACE_{OUTPUT_PATH,AUTO_FLUSH,RAW_LLM}`
- `KONGMING_LOGGING_LEVEL`
- `KONGMING_HOST_KIND`
- `KONGMING_APPROVAL_MODE`
- `KONGMING_TOOL_SHELL_{ENABLED,TIMEOUT_SECONDS,MAX_STREAM_BYTES,TERMINATE_GRACE_SECONDS}`
- `KONGMING_TOOL_FILE_{ENABLED,READ_MAX_BYTES}`
- `KONGMING_COMPACTOR_{ENABLED,MAX_MESSAGES,KEEP_RECENT,KEEP_SYSTEM,TOOL_RESULT_MAX_CHARS}`
- `KONGMING_RETRY_{MAX_RETRIES,RETRY_BACKOFF}`
- `KONGMING_EVOLUTION_MEMORY_{ENABLED,ROOT_PATH,INJECT_PROMPT,READ_MAX_CHARS,VIEW_MAX_CHARS}`
- `KONGMING_EVOLUTION_LEARNING_{ENABLED,MODE,BACKGROUND,AUTO_TRIGGER_ENABLED,PRESET_ID,REASONING_EFFORT,EVERY_N_RUNS,MIN_USER_TURNS,MAX_HISTORY_MESSAGES,MAX_NUTRIENTS,NUTRIENT_CONFIDENCE_THRESHOLD,REVIEW_TIMEOUT_SECONDS,DRAIN_ON_CLOSE_SECONDS,ROOT_PATH}`
- `KONGMING_SCHEDULER_{APPROVAL_MODE,DEFAULT_MAX_TURNS}`（v0.5/v0.5.1 新增，由 `_apply_scheduler_env_overrides` 独立处理）

另有特殊环境变量不参与 Config 模型字段覆盖：`KONGMING_CONFIG`（决定配置文件路径）、`KONGMING_HOME`（决定 runtime home）、`KONGMING_MODEL_PROVIDER_CATALOG`（决定 provider catalog 路径）、`KONGMING_TRACE_RAW_LLM`（`raw_dump` 运行时临时覆盖）。

#### 入口级特殊 env（不经过 Config 模型）

`KONGMING_HOME` 与 `KONGMING_CONFIG` 同类，属于"入口级特殊 env"，**不在 `_ENV_FIELD_PATHS` 白名单里**，也不走 pydantic 校验。它由 `infrastructure.config.paths.get_kongming_home()` 直接读取（支持绝对路径、相对路径、`~` 展开），用于显式覆盖 `kongming_home`。调用方访问 `.kongming` 运行数据根时统一通过 `get_kongming_home()`；消费 `.kongming/*` 配置字段时统一通过 `resolve_kongming_path()`。

`cli.show_reasoning` 通过 CLI `--show-reasoning` 控制。旧静态 `KONGMING_MODEL_PROVIDER/NAME/BASE_URL/API_KEY/API_KEY_HEADER/TIMEOUT/MAX_TOKENS/TEMPERATURE` 会返回带迁移指引的 retired diagnostic。

覆盖规则只影响**标量字段**（str / int / float / bool）；不允许通过环境变量整体替换一个 section。多层嵌套（如 `tool.shell.enabled`）按下划线切成 section path 后在原始 dict 上按路径写入。

## 已知问题 / 待完成

- **env 覆盖必须显式列白名单**：新增字段时要同步改 `loader._ENV_FIELD_PATHS`，否则 `KONGMING_*` 环境变量会被静默忽略。考虑未来用 pydantic 模型的 `model_fields` 结合命名约定来自动生成路径（避免反射歧义的手法待定）。
- **env 值类型全部字符串**：loader 把 cast 交给 pydantic v2；非法值通过 `ConfigValidationError.errors` 定位。
- **多层 YAML overlay 明确不做**：`_MODULE_YAML_MAP` 当前为空，per-module YAML 文件已合并到单文件 `setting.yaml`。若要 "基线 + 覆盖" 两层，当前策略是换文件或直接写一个新 YAML。
- **`get_kongming_home()` 未被全量采纳**：v0.1.3 仅 prompts 装配通过该入口解析 `.kongming/` 根目录；其他模块（memory / trace / sessions）尚未统一使用 `get_kongming_home()`，仍自行拼 `.kongming/xxx`（如 `trace.output_path=".kongming/trace.jsonl"`、`session.store_path=".kongming/sessions.db"`）。未来可另立 task 统一。

## 参考

- [接口契约](../../spec/kongming-agent-v1-minimal/10-contracts.md)
- [v1 文件布局](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md)
- [`config/README.md`](../../../config/README.md)（catalog、选择、credential 三类真源）
- [model-catalog-runtime-config-separation](../../../openspec/changes/model-catalog-runtime-config-separation/)（v0.6 当前实现权威）
- [Finding 1 fix-report（session.backend 接通）](../../fixes/20260420-v1mini-doc-conformance/fix-report.md)
