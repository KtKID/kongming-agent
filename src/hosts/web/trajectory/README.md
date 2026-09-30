# trajectory · 轨迹时间线模块（track-timeline-v0.1）

基于每个 thread 的**双源数据**做只读 Trajectory Projection，输出
`GET /api/threads/{thread_id}/trajectory`。不动 Runner、不新增事件
kind、不做实时推送（实时轨迹留待 v0.2 的 `trajectory.event` WS 帧，
`seq` = trace 行号即断点续传坐标）。

## 双源设计（v0.1.1 修正）

trace 事件里的消息是**脱敏摘要**（`llm.request` 只带 `message_roles` /
`message_count`，`llm.response.message` 只带 `content_chars` /
`tool_call_count`——防爆磁盘），正文只在 session 真源：

| 数据 | 真源文件 |
|---|---|
| 时序 / 耗时 / token / TTFT / 工具入参出参 | `<tid>/trace.jsonl`（seq = 行号） |
| 用户输入全文 / assistant 回复与思考全文 | `<tid>/<tid>.jsonl`（manifest.format 真源，`record_type=message` 行） |
| 系统提示词全文 | `<tid>/system_prompt.json` |

runner 每个 `llm.response` 后按序 append 一条 assistant 消息，故
assistant 正文按出现顺序一一回填 llm 草稿；user 消息按序消费 run
（一次 submit = 一条 user + 一个 run）。session 来源记录的 seq 用负数
递减（system=-1，user 从 -2 起）保证前端 React key / 选中定位唯一。
**无 trace.jsonl 的老 thread 也能产出 system + user_input 记录**（只有
正文，没有时序观测）。

## 门户

`TrajectoryManager`（`manager.py`）——模块唯一对外入口：

- `async get_trajectory(thread_id) -> TrajectoryResponseDTO`：定位
  `<kongming_home>/sessions/<thread_id>/trace.jsonl` → 读文件（缺失 =
  空轨迹）→ 投影 → 回填 thread_id。
- `trace_path(thread_id) -> Path`：与 `hosts.web.run` 落盘路径同源。

thread 存在性判定在路由层（404），本模块不感知 thread 生命周期。

## 内部文件

| 文件 | 职责 |
|---|---|
| `projector.py` | `TrajectoryProjector.project(lines) -> TrajectoryResponseDTO` 纯投影器（零 IO）。两阶段：可变草稿累积（usage 可在 llm.response 后回填）→ EOF 统一物化 frozen DTO |
| `models.py` | 投影内部数据模型：`ParsedTraceEvent`（frozen 值对象）+ `RunDraft` / `LlmDraft` / `ToolDraft` / `UserInputDraft` / `CorruptedDraft`（可变累积草稿） |

## 投影规则（要点）

- `seq` = trace.jsonl 行号（0 起）；session 来源记录用负数递减 id；损坏行 →
  `kind=corrupted` 占位记录（保 seq，不中断）。
- run 分段：`run.start` → `run.end` / `run.cancelled`；`run.end` 无匹配 → `running`。
- 系统提示词：`system_prompt.json` → 账本最前一条 `system` 记录（seq=-1）。
- 用户输入：session jsonl 的 user 消息 → `user_input` 记录（按序消费 run，
  插入该 run 记录段头部；steer 追加输入 v0.1 不展示）。
- LLM 记录：`llm.request` → `llm.response` 区间；TTFT 取 `llm.chunk.first`
  的 `elapsed_ms`；token 取紧随的 `usage` 事件；回复与思考全文由 session
  的 assistant 消息按序回填（trace 只有 `content_chars` 摘要）。
- Tool 记录：`tool.call.start` ↔ `tool.call.end` 按 `call_id` 配对；入参取
  start payload `arguments`（依赖 task-tool-call-arguments 的 runner 修复）。
- 总览：总时长 = 首末事件跨度；状态 = 有 run 进行中则 running，否则末 run 状态。

## 允许 import 边界

- 对外：其他模块只 `from hosts.web.trajectory import TrajectoryManager`。
- 对内：本模块可 import `hosts.web.protocol`（DTO 真源）、
  `infrastructure.config`（路径解析 / Config）。禁止 import
  `hosts.web.threads` 等兄弟模块的内部实现。

## 测试

- `tests/unit/test_web_trajectory_projector.py`：投影单测（双 run 完整 /
  损坏占位 / 空 / 进行中 / 取消）。
- `tests/unit/test_web_routers_thread_trajectory.py`：路由契约（200 完整 /
  200 空 / 404 / 损坏 / 进行中 / 422），真实 create_app + 真实 Manager +
  临时目录真实落盘，只 stub `list_threads`。
- 协议漂移：`tests/unit/test_web_protocol_round_trip.py`（trajectory_response_dto）
  ↔ `web/src/protocol.trajectory.contract.test.ts` 双侧 fixture 对齐。
