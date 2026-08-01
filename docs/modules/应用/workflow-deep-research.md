# workflow: deep_research

`deep_research` 是面向开放式调研问题的证据链 workflow。它把研究主题转换为搜索线，收集来源 URL，读取来源内容，抽取可引用事实，按事实组做对抗式裁决，最终写出带来源、投票比分和统计信息的 Markdown 报告。

当前 v0.1 运行形态是确定性阶段链路：Plan / Search / Extract / Group / Crosscheck / Report 都由 `DeepResearchStrategy` 在主 workflow 进程内执行，并通过 `task.log.jsonl` 记录阶段 task。网页搜索对外统一走 `web_search` 工具；底层 MCP 或用户搜索工具可用时返回搜索结果，缺底层搜索能力时返回工具缺失。

## 入口

公开工具入口是 `run_agent_workflow`：

```json
{
  "mode": "deep_research",
  "payload": {
    "topic": "调研 Kongming deep_research 当前实现是否支持注入搜索 provider",
    "objective": "判断现有实现和可优化点",
    "source_queries": [
      {
        "query_id": "q1",
        "line": "Kongming deep_research source provider",
        "intent": "overview",
        "max_results": 3
      }
    ],
    "limits": {
      "source_budget": 6,
      "fetch_budget": 4,
      "fact_cap": 12,
      "jury_size": 3,
      "reject_quorum": 2,
      "max_content_chars": 60000,
      "search_results_per_line": 6,
      "fetch_concurrency": 4,
      "jury_concurrency": 6,
      "workflow_timeout_seconds": 2400
    },
    "source_policy": {
      "language": "zh-CN",
      "freshness_days": null,
      "allowed_domains": [],
      "blocked_domains": [],
      "prefer_primary_sources": true
    },
    "output_contract": "deep_research_report",
    "audit_tags": ["research"]
  }
}
```

`topic` 是必填字段。`objective` 可省略，工具层会用 `topic` 补齐。`output_contract` 固定为 `deep_research_report`。

工具层还接受这些模型友好别名：

| 输入形态 | 归一化结果 | 代码位置 |
|----------|------------|----------|
| `payload.DeepResearchSpec` | 解包为 payload 顶层字段 | `src/tools/agent_workflow_tool.py::_unwrap_deep_research_spec_payload` |
| `source_provider_fixture` | 复制到 `source_fixture` | `src/tools/agent_workflow_tool.py::_normalize_deep_research_payload` |
| `search_plan.lines` / `search_plan.queries` | 转成 `source_queries` | `src/tools/agent_workflow_tool.py::_deep_research_queries_from_search_plan` |
| 缺省 `source_queries` | 生成 overview / primary_source / skeptical 三条查询 | `src/tools/agent_workflow_tool.py::_normalize_deep_research_payload` |
| 缺省 `limits` / `source_policy` | 写入默认预算和检索偏好 | `src/tools/agent_workflow_tool.py::_normalize_deep_research_payload` |

策略 parser 接收归一化后的 `DeepResearchSpec` 字段，并校验预算范围：

| 字段 | 默认值 / 约束 |
|------|---------------|
| `limits.source_budget` | 默认 10，范围 1-15 |
| `limits.fetch_budget` | 默认 10，必须 >= 0 |
| `limits.fact_cap` | 默认 20，范围 1-25 |
| `limits.jury_size` | 默认 3，必须 > 0 |
| `limits.reject_quorum` | 默认 2，必须 > 0 且 <= `jury_size` |
| `limits.max_content_chars` | 默认 60000，必须 > 0 |
| `source_policy.language` | 默认 `zh-CN` |
| `report.max_findings` | 默认 12，必须 > 0 |

## 注册与装配

| 入口 | 行为 |
|------|------|
| `src/application/agent_workflows/manager.py::AgentWorkflowManager.__init__` | 默认注册 `DeepResearchStrategy(self)`，`list_workflow_strategies()` 可看到 `deep_research` |
| `src/tools/agent_workflow_tool.py::RunAgentWorkflowTool` | 在 schema 的 `mode` enum 暴露 `deep_research`，失败时给出专用参数修正骨架 |
| `src/hosts/web/run.py::_bind_agent_workflow_manager` | Web thread runtime 构造 `AgentWorkflowManager` 时注入来源 provider 和 diagnostics |
| `src/hosts/web/research_source_provider.py::WebResearchSourceProviderFactory` | 从 Web tool registry 里选择 search/fetch 工具并适配为 `ResearchSourceProvider` |

