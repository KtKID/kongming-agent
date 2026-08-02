# Harness Runtime Eval v0.1

Kongming Agent runtime 级评测集。所有题目都在真实 `SessionEngine + Runner` 闭环内运行，每题独立 `KONGMING_HOME`、独立 session 落盘，确保结果可重复、可追溯。

## 题目结构

`tasks/*.yaml` 共 12 题 / 7 类别：

| 类别 | 题数 | 评分类型 | 说明 |
|------|------|---------|------|
| instruction_following | 1 | `json` | JSON 字段和值精确匹配 |
| short_answer | 1 | `exact_text` | 短答案精确匹配 |
| coding | 2 | `python_code` | 生成 Python 代码并跑 pytest |
| repo_fix | 2 | `swebench_diff` | SWE-bench 风格：模型产出 unified diff → `git apply` → `fail_to_pass` + `pass_to_pass` 双测试裁决 |
| tool_execution | 2 | `tool_execution` | 模型真调 builtin tool（search_code / read_file / list_mcp_servers / list_mcp_tools / call_mcp_tool），scorer 检查 runtime event 流和最终文本 |
| long_context | 1 | `json` | 长上下文检索：JSON 答案 + 引用检查 |
| tau_tool_state | 3 | `tool_state` | τ-bench 风格：user simulator 多轮对话 + 工具状态机裁决（policy refuse / state cancel / state return），全库 SHA-256 hash 比对（2026-06-25 起） |

### `swebench_diff` 字段约定

- `base_files`：base commit 的初始仓库内容（模型可见），harness 建临时 git 仓库并 commit；
- `test_files`：评测方持有的测试（模型不可见），写入后参与裁决；
- `fail_to_pass`：修复后必须由失败转通过的 pytest node id；
- `pass_to_pass`：修复必须始终保持通过的回归保护测试 node id，至少声明 1 个；
- `fixture_response`：标杆 unified diff，供 fixture 模式驱动 harness，可保留模型原始输出视角的 code fence。

打分流程：①基线校验（未打补丁时 `fail_to_pass` 必须失败、`pass_to_pass` 必须通过，否则 case 非法）→ ②普通 `git apply` 模型 diff（禁用 `--3way` / fuzzy fallback，并拒绝逃逸路径、`.git` 路径、rename/copy/binary patch 与逃逸 symlink）→ ③复跑两组测试，`fail_to_pass` 全转通过且 `pass_to_pass` 不退化才判通过。

### `tool_execution` 字段约定

- `expected_calls`：每项 `{name, arguments_contains?}`，scorer 顺序遍历 runtime 捕获的 `tool.call.end` 事件，确保按声明顺序全部命中；
- `arguments_contains`：递归子集匹配 tool 调用入参；字符串按大小写无关子串匹配，dict/list 按期望子集匹配；
- `final_contains`：最后一次 assistant 文本必须包含的关键词集合；
- `min_turns`：最小 runner turn 数下限，防止模型短路只回文本。

## 运行

唯一入口：`scripts/run_kongming_harness_eval.py`。脚本通过 `SessionEngine.build(...) + Runner.run(...)` 跑真实闭环，每题独立 session 落盘。

### 环境预设（推荐）

环境预设定义在 `environments.yaml`。日常运行只需要选择 environment id：

```bash
uv run python scripts/run_kongming_harness_eval.py --environment fixture-full
uv run python scripts/run_kongming_harness_eval.py --environment fixture-baseline
uv run python scripts/run_kongming_harness_eval.py --environment minimax-full-ci
```

当前内置预设：

| environment id | 模式 | profile | LLM preset | 用途 |
|------|------|------|------|------|
| `fixture-full` | `fixture` | `full` | — | 验证 Runner 请求链路、工具闭环、file session、metadata 和报告 |
| `fixture-baseline` | `fixture` | `baseline-min` | — | 验证空 instructions、memory session、Noop compactor 的最小切片 |
| `minimax-full-ci` | `preset` | `full` | `minimax-m3` | CI / nightly 真实模型评测 |

