# Harness Runtime Eval v0.1

Kongming Agent runtime 级评测集。所有题目都在真实 `NativeRuntime + Runner` 闭环内运行，每题独立 `KONGMING_HOME`、独立 session 落盘，确保结果可重复、可追溯。

## 题目结构

`tasks/*.yaml` 共 9 题 / 6 类别：

| 类别 | 题数 | 评分类型 | 说明 |
|------|------|---------|------|
| instruction_following | 1 | `json` | JSON 字段和值精确匹配 |
| short_answer | 1 | `exact_text` | 短答案精确匹配 |
| coding | 2 | `python_code` | 生成 Python 代码并跑 pytest |
| repo_fix | 2 | `swebench_diff` | SWE-bench 风格：模型产出 unified diff → `git apply` → `fail_to_pass` + `pass_to_pass` 双测试裁决 |
| tool_execution | 2 | `tool_execution` | 模型真调 builtin tool（search_code / read_file / list_mcp_servers / list_mcp_tools / call_mcp_tool），scorer 检查 runtime event 流和最终文本 |
| long_context | 1 | `json` | 长上下文检索：JSON 答案 + 引用检查 |

### `swebench_diff` 字段约定

- `base_files`：base commit 的初始仓库内容（模型可见），harness 建临时 git 仓库并 commit；
- `test_files`：评测方持有的测试（模型不可见），写入后参与裁决；
- `fail_to_pass`：修复后必须由失败转通过的 pytest node id；
- `pass_to_pass`：修复必须始终保持通过的回归保护测试 node id，至少声明 1 个；
- `fixture_response`：标杆 unified diff，供 fixture 模式驱动 harness，可保留模型原始输出视角的 code fence。

打分流程：①基线校验（未打补丁时 `fail_to_pass` 必须失败、`pass_to_pass` 必须通过，否则 case 非法）→ ②`git apply` 模型 diff（失败回退到 `git apply --3way`，保持严格上下文匹配）→ ③复跑两组测试，`fail_to_pass` 全转通过且 `pass_to_pass` 不退化才判通过。

### `tool_execution` 字段约定

- `expected_calls`：每项 `{name, arguments_contains?}`，scorer 顺序遍历 runtime 捕获的 `tool.call.end` 事件，确保按声明顺序全部命中；
- `arguments_contains`：递归子集匹配 tool 调用入参；字符串按大小写无关子串匹配，dict/list 按期望子集匹配；
- `final_contains`：最后一次 assistant 文本必须包含的关键词集合；
- `min_turns`：最小 runner turn 数下限，防止模型短路只回文本。

## 运行

唯一入口：`scripts/run_kongming_harness_eval.py`。脚本通过 `NativeRuntime.build(...) + Runner.run(...)` 跑真实闭环，每题独立 session 落盘。

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
- 其他题型使用伪 LLM 的确定性 `LLMResponse` 驱动 `NativeRuntime.run()`，验证 request/response 路径、session 落盘和 scorer 语义；
- eval fake tools 由独立 `ToolRegistry` 提供，替换本评测题所需工具名的生产实现，保证 fixture 结果可重复。

### 执行边界

本 eval harness 面向本地可信评测任务和可信运行环境。`python_code` 会把模型生成代码写入 sandbox 后运行 pytest；`swebench_diff` 会在临时 git 仓库里应用模型 diff 并运行 pytest。脚本会拒绝逃逸 sandbox 的 diff 路径，pytest 子进程只继承 sandbox `PYTHONPATH` 并禁用插件自动加载；这不是完整 OS 级安全沙箱。不要把未信任的题集、模型输出或第三方 fixture 放到有敏感文件的宿主上执行。

```bash
uv run python scripts/run_kongming_harness_eval.py
# 等价于：
uv run python scripts/run_kongming_harness_eval.py \
  --environment fixture-full
```

### preset 模式（真实模型）

用 `config/setting.yaml` 的 `web.llm_presets` 配置连真模型：

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
| `--preset` / `--llm` | environment 值 | 迁移期覆盖：`web.llm_presets` 中的 preset id |
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
  summary.json          # 总分 / 通过数 / 分类得分
  tasks.json            # 每题打分明细
  report.md             # 中文 Markdown 报告（展示入口，含 environment/profile/approval 元数据）
  tasks/<task_id>/
    trajectory.json     # 完整 runtime event 流 + score 详情
  sessions/<session_id>/
    manifest.json
    system_prompt.json
    <session_id>.jsonl  # file-backed session 真实回放
```

排查失败题时优先看 `tasks/<task_id>/trajectory.json` 的 `runtime.metadata`、`events` 和 `score.details`，再对照 session JSONL 确认 message 序列。metadata 记录 environment id、配置 hash、profile、approval、session、compactor、runner max turns 和密钥存在状态；密钥值保持在进程环境中。
