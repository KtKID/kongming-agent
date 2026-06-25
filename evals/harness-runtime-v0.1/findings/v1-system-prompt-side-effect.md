# v1 Finding：被测 agent system prompt 在算术题上的副作用

> 评测日期：2026-06-24 / 06-25
> 套件：`evals/harness-runtime-v0.1`
> 涉及环境：`minimax-baseline-ci`、`minimax-full-ci`
> 涉及题目：`short_answer_001`

## 摘要

**一句话**：`full` profile 多出来的那条 "你是 Kongming Harness Eval 的被测 agent，需要使用工具时必须通过真实 tool_call 调用，不要伪造工具结果。" system prompt，让 MiniMax-M3 在面对纯算术题时把 `output_tokens` 砍到 1，直接 emit 一个数字 token 就 stop——跳过链式推理，导致**稳定**答错。`baseline-min` 无该 prompt 时，同模型同题答对。

**为什么是 harness 信号而非模型问题**：唯一变量是 system prompt（profile 切换），模型本身不变；两次重跑 `full` 都失败、输出 token 形态完全一致（`output_tokens=1, reasoning_chars=0`），排除了"采样方差"假设。

## 实验设置

| 维度 | minimax-baseline-ci | minimax-full-ci |
|---|---|---|
| 模型 | MiniMax-M3 (reasoning_effort=high) | 同 |
| LLM preset | minimax-m3 | 同 |
| Approval | auto_allow | 同 |
| Tools | eval fake tools (5 个) | 同 |
| **system instructions** | `""`（空） | `"你是 Kongming Harness Eval 的被测 agent。需要使用工具时必须通过真实 tool_call 调用，不要伪造工具结果。"` |
| **MessageCompactor** | NoopCompactor（原样透传） | 默认 HistoryCompactor |
| **Session backend** | InMemorySession | 文件 session + SessionBootstrap |

题目 `short_answer_001`：「队列 7 任务，每轮执行 2 + 新增 1，第 4 轮后剩几个？只输出一个整数。」正确答案 `3`。

## 数据：三次跑 trajectory 对比

| run_id | profile | output | output_tokens | reasoning_chars | tool_call_count | finish_reason | content_chars | turn_count |
|---|---|---|---:|---:|---:|---|---:|---:|
| `v1-minimax-baseline` | baseline-min | **`3`** ✅ | 2 | 0 | 0 | stop | 1 | 1 |
| `v1-minimax-full` | full | `11` ❌ | **1** | 0 | 0 | stop | 1 | 1 |
| `v2-minimax-full` | full | `4` ❌ | **1** | 0 | 0 | stop | 1 | 1 |

**两次失败的关键证据**：输出数字不同（11 / 4）但 token 形态完全一致（`output_tokens=1, content_chars=1, reasoning_chars=0`）——证明这不是"采样导致偶尔答错"，而是"模型进入了某种确定性的 1-token 拍答路径"。

## 归因（到 trajectory 第 K 步）

- 在 `llm.request` 事件（trajectory `events[2]`）：full 的 `messages[]` 长度 = 2（system + user），baseline 长度 = 1（仅 user）；唯一多出的 system message 内容如上。
- 在 `llm.stream.end` 事件（`events[5]`）：full 两次跑都直接 `content_chars=1, reasoning_chars=0, finish_reason=stop`；baseline `content_chars=1, reasoning_chars=0` 但实际答案对。
- 在 `usage` 事件（`events[7]`）：full `output_tokens=1`，baseline `output_tokens=2`（数字 + 换行符）。

**机制假设**：「被测 agent + 通过 tool_call 调用」这段角色 / 工具暗示，让 MiniMax-M3 把这道纯算术题分类为"无需工具、应秒答"的题型，触发某种"省 token / 快速回答"路径，绕过了 high reasoning_effort 应该激发的 CoT。

## 反驳"采样方差"

| 假设 | 是否成立 | 理由 |
|---|---|---|
| 模型不会做这道题 | ❌ | baseline 同模型同题答对 |
| 采样 temperature 偶尔偏离 | ❌ | 两次 full 都错；如果是采样方差，应该至少一次接近正确 |
| 题目本身有歧义 | ❌ | baseline 输出 `3` 符合期望，证明题目语义清晰 |
| Reasoning 没启用 | ❌ | 两次都 `reasoning_chars=0`，但 baseline 也是 0 却答对——说明这道题在合适 prompt 下根本不需要显式 reasoning chunk |
| **System prompt 触发"省 token 拍答模式"** | ✅ | 唯一变量 + 两次输出形态完全一致 |

## harness 设计教训

1. **"被测 agent" 类角色暗示在某些模型上有反直觉副作用**：原意是引导模型把这是评测、要真的调工具；实际效果是让模型把简单题误判为"应秒答"，反而跳过推理。
2. **`output_tokens=1` + `reasoning_chars=0` + `tool_call_count=0` 是"模型在偷懒"的可观测指纹**：harness 可以把这个组合作为 trajectory 告警信号——尤其在 short_answer / 算术 / 推理类题上出现时，提示 prompt 可能在阻碍模型，而非帮助。
3. **system prompt 不是"加了总没坏处"**：JD 里 "对模型行为有品味" 的具体形态之一，就是知道哪些 prompt 在哪些题型上会变成负向引导。
4. **评测维度建议**：未来题集应有 1-2 道"显式不需要工具、依赖纯推理"的题，作为 system prompt 副作用的 canary。

## 未解决问题（未来工作）

- 这个 1-token 拍答行为在 DeepSeek / 其他模型上是否出现？需要换 preset 验证。
- 把 system prompt 拆成 (a) 只有"被测 agent" 角色，(b) 只有"通过 tool_call 调用"工具暗示，(c) 两段都有——三组对照可以定位真正的触发字。
- `repo_fix_regression_001` 在 v1 跑碰到了 minimax server stream 中断（`Server disconnected without sending a response`），v2 通过——是网络偶发不是 harness 缺陷。但 Runner.run 缺 stream-retry 是已知缺口，未来工作。

## 引用

- yaml 已固化字段：[evals/harness-runtime-v0.1/tasks/short_answer_001.yaml](../tasks/short_answer_001.yaml) `known_failure_modes`
- 完整 trajectory：
  - `evals/harness-runtime-v0.1/runs/v1-minimax-baseline/tasks/short_answer_001/trajectory.json`
  - `evals/harness-runtime-v0.1/runs/v1-minimax-full/tasks/short_answer_001/trajectory.json`
  - `evals/harness-runtime-v0.1/runs/v2-minimax-full/tasks/short_answer_001/trajectory.json`