其他 Python 脚本可以直接复用 API：

```python
import asyncio

from scripts.run_kongming_harness_eval import run_harness_environment

summary = asyncio.run(run_harness_environment("fixture-full"))
print(summary["run_dir"])
```

### fixture 模式（默认）

用题目自带 `fixture_response` / 期望 tool_calls 驱动内置伪 LLM provider，用于验证 Runner 请求链路、session 落盘和 scorer，跳过真实模型网络调用。

fixture 模式的验证边界：

- `tool_execution` 题会通过真实 Runner 产生 tool_call、执行 eval fake tools、回填 tool_result，并进入第二轮 LLM；
- 其他题型使用伪 LLM 的确定性 `LLMResponse` 驱动 `SessionEngine.run()`，验证 request/response 路径、session 落盘和 scorer 语义；
- eval fake tools 由独立 `ToolRegistry` 提供，替换本评测题所需工具名的生产实现，保证 fixture 结果可重复。

### 执行边界

本 eval harness 面向本地可信评测任务和可信运行环境。`python_code` 会把模型生成代码写入 sandbox 后运行 pytest；`swebench_diff` 会在临时 git 仓库里应用模型 diff 并运行 pytest。脚本会拒绝逃逸 sandbox 的 diff 路径、`.git` 路径、rename/copy/binary patch 和逃逸 symlink；pytest 子进程的 `PYTHONPATH`、`HOME`、`TMPDIR`、`TEMP`、`TMP` 指向 sandbox，透传宿主 `PATH` / locale 相关变量，过滤宿主 `PYTHONPATH`、`KONGMING_HOME` 和 API key，并禁用 pytest 插件自动加载。这不是完整 OS 级安全沙箱。不要把未信任的题集、模型输出或第三方 fixture 放到有敏感文件的宿主上执行。

```bash
uv run python scripts/run_kongming_harness_eval.py
# 等价于：
uv run python scripts/run_kongming_harness_eval.py \
  --environment fixture-full
```

### preset 模式（真实模型）

preset 由 `config/model-providers.yaml` catalog 维护（内置 minimax/glm/deepseek 等），
eval resolver 统一通过 `ModelCatalogManager` 解析；用户自定义 preset 放入
`<KONGMING_HOME>/model-providers.yaml`。需在 env 里放好对应 `*_API_KEY`：

```bash
uv run python scripts/run_kongming_harness_eval.py \
  --environment minimax-full-ci
```

### 常用参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--environment` | — | `environments.yaml` 中的 environment id，推荐主入口 |
| `--environment-config` | `evals/harness-runtime-v0.1/environments.yaml` | environment 配置文件路径 |
| `--suite` | environment 值 | 迁移期覆盖：题集目录（含 `tasks/*.yaml`） |
| `--mode` | environment 值 | 迁移期覆盖：fixture 运行模式 |
| `--preset` / `--llm` | environment 值 | 覆盖 model catalog preset ID |
| `--config` | `config/setting.yaml` | Kongming 配置路径 |
| `--profile` | environment 值 | 迁移期覆盖：`baseline-min` / `full` |
| `--approval-mode` | environment 值 | 迁移期覆盖：`auto_allow` / `interactive` / `case` |
| `--max-turns` | environment 值 | 迁移期覆盖：runner 最大 turn 数 |
| `--run-id` | UTC 时间戳 | 本次运行 id（决定输出目录名） |
| `--output-dir` | environment 值 | 迁移期覆盖：输出根目录 |

## 产物

每次运行写入 `<output-dir>/<run-id>/`：

