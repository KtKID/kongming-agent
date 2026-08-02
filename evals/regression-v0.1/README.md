# Regression Eval v0.1

真题回归评测集。题目全部取自项目历史真实 bug / 架构重构，每题带 `source: real_commit:<sha>` 标记和 PROVENANCE 溯源。

与 `evals/harness-runtime-v0.1`（构造题）**物理隔离**：本 suite 只收真题，构造题不放入。

## 真题形态：最小可运行摘录

真题不是把整个项目代码塞进 sandbox，而是**摘出 bug 所在的最短链路**，剥离无关实现：

- 保留真实 buggy 机制（触发点、数据流、决策链结构）
- 剥离 provider / network / IO / session / 审计落盘等无关依赖
- base_files 内的源码是真实代码的精简骨架，让测试能在 swebench 沙箱真实跑起来

## 题目结构

`tasks/*.yaml`，每题字段：

| 字段 | 说明 |
|------|------|
| `id` | 题目 ID，`<domain>_<mechanism>_<seq>` 格式 |
| `category` | 题型分类（`arch_refactor` / `repo_fix` / `safety_logic` 等） |
| `source` | `real_commit:<fix_commit_sha>`，标记真题身份 |
| `provenance` | 溯源块：`source_fix_report` / `buggy_commit` / `fix_commit` / `bug_summary` / `fix_summary` / `extraction_note` |
| `prompt` | 题面：bug 现象 + 修复目标 + 当前代码 |
| `scoring` | `swebench_diff`（详见 `harness-runtime-v0.1/README.md`） |
| `fixture_response` | 标杆 unified diff，供 fixture 模式驱动 harness |

评分复用 `evals/src/scoring.py::_score_swebench_diff`，无新增 scorer。

## 运行

复用 `scripts/run_kongming_harness_eval.py` 入口，指定本 suite 的 environment 配置：

```bash
uv run python scripts/run_kongming_harness_eval.py \
  --environment-config evals/regression-v0.1/environments.yaml \
  --environment fixture-full
```

或直接用 `--suite` 覆盖：

```bash
uv run python scripts/run_kongming_harness_eval.py \
  --suite evals/regression-v0.1 \
  --mode fixture \
  --profile full
```

### 真模型评测（preset 模式）

真题面向真实模型能力评测。`swebench_diff` 类型的题目要求模型产出完整 unified diff，推理模型（如 MiniMax-M3）的 thinking 阶段会消耗大量 token，默认 `model.max_tokens: 4096` 远不够——模型会在 thinking 阶段耗尽预算，正文一个字都没输出。

**建议：真模型跑本 suite 时把 `max_tokens` 调到 `65536`（64k）**。这是真模型验证得出的经验值：

| max_tokens | 结果 | 说明 |
|---|---|---|
| 4096（默认） | ❌ thinking 耗尽，正文 0 字 | 推理模型 thinking 就用光了 |
| 16384 | ⚠️ 不稳定，有时刚好够、有时耗尽 | 边界值，不可靠 |
| 65536（推荐） | ✅ 稳定收敛，正文正常输出 | thinking 充足且能收敛 |
| 131072（128k） | ❌ 超时，thinking 不收敛 | 过大反而让模型"觉得有空间"无限思考 |

由于 eval runner 的 max_tokens 来自 config 文件（environment 不支持 model 级 override），需通过 `--config` 指向一个调大 max_tokens 的配置：

```bash
# 准备 config：基于 config/setting.yaml，把 model.max_tokens 改成 65536，并填入 preset
uv run python -c "
import yaml
with open('config/setting.yaml') as f: cfg = yaml.safe_load(f)
cfg['model']['max_tokens'] = 65536
cfg['web']['llm_presets'] = [{
    'id': 'minimax-m3-eval', 'provider': 'anthropic',
    'base_url': 'https://api.minimaxi.com/anthropic', 'model': 'MiniMax-M3',
    'api_key_env': 'MINIMAX_API_KEY', 'api_key_header': 'x-api-key',
    'reasoning_effort': 'low',
}]
open('/tmp/eval_config.yaml','w').write(yaml.dump(cfg, allow_unicode=True, sort_keys=False))
"

uv run python scripts/run_kongming_harness_eval.py \
  --preset minimax-m3-eval \
  --config /tmp/eval_config.yaml \
  --suite evals/regression-v0.1 \
  --output-dir evals/regression-v0.1/runs \
  --run-id <run-id>
```

