# `src/sitian/` — 司天（工作区观察者系统）

主动扫描指定路径，沉淀工作区全局状态到 `SiTianRecords`，基于状态给出下一步建议。

## 设计理念

| 决策 | 理由 |
|------|------|
| 配置显式声明观察源（v1 不自动发现） | 避免隐式全工作区扫描造成噪音 |
| observations append-only，workspace_state 覆盖写 | 原始事实不丢，派生状态可重建 |
| `SiTian` 前缀统一公共函数命名 | 系统边界收紧，后续 v2 扩展不冲突 |
| 配置模型住 `sitian` 包而非 `infrastructure.config` | 切断 `web → infrastructure.config → core` 传递依赖 |
| `claude_workspace` per-project 展开为多个 work_item | 一个 workspace source 包含 N 个项目，需独立跟踪状态 |
| `output_subdir` 隔离多 kind 产物 | 同一 root 下 `claude/` / `codex/` / `general/` 共存 |
| frozen dataclass（非 pydantic）做运行时模型 | 配置入参用 pydantic 校验；运行时数据轻量 frozen 即可 |
| `global_scanner.py` 自建全局扫描器，不依赖 `web` 包 | 切断 `sitian → web.integrations.claude_code.projects_scanner` 跨层引用；`list_claude_projects_global` / `list_codex_projects_global` 直接读 `~/.claude/projects` / `~/.codex/sessions` 目录结构 |
| `analyzer.py` LLM 分析层独立于扫描 | 扫描产 observations → analyzer 独立调 LLM 生成 `SiTianReport`（alerts + session 行动队列）；V2 schema 按 alert + session 组织；analyzer 不改 observations，只追加报告产物 |
| analyzer 模型通过 catalog preset 解析 | `sitian.analyzer.preset_id/reasoning_effort` 只保存运行选择；CLI 通过 `ModelCatalogManager` 构造独立 snapshot 与 provider |
| `session_reader.py` 逆序流式读取 session jsonl | 大文件不一次性 `json.loads`；`_ReverseLineIterator` 从文件尾部逐块读取，`read_claude_session_tail` 取最近 N 条消息文本，供扫描器填充 `recent_activity` |

## 核心流程

```
加载 SiTianConfig(yaml) → 校验 source.id 唯一 / path 非空 / kind 合法
  ↓
ensure_layout → mkdir + 空 observations.jsonl + 空 workspace_state
  ↓
读 runtime_state → 筛选 next_run_at <= now 的 ready sources
  ↓
按 kind 分派扫描（scanners.py + global_scanner.py + session_reader.py）：
  generic_channel  → rglob + include/exclude + mtime top 20
  generic_chat     → ThreadMetadata 过滤 + FileSession manifest/JSONL 按 thread_id 关联 + 尾部消息读取
  claude_project   → 文件扫描 + session_reader 读最近活动消息
  codex_project    → 文件扫描 + codex session 关联
  claude_workspace → global_scanner.list_claude_projects_global top_n → 1+2N 条 observations
  ↓
追加 observations.jsonl + 覆盖 runtime_state.json
  ↓
归并 work_items（claude_workspace per-project 展开；其他 1:1）
  ↓
生成 suggestions + summary → 写 latest_suggestions.json / latest_summary.md
  ↓
（可选）analyzer.py 调 LLM → SiTianReport（alerts + session 行动队列）→ 写 sitian_report.json / latest_analysis.md
```

## 代码索引