## 核心流程

```text
run_agent_workflow(mode="deep_research")
  -> _normalize_workflow_payload(...)
  -> AgentWorkflowManager.run_workflow_payload(...)
  -> DeepResearchStrategy.run(...)
  -> parse_deep_research_spec(payload)
  -> write workflow manifest(status=running)
  -> Plan: write deep_research/plan.json
  -> Search: ResearchSourceManager.collect_sources(...)
       -> provider.search(query)
       -> SourceDeduper.select(...)
       -> provider.fetch(candidate)
       -> write sources.jsonl + sources.selected.jsonl
  -> Extract: deterministic fact extraction from fetched/failed sources
       -> write facts.jsonl
  -> Group: one fact -> one FactGroup in v0.1
       -> write groups.jsonl
  -> Crosscheck: deterministic fallback juror votes
       -> aggregate_jury_rulings(...)
       -> write rulings.jsonl + groups.checked.jsonl
  -> Report: build report.md + stats.json + phase_summaries.json
  -> write result.json + workflow.json(status=completed)
```

### 当前阶段 task 记录

`DeepResearchTaskLogWriter` 为每个阶段写一组虚拟 task，不调用 `AgentManager.spawn()`。每个 task 的 `task_run_id` 是 `phase-<phase>`，角色名是 `<phase>_fallback`。

| 阶段 | task_run_id | 输入 artifact | 输出 artifact |
|------|-------------|---------------|---------------|
| plan | `phase-plan` | 无 | `deep_research/plan.json` |
| search | `phase-search` | `plan.json` | `sources.jsonl`, `sources.selected.jsonl` |
| extract | `phase-extract` | `sources.jsonl` | `facts.jsonl` |
| group | `phase-group` | `facts.jsonl` | `groups.jsonl` |
| crosscheck | `phase-crosscheck` | `groups.jsonl` | `rulings.jsonl`, `groups.checked.jsonl` |
| report | `phase-report` | `groups.checked.jsonl` | `report.md`, `stats.json`, `phase_summaries.json` |

每个阶段写：

```text
agents/<task_run_id>/
  subagent.json
  task.log.jsonl
```

`task.log.jsonl` 记录 `started` 和 `completed` 事件，字段包括 `phase`、`role`、`prompt_hash`、`tool_allowlist`、`budget_snapshot`、`input_artifacts`、`output_artifacts` 和 `metadata`。`prompt_hash` 来自 `phase/topic/objective/output_contract` 的 SHA256 前 16 位，当前实现没有单独的 LLM prompt 模板文件。

## 来源 provider

Deep Research 的外部来源能力只通过 `ResearchSourceProvider` 协议进入策略：

```text
name: str
search(query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]
fetch(candidate: ResearchSourceCandidate) -> ResearchSourceRecord
```

provider 解析优先级：

| 顺序 | 来源 |
|------|------|
| 1 | `AgentWorkflowManager.deep_research_source_provider` |
| 2 | `DeepResearchStrategy(source_provider=...)` |
| 3 | payload 中的 `source_fixture`，由 `FakeResearchSourceProvider` 执行 |
| 4 | `_DeterministicResearchSourceProvider` 离线兜底 |

`ResearchSourceManager.collect_sources()` 的具体动作：

| 步骤 | 行为 | 产物 / 审计 |
|------|------|-------------|
| search | 对每条 `ResearchSourceQuery` 调用 provider.search，并按 `max_results` 截断 | `deep_research.search_failed` |
| normalize | 补齐 canonical URL 和 `src-<hash>` source id | `normalize_candidate()` |
| dedupe | 去掉 `utm_*` 等跟踪参数、折叠重复 URL、按 `source_budget` 裁剪 | `deep_research.source_selected` / `deep_research.source_duplicate` / `deep_research.source_overflow` |
| fetch | 对预算内候选调用 provider.fetch，按 `max_content_chars` 截断正文 | `deep_research.fetch_failed` |
| record | 汇总 selected、failed、candidate、duplicate 记录 | `deep_research.source_recorded` |

Web 装配使用这些默认工具名：

| 类型 | 默认候选 |
|------|----------|
| search | `deep_research_search`, `web_search`, `search_web`, `browser_search` |
| fetch | `deep_research_fetch`, `web_fetch`, `fetch_url`, `browser_fetch` |

`web_fetch` 由默认 `ToolRegistry` 提供，成功结果中的 `content` / `content_text` 会作为正文进入 strong source；blocked/error 结果保留 `status`、`reason` 和 `suggestion` 供报告和诊断使用。