注意 preset id 不能用 `minimax-m3` / `deepseek` 等内置名——config 迁移层会把它们当"历史退役 preset"清掉（见 `src/infrastructure/config/migrations.py::_RETIRED_PROVIDER_PRESETS`），换个后缀如 `minimax-m3-eval` 即可。

## 溯源

每题溯源见 [PROVENANCE.md](./PROVENANCE.md)。重建任一题目 buggy 版：

```bash
git show <buggy_commit>:<path>     # 取回 buggy 源码
git show <fix_commit>:<path>       # 取回修复后源码，校验 fixture_response
```

## 当前进度

| 题目 | category | buggy_commit | 状态 |
|------|----------|--------------|------|
| `workflow_parallel_self_contained_001` | `arch_refactor` | `97cf954d^` | ✅ 已落地，fixture + 真模型双重验证 |

### `workflow_parallel_self_contained_001` 详解

**真题来源**：commit `97cf954d`（refactor(workflow): parallel strategy 迁移为 self-contained，剥离 manager 反向依赖）。

**bug 机制**：`ParallelWorkflowStrategy` 在构造时接收并持有 `AgentWorkflowManager`（`self._manager`），`run()` 通过 `self._manager.run_parallel_specs()` 把整个执行体反向委托回 manager。这是 strategy → manager 的反向依赖，且 manager 残留了 parallel 专属方法（`run_parallel` / `run_parallel_specs`），其它策略都不在 manager 留下类型专属方法。

**修复目标**：依赖倒置——引入 `WorkflowRuntime` Protocol，`ParallelWorkflowStrategy` 不再持有 manager，所有引擎层能力经 `context.runtime` 借用。

**最小可运行摘录**：从真实 workflow 短链路（manager + parallel strategy + subagent）摘出 5 个文件，保留真实 buggy 机制，剥离 provider / network / IO / session / 审计落盘等无关实现：

| base_files | 说明 |
|---|---|
| `context.py` | `WorkflowExecutionContext` 骨架（真实代码精简，无 `runtime` 字段） |
| `subagents.py` | `SubAgentTask` / `SubAgentRun` / `SubAgentOutcome` 最小 dataclass |
| `description.py` | `WorkflowStrategyDescription` / `CatalogEntry` 骨架 |
| `strategies/parallel.py` | **保留真实 buggy 逻辑**（持有 `_manager`、`run()` 转调 `manager.run_parallel_specs`） |
| `manager.py` | `AgentWorkflowManager` 骨架（含遗留 `run_parallel` / `run_parallel_specs`） |

**评分**：复用 `swebench_diff` scorer。`fail_to_pass` 5 个测试断言 self-contained 重构完成（构造无 manager / 源码不引用 manager / context 有 runtime 字段 / run 用 context.runtime / manager 删遗留方法）；`pass_to_pass` 1 个测试保护 `describe()` 行为不变。

**验证结果**：

- **fixture 模式**：`passed=True score=1.0`（基线校验通过 + fixture diff 应用后 5 个 fail_to_pass 全转通过 + pass_to_pass 保持）。
- **真模型（MiniMax-M3，max_tokens=65536，reasoning=low）**：`status=completed`，模型正确理解题目方向——新增 `WorkflowRuntime` Protocol、context 加 `runtime` 字段、strategy 去掉 manager 持有、`run()` 改用 `context.runtime`、manager 删除遗留方法。**未通过 scorer**：模型产出的 unified diff 3 个 hunk 行数系统性少算（每个少 3 行），`git apply` 报 `corrupt patch`。这是真实的模型能力边界——理解架构重构但产不出格式严格合规的 patch，正是本题的区分度所在。