```
<run-id>/
  summary.json          # 总分 / 通过数 / 分类得分 / metrics（全 run token+成本汇总）
  tasks.json            # 每题打分明细，每题含 metrics（token + per_trial + 可选 cost）
  report.md             # 中文 Markdown 报告（展示入口，含 environment/profile/approval 元数据 + 成本与轮数段）
  tasks/<task_id>/
    trajectory.json     # 完整 runtime event 流 + score 详情
  sessions/<session_id>/
    manifest.json
    system_prompt.json
    <session_id>.jsonl  # file-backed session 真实回放
```

排查失败题时优先看 `tasks/<task_id>/trajectory.json` 的 `runtime.metadata`、`events` 和 `score.details`，再对照 session JSONL 确认 message 序列。metadata 记录 environment id、配置 hash、profile、approval、session、compactor、runner max turns 和密钥存在状态；密钥值保持在进程环境中。

## 金额消耗（metrics）

每次 run 会采集 token 用量并落盘成本账。数据源是 core Runner 每次 LLM 响应 emit 的 `kind=="usage"` 事件（透传 provider 返回的 `LLMResponse.usage`），由 `evals/src/metrics.py` 做三层聚合：单次 LLM 调用 → 单题（跨 repeat trial）→ 整套 run。结果写入 `summary.json` / `tasks.json` 的 `metrics` 字段，并在 `report.md` 渲染「成本与轮数」段。

### token 口径

`metrics.py` 归一化成统一口径的 6 个 key（兼容 anthropic / openai 两套原始字段名）：

| key | 含义 | 来源字段 |
|-----|------|---------|
| `prompt` | 提交总量 | anthropic `input_tokens + cache_read + cache_creation`；openai `prompt_tokens` |
| `uncached_prompt` | 未命中缓存的 prompt | anthropic `input_tokens`；openai `prompt_tokens - cached_tokens` |
| `cache_read` | 命中缓存 | anthropic `cache_read_input_tokens`；openai `cached_tokens` |
| `cache_write` | 写入缓存 | anthropic `cache_creation_input_tokens` |
| `completion` | 输出 | `completion_tokens` / `output_tokens` |
| `total` | 合计 | `total_tokens`，缺失时按 prompt + completion 补齐 |

聚合口径：token / LLM 调用数 / 耗时为跨 trial **总和**（本题 / 本 run 的总花费），轮数报均值与最大值，每题保留 `per_trial` 逐 trial 明细供方差分析。

### pricing 块（可选）

在任一 environment 下按需配 `pricing` 块即可换算金额；**不配置则只报 token 量，不臆造单价**（report 显示「未配置 pricing，仅报 token 量」）：

```yaml
environments:
  minimax-full-ci:
    # ... 其他字段 ...
    pricing:
      currency: USD              # 必填：币种标识
      input_per_mtok: 0.30       # 必填：未命中 prompt 单价（每百万 token）
      output_per_mtok: 1.20      # 必填：completion 单价
      cache_read_per_mtok: 0.06  # 可选：缺省 = input_per_mtok（保守按无折扣口径）
      cache_write_per_mtok: 0.30 # 可选：缺省 = input_per_mtok
```

单价以 provider 官方计费页为准。`_normalized_pricing` 在解析时校验：currency 必须非空字符串，input/output 单价必填且非负，cache 读/写单价缺省回落到 input 单价（即"无缓存折扣"的保守口径）。

### 成本计算

配了 pricing 时，成本按四个 bucket × 每 MTok 单价换算，单位 `USD`：

```
cost = uncached_prompt × input_per_mtok / 1M
     + cache_read       × cache_read_per_mtok / 1M
     + cache_write      × cache_write_per_mtok / 1M
     + completion       × output_per_mtok / 1M
```

注意计费用的是 `uncached_prompt`（未命中量）而不是 `prompt`（总量），避免对缓存命中的部分重复计费。`report.md` 的「成本与轮数」段会显示：LLM 调用数 / 总轮数 / 总耗时、token 总量（分 bucket）、缓存命中率、估算成本，以及每题明细表（含成本列，仅当配了 pricing 时出现）。