fetch 工具缺失时，adapter 会尝试复用 search 结果中的 `content_text`。没有正文时返回 weak record，`error_code="fetch_tool_unavailable"`。

## 数据结构

### DeepResearchSpec

```text
topic: str
objective: str
source_queries: tuple[ResearchSourceQuery, ...]
limits: DeepResearchLimits
source_policy: DeepResearchSourcePolicy
output_contract: "deep_research_report"
source_fixture: Mapping[str, object]
audit_tags: tuple[str, ...]
report: DeepResearchReportOptions
mode: "deep_research"
```

### ResearchSourceQuery

```text
query_id: str
line: str
intent: str
max_results: int
```

### ResearchSourceCandidate

```text
source_id: str
query_id: str
url: str
canonical_url: str
title: str
snippet: str
rank: int
provider_name: str
```

### ResearchSourceRecord

```text
source_id: str
query_id: str
url: str
canonical_url: str
title: str
status: "candidate" | "selected" | "fetched" | "skipped" | "failed" | "duplicate"
tier: "primary" | "secondary" | "blog" | "forum" | "weak" | "strong" | "duplicate"
content_text: str | None
error_code: str | None
error_message: str | None
provider_name: str
rank: int
duplicate_of: str | None
```

### ResearchFactRecord

```text
fact_id: str
source_id: str
statement: str
citation: str
status: "pending" | "upheld" | "rejected"
```

### FactGroup

```text
group_id: str
canonical_statement: str
member_fact_ids: tuple[str, ...]
source_ids: tuple[str, ...]
best_excerpt: str
support_count: int
```

### JuryRuling / CheckedFactGroup

```text
JuryRuling:
  ruling_id: str
  group_id: str
  juror_id: str
  reject: bool
  abstain: bool
  reason: str
  contradicting_evidence: tuple[str, ...]
  source_coverage: str

CheckedFactGroup:
  group_id: str
  status: "pending" | "upheld" | "rejected"
  cast_count: int
  reject_count: int
  abstain_count: int
  tally: str
  decision_reason: str
```

## 产物

一次运行的目录结构：

```text
agent-workflows/<workflow_id>/              # 单次 workflow 运行根目录，<workflow_id> 是本次运行的唯一 ID
  workflow.json                             # 公共 workflow manifest，记录 mode、状态、开始/结束时间
  audit.jsonl                               # 公共审计日志，记录阶段开始/完成、来源选择、去重和 provider 诊断
  result.json                               # 公共运行结果，包含 deep_research artifact_paths、stats 和 report_path
  reports/                                  # 通用 workflow 报告索引目录，供 AgentWorkflowResult 和 viewer 读取
    index.json                              # 报告索引；deep_research 当前不写子 agent report 明细
  agents/                                   # 阶段 task 目录，当前用虚拟 task 表达 Plan/Search/Extract 等阶段
    phase-plan/                             # Plan 阶段目录，记录研究计划生成任务
      subagent.json                         # 阶段 task 索引，包含 task_run_id、phase、role 和 task_log_path
      task.log.jsonl                        # Plan 阶段 started/completed 事件
    phase-search/                           # Search 阶段目录，记录来源检索、去重、fetch 和选择任务
      subagent.json                         # Search 阶段 task 索引
      task.log.jsonl                        # Search 阶段 started/completed 事件和输出 artifact 路径
    phase-extract/                          # Extract 阶段目录，记录从来源内容抽取事实的任务
      subagent.json                         # Extract 阶段 task 索引
      task.log.jsonl                        # Extract 阶段 started/completed 事件
    phase-group/                            # Group 阶段目录，记录事实分组任务
      subagent.json                         # Group 阶段 task 索引
      task.log.jsonl                        # Group 阶段 started/completed 事件
    phase-crosscheck/                       # Crosscheck 阶段目录，记录 jury 裁决和事实组状态聚合任务
      subagent.json                         # Crosscheck 阶段 task 索引
      task.log.jsonl                        # Crosscheck 阶段 started/completed 事件
    phase-report/                           # Report 阶段目录，记录最终报告、统计和阶段摘要写入任务
      subagent.json                         # Report 阶段 task 索引
      task.log.jsonl                        # Report 阶段 started/completed 事件
  deep_research/                            # deep_research 专属结构化产物目录，Report 和 viewer 主要读取这里
    spec.json                               # 预留规格文件；writer 初始化时创建，当前内容为空对象
    plan.json                               # 研究计划，包含 topic、objective、source_queries、limits 和 source_policy
    sources.jsonl                           # 全部来源记录，包含 fetched、failed、candidate、duplicate 等状态
    sources.selected.jsonl                  # 进入 Extract 的非 duplicate 来源记录
    facts.jsonl                             # Extract 阶段抽取出的可引用事实
    groups.jsonl                            # Group 阶段生成的事实组
    rulings.jsonl                           # Crosscheck 阶段生成的事实组聚合裁决
    groups.checked.jsonl                    # fact group 与 ruling 的配对结果，Report 阶段输入
    stats.json                              # 来源数、事实数、组数、裁决数和 provider 统计
    phase_summaries.json                    # 六个阶段的完成摘要、输出 artifact 和 metadata
    report.md                               # 最终 Markdown 研究报告
```