| 文件 | 导出 | 说明 |
|------|------|------|
| `config.py` | `SiTianConfig`, `SiTianSourceConfig` | pydantic 配置模型；source kind 枚举、output_subdir 验证 |
| `models.py` | `SiTianObservation`, `SiTianSourceRuntimeState`, `SiTianWorkItem`, `SiTianWorkspaceSourcesSummary`, `SiTianWorkspaceBlocker`, `SiTianPendingApproval`, `SiTianWorkspaceRisk`, `SiTianWorkspaceState`, `SiTianAlert`, `SiTianProjectSnapshot`, `SiTianReport` 等 11 个 frozen dataclass | 运行时数据模型，含 `to_dict()` / `from_dict()` 序列化；V2 新增 `SiTianAlert` / `SiTianProjectSnapshot` / `SiTianReport`（analyzer 产出） |
| `store.py` | `SiTianRecordsStore`, `resolve_sitian_root`, `decode_work_item_filename` | 异步文件存储层；atomic write + asyncio.Lock；管理 observations / runtime_state / workspace_state / work_items / scans / sitian_report / observations_hash 全部 I/O |
| `scanners.py` | `SiTianScanSource`, `SiTianScanBatch` | 4 种 kind 的扫描实现；使用 `global_scanner.py` 和 `session_reader.py` 替代原 `web` 包依赖；scan 耗时审计字段 |
| `global_scanner.py` | `SiTianSessionInfo`, `SiTianProjectInfo`, `SiTianCodexSessionInfo`, `SiTianCodexProjectInfo`, `SiTianKongmingSessionInfo`, `SiTianKongmingProjectInfo`, `list_claude_projects_global`, `list_codex_projects_global`, `list_kongming_generic_chat_projects` | 自建全局扫描器（解耦 `web` 包依赖）：直接读 `~/.claude/projects`、`~/.codex/sessions` 与 `<kongming_home>/web/threads + sessions`；按 mtime 排序 + title 提取 |
| `session_reader.py` | `read_claude_session_tail`, `read_kongming_session_tail` | Claude 与 Kongming FileSession JSONL 逆序流式读取器；`_ReverseLineIterator` 从文件尾部逐块读，取最近 N 条 user/assistant 文本 |
| `analyzer.py` | `sitian_analyze`, `report_to_markdown`, `compute_observations_hash` | LLM 分析层（V1 + V2 schema）：observations → LLM prompt → `SiTianReport`（alerts + session 行动队列 + project snapshots）；审计日志 + 项目级默认路径；`compute_observations_hash` 用于增量分析判断 |
| `suggestions.py` | `SiTianMaterializeState`, `SiTianBuildSummaryMarkdown` | observations → work_items 归并 + 建议/blocker/risk 生成 + markdown 摘要 |
| `service.py` | `SiTianRunOnce`, `SiTianRunLoop`, `SiTianReadState`, `SiTianRunResult` | 编排层：一轮扫描全链路（含 `_run_llm_analysis` 集成 analyzer）/ 循环模式 / 只读状态查询 |
| `cli.py` | `main`（click group） | CLI 入口：`run-once` / `loop` / `state` 三个子命令 |
| `__init__.py` | re-export 全部公共符号 | 包入口 |

## 配置

- 配置文件：`config/sitian.local.yaml`（`SiTianConfig` section）
- 封装脚本：`./sitian.sh`（scan / state / loop / summary / clean）
- 默认产物根：`<kongming_home>/sitian/`，其中 `kongming_home` 默认是 `Path.home() / ".kongming"`，可由 `KONGMING_HOME` 显式覆盖
- 环境变量：`SITIAN_CONFIG`（脚本层配置路径）、`SITIAN_ROOT`（脚本层产物根目录覆盖）
- analyzer 模型：`sitian.analyzer.preset_id/reasoning_effort`；空 preset 继承 `setting.model.preset_id`
- 产物目录：`<产物根>/<output_subdir>/`（含 observations.jsonl、runtime_state.json、workspace_state.json、work_items/、scans/、latest_suggestions.json、latest_summary.md、sitian_report.json、latest_analysis.md、observations_hash.txt）

## 测试

| 测试文件 | 覆盖 |
|----------|------|
| `tests/unit/test_sitian_service.py` | service 层编排（RunOnce / ReadState） |
| `tests/unit/test_sitian_cli.py` | CLI 命令解析与输出 |

## 设计文档

[docs/spec/sitian-v1/](../../spec/sitian-v1/README.md) — 完整 spec（模块拆分 / 核心流程 / 数据模型 / task map）

## 已知问题 / 待完成

- 控制面展示（CLI / Web / Whiteboard 适配层）尚在方案阶段，未进入开发
- ~~`scanners.py` 跨包依赖 `web.integrations.claude_code.projects_scanner`~~——已解耦，改为自建 `global_scanner.py`