### 真实样例

以 `tau_state_return_001`（minimax-m3，repeat=4，11 次 LLM 调用，全通过）为例，配 MiniMax-M3 标准档单价（input $0.30 / cache_read $0.06 / output $1.20 per MTok）：

```
prompt 7574（全是 cache_read，命中率 100%）+ completion 970
→ cost = 7574 × 0.06/1M + 970 × 1.20/1M
       = 0.000454 + 0.001164 = 0.001618 USD
```

report.md 渲染：

```
- LLM 调用：`11` 次，总轮数：`11`，总耗时：`22576 ms`
- Token 总量：prompt `7574`（未命中 `0` / cache 读 `7574` / cache 写 `0`），completion `970`
- 缓存命中率：`100.0%`
- 估算成本：`0.001618 USD`

| 任务 | 轮数(均) | LLM 调用 | prompt | cache读 | cache写 | completion | 成本 |
| `tau_state_return_001` | 2.75 | 11 | 7574 | 7574 | 0 | 970 | 0.001618 USD |
```

### 数据准确性边界

metrics 忠实记录 provider 返回的 usage 值——它收到什么就记什么。若某次 run 出现 `input_tokens=0` 但 `completion>0`，通常是 **provider 兼容端点在该请求形态下未上报 input token**（provider transport 层问题），不是 metrics 计算错误。排查这类问题用 `tests/e2e/test_*_raw_usage_live.py`（设 `KONGMING_E2E_REAL_MODEL=1`）抓 raw SSE / trace usage / session usage 三层做一致性比对。fixture 模式的伪 LLM usage 为空 dict，token 恒为 0、无 cost，只用于验证 metrics 落盘与渲染链路。

## 真实工程回归题（regression）

> 状态：规划中，题目尚未入库。

### 目录组织

真题与构造题**物理隔离**，各自独立 suite 目录，真题不放入 `harness-runtime-v0.1`：

```
evals/
├── harness-runtime-v0.1/   # 构造题：题面自造，测通用 agent 能力
│   └── tasks/*.yaml
├── regression-v0.1/        # 真题：取自项目历史 bug / 真实 git commit
│   ├── PROVENANCE.md       # 每题登记 source_fix_report / buggy_commit / fix_commit
│   ├── environments.yaml   # 独立环境预设（复用同一套 fixture/preset 模式）
│   └── tasks/*.yaml        # 每题 YAML 内嵌 PROVENANCE 字段
└── src/                    # scorer / runner / loader 两侧共享，不重复实现
```

隔离理由：

- **PROVENANCE 语义不同**：真题带真实 git commit 溯源，构造题题面自造
- **生命周期不同**：真题随项目演进增删（bug 修复后归档），构造题长期稳定
- **base 校验更严**：真题的 `base_files` 必须能由登记的 `buggy_commit` 还原，构造题无此约束
- **防污染**：真题的 buggy 代码和历史测试 fixture 不混入构造题集，避免误用或误改

`evals/src/` 的 scorer / runner / loader 由两侧 suite 共享；新增 scorer（题型 B/D/E）也只落 `evals/src/`，不在任一 suite 目录内复制。

### 动机

现有 7 类题型测的是通用 agent 能力（指令遵循、工具调用、代码生成），测不出"在本代码库上的真实工程能力"。项目积累的已修复 bug、P0 事故、架构重构是可验证的天然素材，而且能覆盖通用 benchmark 测不到的项目专属能力面：安全决策链、Web 协议双侧同步、import 契约边界。

### 素材地图