`result.json` 的 `deep_research` 节点包含：

| 字段 | 说明 |
|------|------|
| `topic` / `objective` | 研究输入 |
| `source_provider` | 实际使用的 provider 名称 |
| `artifact_paths` | `DeepResearchArtifactWriter.artifact_paths()` 加 plan / selected sources / checked groups / phase summaries |
| `stats` | 来源数、事实数、组数、裁决数和 provider 统计 |
| `report_path` | 最终 Markdown 报告路径 |
| `phase_summaries` | 内存中同一份阶段摘要 |
| `source_provider_diagnostics` | Web provider 工厂诊断，可能为 null |

工具返回文本会额外输出：

```text
deep_research_report: <workflow_dir>/deep_research/report.md
```

## 报告内容

当前 `report.md` 由 `_build_report()` 确定性生成：

| 段落 | 来源 |
|------|------|
| H1 | `spec.topic` |
| Objective | `spec.objective` |
| Tally | upheld / rejected group 计数和事实数 |
| Findings | 每个 `CheckedFactGroup` 的状态、statement、`tally`、`decision_reason` 和 citation |
| Sources | 全部 source record 的 title、URL、status/tier |
| Citations | 每条 fact statement 对应 URL |

事实抽取规则：

| 来源状态 | fact statement |
|----------|----------------|
| `fetched` 且有正文 | 正文第一句，最长 500 字符 |
| `failed` / `candidate` | `Source unavailable: <title/url>; reason=<error>` |
| `duplicate` | 跳过 |

裁决规则：

| 条件 | 结果 |
|------|------|
| fact statement 包含 `unavailable` | 生成 reject fallback votes |
| reject 数 >= `reject_quorum` | `status="rejected"`，`decision_reason="reject_quorum"` |
| 有有效投票且 reject 数未达 quorum | `status="upheld"`，`decision_reason="upheld"` |
| 全部弃权 | `status="rejected"`，`decision_reason="insufficient_casts"` |

## 配置

Web 来源 provider 配置位于 `Config.web.deep_research_source_provider`：

```yaml
web:
  deep_research_source_provider:
    enabled: true
    provider_name: web_user_tool_research_source
    search_tool_name: web_search
    fetch_tool_name: web_fetch
    search_tool_names:
      - deep_research_search
      - web_search
      - search_web
      - browser_search
    fetch_tool_names:
      - deep_research_fetch
      - web_fetch
      - fetch_url
      - browser_fetch
```

支持的环境变量：