| 素材源 | 路径 | 信息密度 | 最适合的题型 |
|------|------|------|------|
| 结构化 fix report | `reports/fix/*.md` | ⭐⭐⭐⭐⭐ 五段式（Bug/根因/方案/文件/测试） | repo_fix、根因分析 |
| P0/P1 事故 | `reports/p0-*.md`、`reports/bug/*.md` | ⭐⭐⭐⭐⭐ 含完整攻击链/复现步骤 | 安全决策、并发隔离 |
| git 历史 | 单文件多 commit | ⭐⭐⭐⭐⭐ 能精确还原 buggy 源码 | repo_fix 的 `base_files` 真源 |
| CR 报告 | `reports/cr/*.md` | ⭐⭐⭐⭐ 含 spec 合规清单 + 测试矩阵 | 测试补全、合规审计 |
| dev-pipeline tasks | `dev-pipeline/tasks/*/README.md` | ⭐⭐⳾⳾ 需求描述，无 bug 现场 | 需求→实现 |

### 取回 buggy 版本的三种手段

1. **git 历史精确还原（首选）**。fix report 定位到修复 commit 后，父 commit 即 buggy 版本：

   ```bash
   git show <fix_commit>^:src/safety/grant_store.py   # buggy 源码 → base_files
   git show <fix_commit>:tests/unit/test_*.py          # 回归测试 → fail_to_pass
   ```

   直接喂现有 `swebench_diff` scorer，零新增基础设施。

2. **fix report 反推重建**。文件被重构抹平、路径已变时，按 report 根因重建一个最小复现 repo（例如 `docs/fixes/20260421-provider-404-url` 的 URL 拼接 bug 可用 30 行代码独立重建）。

3. **spec + git 双源还原**。架构重构题用 git 取老实现，用对应 task README 取迁移目标（例如 `parallel-strategy-migration-v0.1`）。

### 题型设计

| 题型 | 说明 | 复用 scorer | 状态 |
|------|------|------|------|
| A. repo_fix | SWE-bench 风格：git 还原 buggy 代码 + 回归测试，模型产 unified diff | `swebench_diff`（现有） | MVP 首批 |
| B. 根因诊断 | 给 buggy 代码 + trace 片段，输出诊断报告（不改代码） | `diagnosis`（新增） | 规划 |
| C. 安全决策推理 | 给安全链 + 规则 + 具体命令，推 outcome / matched resolver / event | `json`（现有） | 规划 |
| D. 架构重构 | 给老实现 + 迁移目标，产出新模块边界与 import 关系 | `architecture_lint`（新增，调 `make lint-imports`） | 规划 |
| E. 协议漂移检测 | 给 Python `src/hosts/web/protocol/` 新增帧，补 `web/src/protocol.ts` 对侧 | `protocol_parity`（新增，调 `tsc`） | 规划 |

题型 D 的 scorer 直接复用仓库既有的 `lint-imports` 架构边界守卫，是通用 benchmark 不具备的差异化能力题。

### MVP 落地路径

先做 3 道 repo_fix 题（题型 A，零新增 scorer），验证"取材 → 出题 → 跑通"全链路：

1. `grant-session-scoping`（并发隔离，素材 `reports/bug/bug-report-20260427-235232-grant-session-scoping.md`）
2. `provider-404-url`（URL 拼接，素材 `docs/fixes/20260421-provider-404-url/`）
3. `p0-safety-rm-bypass`（安全，素材 `reports/p0-safety-rm-bypass-2026-05-08.md`）

每题在 YAML 内登记 PROVENANCE：`source_fix_report` / `buggy_commit` / `fix_commit`，保证可追溯、可重建。跑通后向题型 C（纯文本安全题，可大规模铺）和题型 B（诊断题）扩展。

规划新增 `evals/regression-v0.1/` 独立 suite 承载真题（与 `harness-runtime-v0.1` 物理隔离，见上节「目录组织」），维护自己的 `environments.yaml` 但复用同一套 fixture/preset 预设模式，通过 `scripts/run_kongming_harness_eval.py --environment-config evals/regression-v0.1/environments.yaml` 入口运行，无需改 runner 与 scorer 代码。