| 环境变量 | 目标字段 |
|----------|----------|
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_ENABLED` | `enabled` |
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_PROVIDER_NAME` | `provider_name` |
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_SEARCH_TOOL_NAME` | `search_tool_name` |
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_FETCH_TOOL_NAME` | `fetch_tool_name` |
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_SEARCH_TOOL_NAMES` | `search_tool_names` |
| `KONGMING_WEB_DEEP_RESEARCH_SOURCE_PROVIDER_FETCH_TOOL_NAMES` | `fetch_tool_names` |

这些字段也由 `src/infrastructure/config/schema.py` 暴露给配置管理页。

## 代码索引

| 文件 | 导出 / 内容 | 说明 |
|------|-------------|------|
| `src/application/agent_workflows/manager.py` | `AgentWorkflowManager` | 注册 `DeepResearchStrategy`，持有 provider 和 diagnostics，写公共 workflow 产物 |
| `src/application/agent_workflows/strategies/deep_research/__init__.py` | public exports | 导出策略、合同、provider、dedupe、artifact writer 和 jury API |
| `src/application/agent_workflows/strategies/deep_research/contracts.py` | `DeepResearchSpec` / `ResearchSourceProvider` / parser / dataclasses | payload 合同、来源合同、事实 / 裁决 / 报告数据结构 |
| `src/application/agent_workflows/strategies/deep_research/dedupe.py` | `SourceDeduper` / `canonicalize_url` / `stable_source_id` | URL 规范化、跟踪参数清理、重复来源折叠、预算溢出 |
| `src/application/agent_workflows/strategies/deep_research/source_provider.py` | `ResearchSourceManager` / `FakeResearchSourceProvider` | provider search/fetch 编排、失败降级和 fake fixture |
| `src/application/agent_workflows/strategies/deep_research/fact_board.py` | `DeepResearchArtifactWriter` | 固定 deep_research artifact 路径和 JSONL/JSON/Markdown 写入 |
| `src/application/agent_workflows/strategies/deep_research/jury.py` | `AdversarialJury` / `aggregate_jury_rulings` | reject quorum、abstain、tally 和 fallback vote 语义 |
| `src/application/agent_workflows/strategies/deep_research/task_log.py` | `DeepResearchTaskLogWriter` | 阶段 task log、subagent.json 索引和 workflow audit 事件 |
| `src/application/agent_workflows/strategies/deep_research/strategy.py` | `DeepResearchStrategy` | Plan/Search/Extract/Group/Crosscheck/Report 状态机 |
| `src/tools/agent_workflow_tool.py` | `RunAgentWorkflowTool` | `deep_research` schema、payload 归一化、失败提示和 result 投影 |
| `src/hosts/web/research_source_provider.py` | `WebResearchSourceProviderFactory` / `UserToolResearchSourceProviderAdapter` | Web tool registry 到 `ResearchSourceProvider` 的适配 |
| `src/hosts/web/run.py` | `_bind_agent_workflow_manager` | Web thread runtime 绑定 workflow manager 和来源 provider |
| `src/infrastructure/config/models.py` | `WebDeepResearchSourceProviderConfig` | Web provider 配置模型 |
| `src/infrastructure/config/loader.py` | env override list | provider 配置环境变量加载 |
| `src/infrastructure/config/schema.py` | field metas | 配置页字段元数据 |

## 测试索引

| 测试 | 覆盖 |
|------|------|
| `tests/unit/test_agent_workflow_tool_deep_research.py` | tool schema、默认值归一化、tool -> manager 分流、失败提示 |
| `tests/unit/test_deep_research_contracts.py` | payload parser、limits、source policy、report options |
| `tests/unit/test_deep_research_source_dedupe.py` | canonical URL、source id、重复折叠 |
| `tests/unit/test_deep_research_fact_board.py` | artifact writer 路径和 JSONL/JSON/Markdown 写入 |
| `tests/unit/test_deep_research_jury.py` | quorum、abstain、tally、fallback votes |
| `tests/unit/test_deep_research_subagent_task_logs.py` | task.log.jsonl、subagent.json、audit 索引 |
| `tests/unit/test_agent_workflow_deep_research_strategy.py` | strategy 描述、确定性产物和 result data |
| `tests/unit/test_web_agent_workflow_manager_deep_research_binding.py` | Web manager 绑定来源 provider |
| `tests/unit/test_web_deep_research_source_provider_factory.py` | Web provider 工厂和工具返回值归一化 |
| `tests/unit/config/test_deep_research_source_provider_config.py` | env override 和 schema field meta |
| `tests/integration/test_deep_research_source_provider.py` | provider adapter search/fetch 协议 |
| `tests/e2e/test_deep_research_workflow.py` | tool 到 strategy 到 artifact 的离线 smoke |
| `tests/e2e/test_deep_research_injected_source_provider_workflow.py` | 注入 provider 的 URL 流转、phase summary 和 diagnostics |
| `tests/e2e/test_web_deep_research_thread_smoke.py` | Web thread 真实入口 smoke |

## 已知边界 / 待完成

| 项 | 状态 |
|----|------|
| planner/searcher/extractor/grouper/juror/reporter 真实子 agent | 当前由确定性阶段 task log 表达；后续版本可通过父 `AgentManager.spawn()` 派生阶段 child |
| 来源质量 | 报告质量取决于 provider.search/fetch 返回的标题、正文和 URL 覆盖 |
| 事实合并 | 当前一条事实生成一个 group；语义等价合并还需要后续策略实现 |
| jury 交叉审查 | 当前使用 deterministic fallback votes；真实反向证据搜索还需要接入 juror 子 agent |
| Web 专用详情页 | 当前主要依赖通用 workflow artifacts 和 task log；专属 UI 由 workflow viewer 后续接入 |
