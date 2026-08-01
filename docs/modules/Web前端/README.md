# src/hosts/web/ + web/ — Web 前端

多 thread 聊天服务：FastAPI 后端 + React 前端 + WebSocket 实时通信。
用户通过浏览器与多个独立 thread 中的 LLM 对话；每个 thread 按 `backend_kind`
走两条路径之一：

- `generic_chat`（v0.1.5 默认）：每 thread 独立 ThreadCell（runtime + session + pending input），WS `/ws/threads/{id}`
- `claude_code`（v0.1.6 引入 + v0.2 完善）：走 Claude Agent SDK 子进程，WS `/ws/claude-code?thread_id=...`，thread metadata 持久化 SDK session 绑定，支持跨刷新历史回放 + 跨 cwd session 浏览导入

## 设计理念

| 决策 | 理由 |
|------|------|
| per-thread cell 隔离 | 每个 ThreadCell 持有独立 SessionEngine + HostDispatcher + pending input 状态；`PermissionsManager` 按顶层 thread_id 读取独立 allow/deny 本子。 |
| child 状态单一真源 | `GET /api/threads/{thread_id}/subagents` 经 `ThreadManager.get_cell()` → `HostDispatcher` → `AgentManager` 读取 `TaskRegistry` 不可变投影；pending/running/terminal 共用同一 task identity，未 boot thread 返回空列表。 |
| generic chat assistant 回复 fork | 完成态 assistant 气泡下方携带服务端 `history_index` 的分叉图标；`POST /api/threads/{id}/fork` 在源 thread 无 active run、pending approval、pending input 与 send-now claim 时通过 `SessionEngine.read_session_history` 取得历史前缀；`ThreadManager.fork_thread` 校验目标为无 `tool_calls` 的 assistant，并校验此前 tool request/result 全部配对，再通过 `seed_empty_session_history` 深拷贝结构化 Message 与引用附件；`forked_from_id` 记录直接父 thread，新 thread 使用独立 permissions 与 run count。 |
| generic chat 历史门户 | Web 首条消息走 `append_session_message`，WS history frame 走 `read_session_history`，fork 播种与失败补偿走 `seed_empty_session_history` / `clear_session_history`；Session factory、缓存和 raw Session 由 `SessionEngine` 独立维护。 |
| WS 长连接 + REST 辅助 | 流式输出走 WS 双向帧；thread CRUD / auth 走 REST，职责清晰 |
| TimelineStore 有界流式提交 | `ChatTimelineStore` 按 `(threadId, runId, turnId)` 暂存 delta；前台以 50ms + rAF 提交、后台以 100ms timer 提交，terminal / tool / history 原子 flush，per-turn 与 Store 总量硬上限兜底 |
| thread status 单一 owner | `ThreadStatusManager` 独占 active map、全局 sequence、per-thread run generation 和每连接 bounded queue；连接首帧为 active snapshot，snapshot/增量/approval 共用单连接 writer，旧 run lease 无法清除新 run。 |
| 发送态与运行态分离 | `threadDispatch` 只保存 transport 的 dispatching/error；`threadStatus` 只消费服务端 snapshot/delta。发送失败保留 Composer 草稿，服务端确认前不生成非 generic 用户气泡。 |
| 全局审批 inbox | generic_chat 审批由 `ApprovalManager` 持有 pending 状态，Web 通过 `approval.inbox.*` 展示和回写；卡片在提交中保留，失败回执恢复重试，只有 authoritative remove 或 snapshot omission 删除。 |
| generic_chat 长期记忆 | `run.py::_make_runtime_factory` 默认消费 `evolution.memory.enabled=true`：共享资产装配创建并加载 `MemoryStore`，将冻结快照交给 instruction loader，并把 `MemoryTool` 注册进共享 `ToolRegistry`；显式关闭时同步跳过 store、prompt source 和 tool |
| generic_chat 显式进化审查 | `app.state.evolution_manager` 是唯一状态 owner；`run.py` 通过 Manager 注册公开 `request_evolution_review` 与私有 `evolution_write`，主 runtime 只启用前者并安装 after-run lifecycle；scheduler runtime 过滤两者中的 lifecycle-bound 请求 Tool |
| generic_chat `/evolve` 控制命令 | Slash catalog 只对现有 generic_chat thread 展示 `/evolve`；`websocket/routes.py` 在 pending input 前消费命令，从当前 Session 构造证据窗口并启动 child reviewer；控制命令消费后按 `cell.current_run_task` 真相恢复 thread-status，主 run 空闲时广播 `idle`，主 run 活跃时保留其运行态 |
| 中间件栈 CSRF → Auth → Router | CSRF 最外层拦截跨站请求；Auth 验证 cookie；router 在最内层，保证所有 `/api/*` 路径鉴权 |
| idle eviction 后台扫描 | ThreadManager 周期扫描空闲 cell 并 evict，防止长期运行 server 内存泄漏 |
| app.py 零 sibling import | `src/hosts/web/app.py` 不 import `core` / `tools` / `executors` / `safety` / `host`，所有依赖通过 `runtime_factory` 闭包注入 |
| backend_kind 路由分流（v0.1.6）| ThreadMetadata 加 `backend_kind: generic_chat \| claude_code`；前端按字段路由到不同 ws endpoint + 不同渲染组件；通用路径完全保留 v0.1.5 行为 |
| thread ↔ sdk_session_id 1:1 绑定（v0.2）| thread metadata 持久化 sdk_session_id + cwd；写入后只读（`SdkSessionAlreadyBoundError`）+ 全局 1:1（`SdkSessionConflictError`）；删 thread 不动 SDK 自管的 jsonl |
| claude_code 历史回放（v0.2）| 后端 `jsonl_history.py` 解析 `~/.claude/projects/<encoded-cwd>/<sdk_session_id>.jsonl` 转 NormalizedMessage[]；前端 ClaudeCodeView mount 时一次性拉历史 + ws 接力新消息 |
| 跨 cwd session 浏览（v0.2）| 后端 `projects_scanner.py` 扫 `~/.claude/projects/` 出 projects/sessions 摘要；左栏 Tab「通用/Claude」切换；Claude tab 列树 + 点击 `POST /api/threads/import-claude-session` 创建新 thread 续聊（防重复绑定）|
| jsonl_history 不复用 normalizer | live SDK 流（dataclass 实例）跟持久化 jsonl（dict）字段相似但不直接互换；独立 parser 让两边各自演进，SDK 升级时风险隔离 |
| 审批状态按 thread 与 cwd 归属 | `PermissionsManager` 使用 schema v2 结构化规则；Shell allow 同时绑定顶层 thread 与 prepared effective cwd。Web 展示并原样回传冻结的 `RememberRule.scopeCwd`。 |
| 每 cwd 审批处置模式 | `app.py` 装配 `WebAutoApprovalManager`，`ApprovalRuntimeManager` 与 generic run 复用其 policy；聊天 Composer 提供 `user` / `llm` / `full_trust` 选择器。 |
| Web 审批运行时装配走单一门户 | `app.py` 通过 `ApprovalRuntimeManager` 装配共享 `PermissionsManager`、`ApprovalManager`、Inbox/Avatar sink 和 Claude bridge factory；app shell 保持零 safety/tools 直接依赖。 |
| 媒体上传子系统（uploads） | 图片上传 → 资产存储 → 审批消息组装三层分离；asset_id 强制 uuid4 hex 格式校验防 path traversal；MIME 白名单来自 `core.contracts.IMAGE_EXT_BY_MIME`，registry / validation 复用同一映射；预留 video/file 类型扩展点 |
| attachments 全链路透传 | websocket/routes.py → ThreadManager → HostDispatcher → SessionEngine.run → Runner → Message.metadata["attachments"] → AnthropicMediaAdapter；容错策略：图片有问题退化纯文本 + warning log + dropped_attachments audit trail，不抛错 |
| dashboard/config 配置管理（manage-config-tab） | facade 单一入口 `ConfigManager`：聚合 schema 元数据（139 字段 / 7 group）+ ruamel.yaml round-trip writer + detached restart；5 端点 `/api/manage/config/*` 统一 HTTP 翻译（409 lock / 422 validation / 503 restart）；前端 `web/src/modules/dashboard/config/` 同构 facade（`ConfigPage` + `api.ts`），按后端 group 渲染配置 section，list 字段只读 |
| 管理页插件开关 | `PluginManagementManager` 管理 MCP 工具 enabled bool；REST `/api/manage/plugins` 展示和更新单个工具；新 thread 创建时读取 bool 生成 `SessionEngine` 工具快照 |

## 核心流程

`generic_chat` 当前普通发送、排队、插队和接收侧下行见 [`message-flow-sequence.md`](message-flow-sequence.md)。

### WS 连接生命周期

```
Browser                              Server
  |-- WS /ws/threads/{id} ----------->| cookie 验证 + thread_id 校验
  |<-- ws.accept() ------------------|
  |<-- thread.history ---------------| 全量历史
  |<-- pending-input.snapshot --------| 队列快照
  |                                   |
  |== 入帧循环 ======================|
  |  /evolve ----------------------->| EvolutionManager.start_manual_command_review
  |                                  | → child reviewer（主 LLM 零调用）
  |<-- thread-status: idle ----------| 主 run 空闲；复盘状态由 evolution 卡片独立展示
  |  user.input --------------------->| ThreadManager.submit_user_input
  |                                  | → HostDispatcher.submit(QUEUE)
  |  pending-input.send-now -------->| ThreadManager.send_pending_input_now
  |                                  | → HostDispatcher.submit(IMMEDIATE)
  |  approval.inbox.resolve --------->| ApprovalManager.resolve
  |  ping --------------------------->| pong
  |                                   |
  |<-- content.delta / reasoning.delta| 流式文本
  |<-- turn.start / turn.end --------| 轮次标记
  |<-- tool.call.start / end --------| 工具调用
  |<-- approval.inbox.add/remove ----| 全局审批卡片
  |<-- approval.decision ------------| 审批结果通知
  |<-- error ------------------------| 错误横幅
  |<-- cell.evicted -----------------| cell 被回收（idle / manual / shutdown）
  |                                   |
|-- disconnect -------------------->| adapter.close()（不 evict，允许重连）
```

### Web 流式渲染背压（web-streaming-render-backpressure-v01）

`ChatManager -> ChatTimelineStore -> ChatRenderAdapter -> MessageList` 是唯一的实时渲染路径。`assistant_message_delta` 先进入 Store 私有 `pendingByTurn`，按 `TurnKey(threadId, runId, turnId)` 分离正文与 reasoning；前台最多 20 FPS，后台目标 10 FPS。每个 Store 同时仅有一个可取消 callback，terminal、工具开始、普通错误和 history 均先提交所有 pending，再在边界末尾通知一次；`llm_error` 丢弃失败 turn 的残缺 assistant，同时提交其它 turn 的 pending。

buffer 的硬预算为每 turn 256 events / 32 KiB、每 Store 32 turns / 2048 events / 256 KiB。达到任一预算时走 `emergency-size` 同步提交，优先保证最终文本与内存上界。流式 assistant 使用 React 转义的 `whitespace-pre-wrap` 纯文本和光标，完成态才用按 `text` memo 的 Markdown；delta 日志按 turn 每秒输出无正文摘要。故障形状的脱敏 1557-chunk fixture 位于 `web/src/chat/__fixtures__/streaming-render-oom-v01.json`，单测 warm-up 后连续三次走真实 ChatManager 主链。

### 管理页插件开关

插件页当前只展示 MCP 注册出来的工具。后端入口在 `src/hosts/web/plugin_management/`，状态文件为 `<kongming_home>/web/plugin-tools.json`；`GET /api/manage/plugins` 返回当前可展示工具，`PATCH /api/manage/plugins/{tool_id}` 只更新该工具的 `enabled` bool。

开关生效边界是新建 `SessionEngine`。`src/hosts/web/run.py` 的 runtime factory 先让 MCP 注册链路把真实 Tool 放入 `ToolRegistry`，再同步插件状态；创建新 thread/cell 时用 `PluginManagementManager.enabled_tool_names(...)` 过滤工具名。已经创建的 thread/cell 持有自身工具快照，继续按创建时状态运行。

Runner 发送 LLM request 时基于本 session 的 `resolved_tools` 生成 provider tools schema，不改写 latest user message；模型调用关闭或已卸载工具时，tool result 会返回“工具不可用”，让模型继续改用当前可用工具。

### claude_code 路径（v0.1.6 引入 / v0.2 完善）

```
浏览器 NewThreadDialog 选 "Claude Code"
  ↓ POST /api/threads { backend_kind: "claude_code" } → ThreadMetadata（sdk_session_id="" 占位）
浏览器 navigate /chat/{thread.id}
  ↓ Chat.tsx 看 backend_kind=claude_code → 渲染 <ClaudeCodeView>
  ↓ useClaudeCodeWS → WS /ws/claude-code?thread_id={id}（4 道校验：格式 / thread 存在 / backend_kind / sdk_session_id 可空）
  ↓ ClaudeCodeView 用户发首条消息
  ↓ claude-command → ClaudeCodeService.query → ClaudeAgentOptions(include_partial_messages=True)
  ↓ SDK 子进程 → 流出 SystemMessage(init, session_id=<UUID>) → service._consume 检测 →
       sessions.rename(thread_id → sdk_uuid) + thread_manager.bind_sdk_session(thread_id, sdk_uuid, cwd)
  ↓ stream_delta / text / tool_use / ... → normalizer → ws → ClaudeCodeView dispatch（14 kind）
  ↓ permission_request → broadcaster.emit_add → `approval.inbox.add` (S2C) → <ApprovalToastQueue /> 三按钮 → `approval.inbox.resolve` (C2S)
     （v0.1.6 模态审批弹窗已于 smart-approval-v2-inbox 退役，改为右下角全局浮窗；
      详见 dev-pipeline/tasks/smart-approval-v2-inbox/）

—— 刷新页面（v0.2 核心刚需）——
ClaudeCodeView mount 时 thread.sdk_session_id 非空
  ↓ GET /api/threads/{id}/claude_history → jsonl_history.parse_jsonl_history → NormalizedMessage[]
  ↓ items[] 一次性 push 历史，loading 态占位
  ↓ ws 接力新消息（不重复，因为 service.query 用 options.resume = sdk_session_id 续上下文）

—— 跨 cwd 浏览（v0.2）——
左栏 Tab 切 Claude → ClaudeProjectsTree
  ↓ GET /api/claude/projects → projects_scanner.list_projects → 项目折叠树（默认 10 条 / 显示更多）
  ↓ 点 session 卡片 → POST /api/threads/import-claude-session
       后端反查 sdk_session_id 已绑 → 跳现有 thread (imported=false)
       否则 create_thread + bind_sdk_session → 跳新 thread (imported=true)
  ↓ navigate /chat/{thread.id} → 同"刷新页面"路径回放历史
```

### tool_use 流式 pending 卡片（2026-05）

Anthropic SDK 在 `content_block_start(tool_use)` → `content_block_delta(input_json_delta) × N` → `content_block_stop` 序列里流式构建工具入参。早期实现把 `input_json_delta` 当成普通文本 delta 拼到 assistant 气泡（看起来像"模型说出 JSON"），并被 `content_block_stop` 切成多条 ChatItem。

现链路：

```
content_block_start(tool_use, id, name)
  ↓ normalizer.py → stream_status(phase=tool_calling, toolName, toolId)
ClaudeCodeView 创建 pending ToolItem
  (kind="tool", arguments={}, partialInput="", pending=true, ok=null, callId=toolId)

content_block_delta(input_json_delta, partial_json)
  ↓ normalizer.py → stream_delta(deltaType=input_json, content, toolId)
ClaudeCodeView 按 callId===toolId 找最近 pending → partialInput += content
  （toolId 缺失时取末尾 pending；找不到则静默丢弃）

AssistantMessage[ToolUse]（完整帧）
  ↓ normalizer.py → tool_use(toolName, toolInput, toolId)
ClaudeCodeView 按 toolId 匹配 pending → 原地 resolve
  arguments=toolInput, partialInput=undefined, pending=false
  （ToolCard 同 id 内更新，UI 不闪烁）
```

ToolCard 在 pending=true 时，折叠态显示工具名 + running badge；展开显示 partialInput（标"构建参数中…"）。pending=false 后展开切回现有 arguments 渲染。详见 [task/web-claude-input-json-pending-card](../../../dev-pipeline/tasks/web-claude-input-json-pending-card/README.md)。

### claude_code 通道 interrupt 链路（interrupt-claude-channel-v0.1）

generic_chat 通道的 interrupt（runner 顶层吞 CancelledError）见 `docs/modules/核心/README.md`。claude_code 通道走 Claude Agent SDK 子进程，**单靠 task.cancel() 打不断递归子 agent**（CLI 内部 Python 看不见）；必须用 SDK 原生 `ClaudeSDKClient.interrupt()` 通过 control_request 通知 CLI 子进程，触发 `QueryEngine.abortController.abort()` → AbortController 树级联打断主 agent + 所有 subagent + 子 agent 内 Bash subprocess。

```
[1] 用户点 Stop（ClaudeCodeView）
    isRunning = streamPhase !== "idle" || items.some(i => i.streaming)
    onInterrupt 300ms 节流 + isRunning gate
    → socket.send({type:"abort-session", sessionId: thread.claude_thread_id || threadId})
[2] WS /ws/claude-code 入帧 → route.py:409
    if msg_type == "abort-session": await service.abort(sid)
    （不再主动发 complete 帧 — interrupt-claude-channel-v0.1 删了重复 emit）
[3] ClaudeCodeService.abort()
    ├─ _lookup_client_for_abort(sid)：双 key 查（直接 + fallback 反查 _sessions._sessions[k].session_id）
    │    防 v0.2 rename race：_clients key 可能是 placeholder，_sessions.session_id 是 sdk_uuid
    ├─ asyncio.create_task(_safe_interrupt(client))  # fire-and-forget，防 60s control_request timeout 卡 ws
    │    └─ try/except await client.interrupt()      # SDK 路径：cc-agent-sdk client.py:250
    │         └─ stdin 写 {"subtype":"interrupt"} control_request → CLI 子进程
    │              └─ QueryEngine.interrupt() → abortController.abort() → 子 agent 树级联
    └─ await _sessions.request_abort(sid)            # task.cancel() 兜底
[4] CLI 子进程 abort 完 → 流出 complete(aborted=true) → _consume 单一来源 emit
[5] 前端 ClaudeCodeView 收 complete.aborted=true
    → 复用现有 case "complete" 切 streaming=false + setStreamPhase("idle")
    → isRunning 推导切回 false → Stop 按钮消失
```

**幂等保证**：
- 后端：`_safe_interrupt` 吞掉所有异常；`request_abort` 已幂等（task.done 后 cancel no-op）
- 前端：300ms 节流（lastInterruptAtRef）+ isRunning=false 时 onInterrupt 早 return

**兜底场景表**：

| 场景 | 仅 SDK interrupt 够吗 | task.cancel() 兜底解决 |
|---|---|---|
| `client.interrupt()` 自身 raise / control_request 60s timeout | ❌ | 强 cancel _consume，前端能收到 aborted |
| CLI 子进程僵尸（pipe still open 但子进程不响应）| ❌ | 同上 |
| `client.interrupt()` 成功但某 tool 不响应 abort signal | ❌ | _consume 不再等子进程 |
| `_clients[session_id]` 找不到（race / 已 disconnect）| —（跳过 SDK）| 仅靠 task.cancel |

**已知不修（留 follow-up）**：
- 多 tab fanout：claude_code 通道无 fanout，A tab 点 Stop → complete 帧只发到 `record.writer`（最后一次 replace_writer 的 ws），其他 tab 收不到
- abort 后 `_clients[sid]` 不 disconnect（复用），有 SDK 内部状态残留风险；当前测试覆盖了基本复用路径，real prod 风险靠 e2e 兜

**详见** [`dev-pipeline/tasks/interrupt-claude-channel-v0.1/`](../../../dev-pipeline/tasks/interrupt-claude-channel-v0.1/README.md)（含完整链路图、opus 评审 3 P0 必修说明、5 项 grep 结论）。

### 审批回写链路

```
工具调用触发审批
  ↓ SafetyDecisionEngine：DangerGuard → per-cwd disposition → thread permissions
  ↓ ConsentResolver 生成冻结 thread_id 的 remember candidate
  ↓ ApprovalManager.request(...)
InboxEventSink → ApprovalInboxBroadcaster.emit_add
  ↓
浏览器全局 ApprovalToastQueue 展示 canonical expression/displayText/scopeCwd
  ↓ 用户点击允许一次 / 允许并记住 / 拒绝一次 / 拒绝并记住
approval.inbox.resolve { requestId, allow, remember, rememberRule? }
  ↓
ApprovalManager 严格比对 pending candidate 并调用 PermissionsManager.write_entry
  ↓
approval.inbox.resolve_result { accepted, error? }
  ↓
规则原子写入 pending.thread_id 对应 schema v2 JSON snapshot
```

### 全局审批 inbox 与三模式流程

generic_chat 审批通过统一 Engine/Permissions Manager 后进入全局 inbox，Web UI 展示规则候选与写回状态：

```
generic_chat 工具调用
  ↓ DangerGuard
      ├─ HardBlock → rejected
      └─ elevated / destructive → 人工审批，关闭 remember 与快捷确认
  ↓ per-cwd disposition
      ├─ full_trust → 普通调用直接放行并高优先级审计
      ├─ llm → default:ask 模型复核，allow 后倒计时
      └─ user → PermissionsManager.resolve
          ├─ deny → 拒绝
          ├─ allow → 放行
          └─ 无命中 → 人工审批
  ↓ make_manager_prompt_fn(channel="generic_chat", thread_id, default_cwd)
ApprovalManager.request(...)
  ↓ InboxEventSink.to_inbox_payload
  ↓ ApprovalInboxBroadcaster.emit_add
  ↓ web/src/features/approval-inbox/ApprovalToastQueue
      ├─ danger=true → 强红、禁快捷键、无记住按钮
      ├─ rememberAllowed=true → 卡片展示命令前缀与 effective cwd
      ├─ rememberAllowed=false → 明示当前请求只支持单次审批
      └─ resolve_result.error → 保留 pending 并显示后端错误
```

### Avatar / XSpace 审批回写经验

Avatar 通道用于 XSpace 侧轮询 Kongming 的可展示消息，并把审批按钮动作回写到 Kongming：

```
工具调用触发审批
  ↓ ApprovalManager 生成 pending request_id
AvatarApprovalSink.emit_approval_required(pending)
  ↓ AvatarManager.register_message(source="approval", request_id=pending.request_id)
XSpace 轮询 /api/avatar/v1/messages?source=approval&status=active
  ↓ 用户点击 accept_once / accept_for_session / reject
POST /api/avatar/v1/approvals/{requestId}/resolve
  ↓ request.app.state.approval_manager.resolve(requestId, decision)
ApprovalManager 释放 pending，runner 继续或拒绝工具调用
```

沉淀规则：

- `requestId` / `callId` 统一指 ApprovalManager 的 pending request id；原始工具调用 id 放 `metadata.toolCallId`。
- `AvatarApprovalSink` 写入消息时补齐 `metadata.callId`、`metadata.requestId`、`metadata.toolCallId`、`metadata.runId`、`metadata.xspaceEventType`，方便 XSpace 按钮协议和日志排查。
- `POST /api/avatar/v1/approvals/{requestId}/resolve` 的 action 映射沿用当前 `ApprovalManager.resolve()` 合同：`accept_once` 生成单次 allow；`accept_for_session` 从 broadcaster 的 pending snapshot 取冻结 `rememberRule` 并一并提交；`reject` 生成单次 deny。
- Avatar router 通过 `request.app.state.approval_manager` 读取审批 Manager；`run.py` 装配 `ApprovalManager` 后负责挂载到 app.state。
- 测试分两层：sink 单测覆盖 pending → AvatarManager 消息注册；路由单测覆盖三态 action、`requestId/callId` 校验、pending 缺失 404、router 边界 import 守卫。完整工具触发审批链路需要单独 smoke / 集成测试覆盖。

### Claude Code 统一审批流程

```
ClaudeCodeWS 收 claude-command
  ↓ ClaudeCodeService.query → SDK 子进程
  ↓ SDK 发 permission_request（tool_name, command）
  ↓ ApprovalBridge.can_use_tool()
       ↓ SDK tool/input canonicalize 为 ApprovalRequest
       ↓ 与 generic_chat 共用 SafetyDecisionEngine + PermissionsManager
       ↓ ApprovalManager / InboxEventSink
       ↓ 通用 ApprovalDecision 映射为 SDK allow/deny
  ↓ 当前顶层 thread 的 JSON snapshot 持有 allow/deny 与 revision

auto-approval-set-mode/query → AutoApprovalStateFrame(mode, timeoutMs, ruleOverrides)
```

### 图片上传流程

```
浏览器 Composer 📎 按钮 / 图片粘贴
  ↓ POST /api/uploads/images（multipart: image + thread_id）
  ↓ MediaUploadValidator：MIME 白名单 → 5MB size 限制 → thread 存在性 → thread 归属
  ↓ AssetRegistry.register：uuid4 asset_id + sha256 + AttachmentAsset 构造
  ↓ AssetStorage.store：原子写 <base>/<kind>s/<thread_id>/<asset_id>.<ext>
  ↓ 返回 UserInputAttachment{asset_id, kind, mime_type, size_bytes, width, height}
  ↓ 前端拼 UserInputFrame{content, attachments: [UserInputAttachment]}
  ↓ WS 发送 → websocket/routes.py 提取 attachments → ThreadManager.submit_user_input
  ↓ claude_code 路径：AttachmentPrefixBuilder.build() → @<abs_path> CLI 前缀
  ↓ generic_chat 路径：HostDispatcher.submit(QUEUE, attachments) → SessionEngine.run → Message.metadata["attachments"]
```

### 模型服务商与 Composer 模型下拉

Web 侧模型选择分两层：服务商连接状态页按 provider 展示，Composer 模型下拉按 provider 下的 model preset 展示。

```
GET /api/model-providers/catalog
  ↓ app.state.model_catalog_manager.list_providers()
  ↓ 合并内置与用户 catalog
  ↓ 管理页展示 Minimax / GLM / DeepSeek 等服务商

POST /api/model-providers/{provider_id}/connect
  ↓ ConfigManager.write_env_values()
  ↓ 只写 KONGMING_HOME/.env 中的 provider key
  ↓ 不把内置 provider preset 写回 setting.yaml

GET /api/model-providers/model-families
  ↓ ModelCatalogManager.list_providers()
  ↓ credential 引用判断 provider connection state
  ↓ models[*] 直接投影 preset、reasoning capability 与 context window
  ↓ ConnectedModelFamilyDTO[]
  ↓ ModelSwitcher 按 familyId 渲染，点击提交 presetId
```

读取顺序与归属规则：

| 阶段 | 来源 | 规则 |
|------|------|------|
| 服务商列表 | `config/model-providers.yaml` | provider 级配置：`provider_id`、展示文案、默认 key env、endpoint/header、`models[*]` |
| 连接状态 | `KONGMING_HOME/.env` + 真实进程 env | `default_api_key_env` 命中即 connected；fallback env 命中时运行态同步到默认 env 名 |
| 模型列表 | 合并 catalog 的 provider `models[*]` | 内置与用户自定义模型使用同一结构；用户同 provider ID 完整替换 |
| 前端下拉 | `/api/model-providers/model-families` | 一个 `ConnectedModelFamilyDTO` 对应一个可选模型，点击后走 `/api/threads/{id}/preset` 更新 thread preset |

示例：`DEEPSEEK_API_KEY` 有值且 catalog 声明 `deepseek`、`deepseek-pro` 两个 `models[*]` 时，Composer 下拉展示两个 DeepSeek 模型。管理页仍只展示一个 DeepSeek provider 连接卡片。

## 代码索引

### 后端 `src/hosts/web/`

| 文件 | 导出 | 说明 |
|------|------|------|
| `app.py` | `create_app` | FastAPI app factory：中间件栈 + routers + WS + 静态服务 + lifespan；通过 Web Manager 门户注入统一审批运行时和 uploads 单例。 |
| `app.py` app state | `model_catalog_manager` | Web composition root 注入统一模型目录门户，provider/preset/thread 路由共享同一入口 |
| `app_support/auto_approval_manager.py` | `WebAutoApprovalManager` | Web per-cwd 模式与审计装配门户；`create_app` 绑定到 app state。 |
| `app_support/approval_runtime_manager.py` | `ApprovalRuntimeManager` | Web 审批 composition portal：构造共享 Permissions/pending Manager、Inbox/Avatar sink，并为 Claude route 创建复用同一安全链的 bridge。 |
| `app_support/thread_permissions_rest_manager.py` | `ThreadPermissionsRestManager` | REST 门户：把 schema v2 snapshot/CAS 替换投影为结构化 wire DTO，并把非法 scope、revision 冲突、迁移/存储失败映射为稳定 Web 错误。 |
| `run.py` | `main` / `_build_manager_and_inbox_sink` | `python -m hosts.web.run` 启动入口；generic_chat runtime 从 app.state 复用统一 Manager 实例图；通过 `EvolutionManager.register_runtime_tools()` 与 `enabled_tool_names()` 固定公开/私有 Tool 边界。 |
| `app_support/host_adapter.py` | `WebHostAdapter` | `HostAdapter` 的 WS 输出兼容实现；generic_chat 审批已迁到 ApprovalManager，adapter 不持有审批 pending 状态 |
| `threads/manager.py` | `ThreadManager` + channel binding errors | Thread fleet 单例：boot/evict/idle；`fork_thread` 在 assistant 回复边界复制 generic chat Session 前缀与附件，并校验工具请求/结果配对；per-thread mutation lock 串行化 metadata 创建、更新、删除，确认目标 `exists → absent` 后按显式 thread id 清理 permissions，失败进入可观测重试队列；启动保持其他宿主的权限本。 |
| `threads/cell.py` | `ThreadCell` / `ThreadCellStatus` | 单 thread 装配束：runtime + host_dispatcher + adapter + sinks + pending input 状态机 |
| `threads/metadata.py` | `ThreadMetadata` + CRUD 函数 | thread 元数据 JSON 持久化（`.kongming/web/threads/`）；schema v12 加 nullable `forked_from_id` 并从旧版本连续懒升级。 |
| `websocket/routes.py` | `register_ws_routes` | WS 端点 `/ws/threads/{id}`：入帧分发（user.input / pending-input.* / ping）；generic_chat `user.input` 提取 attachments / references 后进入 ThreadManager pending input queue，再由 HostDispatcher.submit 投递 |
| `websocket/event_sink.py` | `WSEventSink` | `EventSink` → S2C 帧翻译（14 种事件 → 14 种帧） |
| `auth.py` | `AuthMiddleware` / `CSRFMiddleware` + cookie 工具 | Cookie 鉴权 + CSRF 保护中间件 |
| `auth_secrets.py` | 密码 / secret 持久化 | bcrypt hash + session secret 落盘，首次后清 env |
| `rate_limit.py` | `LoginRateLimiter` | Per-IP 登录失败限流（默认 5 次 / 5 分钟） |
| `errors.py` | 7 个错误子类 + handler | 统一 KongmingWebError → ErrorResponseDTO JSON |
| `static.py` | `install_static` | SPA 静态文件服务 + history mode fallback |
| `ctl.py` | `main` / 子命令 start/stop/restart/status/log | Web 服务进程管理 CLI（替代 `web-ctl.sh`）。复用 `load_config` 读 port/host，`subprocess` + `socket` 管进程。调用方式：`python -m hosts.web.ctl start|stop|restart|status|log` |
| `app_support/startup_progress.py` | `StartupProgress` / `STARTUP_STEPS` | 启动进度上报：写 `.kongming/web/startup.json` 供 Tauri 轮询 |
| `threads/types.py` | `ThreadCellProtocol` / `ThreadManagerProtocol` | 测试 mock 用 typing.Protocol 接口；v0.2 加 `find_thread_by_sdk_session_id` + `bind_sdk_session` |
| `app_support/llm_protocol.py` | `NormalizedMessage` TypedDict + 15 `MessageKind` + `LLMProvider` Literal + 4 个 C2S 帧 TypedDict | claude_code 路径协议形态（与 generic_chat 的 `protocol/ws_frames.py` 平行）；v0.2 stream-progress 加 `stream_status` kind + `phase`/`blockIndex`/`deltaType`/`model` 字段 |
| `app_support/cron_delivery.py` | `WebDeliverySink` | cron 投递 sink（v0.3 M4）：通过 `CronWSBroker` 把 `cron.run.completed` 事件 broadcast 给 WS 订阅者；不落盘，仅实时推送 |
| `websocket/cron.py` | `CronWSBroker` + `get_broker` + WS `/ws/cron` 端点 | cron 全局 WS broker（v0.3 M4）：独立端点，不复用 thread WS；cookie 鉴权；单例连接池 + `asyncio.gather` 广播 |
| `websocket/thread_status_manager.py` | `ThreadStatusManager` + `ThreadStatusRunLease` | thread status 唯一状态 owner：active snapshot、sequence、run generation、stale lease 拒绝、per-connection bounded queue 和唯一 writer。 |
| `websocket/thread_status.py` | `ThreadStatusEventSink` + `get_thread_status_manager` + WS `/ws/thread-status` 端点 | generic/Claude/Codex producer 统一向 Manager 发布；连接先排 thread-status snapshot，再排 approval snapshot；status、approval、usage、pong 都经 Manager 单 writer 出站。 |
| `approvals/global_inbox/` | `ApprovalInboxBroadcaster` / `get_inbox_broadcaster` | 全局审批 inbox 路由器。维护 request snapshot 与 `approval.inbox.resolve` 路由目标；subscriber 的出站发送由 `ThreadStatusManager` 串行化。 |
| `avatar/approval_sink.py` | `AvatarApprovalSink` | ApprovalManager event sink，把 pending 审批投影为 Avatar registry 消息；`requestId/callId` 使用 pending request id，原始工具调用 id 写入 `metadata.toolCallId`。 |
| `routers/avatar.py` | `router` / `resolve_avatar_approval` | Avatar REST v1 路由：消息 list/ack/chat bring-up 以及 `POST /api/avatar/v1/approvals/{requestId}/resolve`；resolve 从 `app.state.approval_manager` 读取 Manager 并映射 XSpace 三态 action。 |
| `slash_candidates_loader.py` | `load_slash_candidates` | lifespan 调用：合并 builtin commands + skill specs 为统一候选列表，供 `/api/slash-candidates` 透传 |
| `slash_catalog/manager.py` | `SlashCatalogManager` | Web 斜杠目录门户：按请求上下文聚合 workflow、command、skill provider，并为前端扁平搜索提供 group 与叶子 item 数据。 |
| `whiteboard/store.py` | `WhiteboardSnapshot` / `WhiteboardCardRecord` / `WhiteboardLayoutUpdate` + CRUD 函数 | 白板持久化文件：根目录由 `WhiteboardManager(<kongming_home>/whiteboard)` 注入，project scope 按 `thread.cwd` 编码分区；每个 workspace 下保存 `board.json` + `cards/*.md` |
| `workspace/model.py` | `WorkspaceError` + `require_workspace_root` / `normalize_relative_path` / `resolve_workspace_path` | workspace 文件与 shell 共用辅助：thread→cwd 解析、路径校验与规范化 |
| `workspace/git.py` | `WorkspaceGitError` / `WorkspaceGitStatusEntry` + `read_git_status` / `read_git_branches` / `read_git_commits` 等纯函数 | workspace git 操作层：subprocess 调 git CLI，返回结构化数据（状态/分支/提交/diff/stage/unstage/checkout/commit） |
| `workspace/shell.py` | `WorkspaceShellProcess` + `build_claude_command` / `build_system_shell_command` / `is_claude_command` | workspace shell 运行时：PTY 子进程管理 + ANSI 清洗 + claude 命令识别与构建 |
| `ws_fanout.py` | `WebSocketFanout` | 同一 thread 多 WS 连接广播器：`attach_ws` / `detach_ws` / `send_json`；单连接失败只移除该连接，不影响其它 |

### 预留 auto 子系统 `src/hosts/web/approvals/auto/`

共享实现位于 [`docs/modules/安全/README.md`](../安全/README.md) 记录的 `src/safety/auto_approval/`。Web 的两个频道使用 `auto-approval-set-mode` 和 `auto-approval-query`，把 state 帧回写到同一 per-cwd store：

| 文件 | 导出 | 说明 |
|------|------|------|
| `src/hosts/web/approvals/auto/__init__.py` | `AuditLogger` + shared auto 类型 | Web 审批处置模式子系统入口。 |
| `src/hosts/web/approvals/auto/audit.py` | `AuditLogger` / `Outcome` | JSONL 审计追加器，`O_APPEND` 原子写保证并发安全；`log_request` / `log_decision` 两个 helper。 |
| `src/hosts/web/approvals/auto/config_store.py` | `ConfigStore` / `ProjectConfig` / `cwd_hash` | 兼容 re-export，真源为 `safety.auto_approval.config_store`。 |
| `src/hosts/web/approvals/auto/matchers.py` | `matches` / `normalize_bash_cmd` / `split_chained` | 兼容 re-export，真源为 `safety.auto_approval.matchers`。 |
| `src/hosts/web/approvals/auto/policy.py` | `AutoApprovalPolicy` | 兼容 re-export，真源为 `safety.auto_approval.policy`。 |
| `src/hosts/web/approvals/auto/rules.py` | `RuleDefinition` / `RuleSet` / `MatchKind` / `load_default_rules` / `materialize_user_rules_yaml` | 兼容 re-export，真源为 `safety.auto_approval.rules`。 |

### 媒体上传子系统 `src/hosts/web/uploads/`

| 文件 | 导出 | 说明 |
|------|------|------|
| `__init__.py` | (空) | Package 占位 |
| `storage.py` | `AssetStorage` / `AttachmentAsset` / `AttachmentKind` / `AttachmentStatus` | 文件 IO 抽象层，`<base>/<kind>s/<thread_id>/<asset_id>.<ext>` 路径结构，原子写 `.tmp + os.rename`；`delete_thread_assets` 供 thread 删除时级联清理 |
| `registry.py` | `EXT_BY_MIME` / `AssetRegistry` / `compute_sha256` | 资产记录管理（uuid4 ID + sha256 + AttachmentAsset 构造 + 投影为 UserInputAttachment DTO）；`EXT_BY_MIME` 复用 `core.contracts.IMAGE_EXT_BY_MIME` |
| `validation.py` | `MediaUploadValidator` / `UploadValidationError` | 上传校验器：MIME 白名单 → 5MB size 限制 → thread 存在性 → thread 归属；白名单从 registry.EXT_BY_MIME 派生 |

### claude_code 子模块 `src/hosts/web/integrations/claude_code/`（v0.1.6 引入 + v0.2 完善）

| 文件 | 导出 | 说明 |
|------|------|------|
| `route.py` | `router` | WS endpoint `/ws/claude-code?thread_id=...`；从 `ApprovalRuntimeManager` 获取 per-connection bridge，route 不依赖 safety 具体类型。 |
| `contracts.py` | `ClaudeApprovalProtocol` / `ClaudeApprovalFactoryProtocol` | route/service 与具体安全实现之间的最小结构合同。 |
| `service.py` | `ClaudeCodeService` | 调 SDK、复用 client、自动 resume、附件前缀；只消费 `ClaudeApprovalProtocol`。 |
| `approval.py` | `ApprovalBridge` | SDK tool/input ↔ canonical ApprovalRequest/ApprovalDecision 协议适配；共享安全链持有最终决策权。 |
| `_attachment_prefix.py` | `AttachmentPrefixBuilder` | 把 `UserInputAttachment` 列表转成 Claude Code CLI 的 `@<abs_path>` prompt 前缀；路径含空格走 `@"..."` quoted 语法；失败容错不阻塞发送 |
| `normalizer.py` | `ClaudeNormalizer` | live SDK 流式 message → NormalizedMessage 字典；过滤内部前缀；deny 后 ToolResult 去重 |
| `session_manager.py` | `SessionManager` + `SessionRecord` | `active_sessions` dict + writer 替换（重连）+ rename（thread_id → SDK 真 session_id）|
| `jsonl_history.py`（v0.2 new）| `jsonl_path_for` / `parse_jsonl_history` | 解析 `~/.claude/projects/<encoded-cwd>/<sdk_session_id>.jsonl` 转 NormalizedMessage[]；过滤 sessionId + timestamp 升序 + 宽容 skip |
| `projects_scanner.py`（v0.2 new）| `list_projects` + `ProjectSummary` / `SessionSummary` | 扫描 `~/.claude/projects/` 列所有 project + sessions 摘要（标题前 40 字 / mtime / 行数）；按 mtime desc |

### 配置管理子模块 `src/hosts/web/dashboard/config/`（manage-config-tab）

facade 单一入口：`ConfigManager`（聚合 schema + writer + restart）+ `router`（5 端点）。
ruamel.yaml round-trip 复用 v0.1.4 safety 模块已引入的依赖，无新增第三方包。

| 文件 | 导出 | 说明 |
|------|------|------|
| `__init__.py` | `ConfigManager` / `router` | facade 对外只暴露这两个符号；其他模块禁止直接 import 子文件 |
| `schema.py` | `FieldMeta` / `list_field_metas` / `list_groups` | 字段元数据真源：139 字段 × 7 group（model 10 / runtime 18 / tool_approval 24 / safety 8 / host_observ 55 / workflow 3 / sitian 21） |
| `writer.py` | `ConfigWriter` | ruamel.yaml round-trip 写回 + flock 文件锁 + load_config 校验回滚；保留注释/顺序/空行 |
| `restart.py` | `RestartLauncher` | detached subprocess 调 `./start.sh web restart`；不阻塞 HTTP 响应 |
| `manager.py` | `ConfigManager` + 5 DTO + 4 异常 | 聚合层：`get_schema` / `get_effective` / `get_raw` / `save_patch` / `restart`；异常体系 `ConfigLockedError` / `ConfigValidationError` / `ConfigWriteError` / `RestartFailedError` |
| `router.py` | `router` | 5 端点 + HTTP 状态翻译：409 Locked / 422 Validation / 500 Write / 503 Restart |

**前端对端**：`web/src/modules/dashboard/config/` 同构 facade 设计

| 文件 | 职责 |
|------|------|
| `ConfigPage.tsx` | 顶级容器：顶横 tab + schema.groups 动态 section + 底部 `SaveRestartBar` |
| `api.ts` | 唯一对外 fetch 出口：`getSchema` / `getEffective` / `getRaw` / `savePatch` / `restart` / `getHealth` |
| `types.ts` | DTO TS 复刻（与 Python 严格对齐） |
| `store.ts` | zustand 状态层：`dirty` / `saveStatus` / `restartStatus` / health polling |
| `sections/{Model,Runtime,ToolApproval,Safety,HostObserv}Section.tsx` | 已定制 group 渲染 |
| `sections/GenericConfigSection.tsx` | 未定制 group 的通用渲染，当前覆盖 workflow / sitian |
| `components/{FieldRenderer,SafetyRulesView,YamlPreview,SaveRestartBar}.tsx` | 字段渲染 / safety 规则只读视图 / YAML 预览 / 保存重启状态栏 |

**路由变更（破坏性）**：
- 删除：`/manage/runtime-status`
- 新增：`/manage/config[/:section]` + `/manage/network`
- `Manage.tsx` 从顶部横 tab 改成左侧竖 tab（配置 / 网络）

**已知 gap**：
- list 类型字段（如 `sitian.sources`）只读展示，后续可按业务加专用编辑器。
- 详见 `dev-pipeline/tasks/manage-config-tab/README.md`。

### 协议层 `src/hosts/web/protocol/`

| 文件 | 导出 | 说明 |
|------|------|------|
| `_base.py` | `ErrorCode` / `EvictReason` / `ApprovalOutcome` + 帧基类 | 公共枚举 + frozen Pydantic 基类。 |
| `ws_frames.py` | WS 帧类 + union + adapter | 审批 inbox 帧包含 `danger`、`rememberAllowed` 与无 scope 的 remember candidate；resolve 使用 `remember: bool`。 |
| `rest_models.py` | REST DTO | 含 `PermissionRuleDTO`、schema v2 `ThreadPermissionsDTO`、migration summary 与 update DTO；固定字段和 nullability，严格拒绝未知字段。 |
| `__init__.py` | 40+ 个 re-export | 协议层统一入口 |

`ThreadStatusFrame` 已采用渐进式生成链，`ThreadStatusSnapshotFrame` 组合该生成类型：

1. Python `hosts.web.protocol.ThreadStatusFrame` 是字段、类型、必选性和 phase
   枚举的真源。
2. `scripts/export_thread_status_frame_schema.py` 使用 Pydantic serialization
   schema，并按 `model_dump(exclude_none=True)` 规范化 required。
3. `make web-protocol-generate` 生成并提交
   `web/src/protocol/generated/thread-status-frame.{schema.json,ts}`。
4. `make web-protocol-check` 在临时目录重新生成并逐字节比较；CI 会执行该门禁。
5. `web/src/protocol/ws-thread-status.ts` 重导出生成类型，并定义
   `thread-status.snapshot {watermark, items}`；前端以 snapshot 全量 replace。

后续协议继续以完整 frame 或完整通道为迁移单位；生成目录只读，字段变化从
Python Pydantic 真源发起。

### REST 路由 `src/hosts/web/routers/`

| 文件 | 前缀 | 端点 |
|------|------|------|
| `auth.py` | `/api/auth` | POST login / POST logout / GET me |
| `threads.py` | `/api/threads` | thread CRUD、导入与历史；POST `{id}/fork` 接收 assistant `history_index` 并创建精确前缀分支，快照冲突返回 409；GET/PUT `{id}/permissions` 读取/整本 CAS 更新当前 thread 本子；DELETE 由 ThreadManager 编排本子清理补偿。 |
| `claude.py`（v0.2 new）| `/api/claude` | GET `projects`（projects/sessions 摘要树）|
| `presets.py` | `/api/presets` | GET 合并 catalog 的脱敏 preset 列表；运行字段全部来自 `ResolvedModelConfig` |
| `model_providers.py` | `/api/model-providers` | 通过 `ModelCatalogManager` 提供 catalog / connection / probe / model-families；连接成功只写 provider-specific credential env |
| `codex.py` | `/api/codex` | GET `projects`（Codex 项目列表）/ GET `sessions/{id}/history`（会话历史回放）/ GET `sessions/{id}/meta`（会话元数据）|
| `uploads.py` | `/api/uploads` | POST `images`（上传图片返回 `UserInputAttachment`）+ GET `{asset_id}`（读取资产 bytes）；`asset_id` 强制 32 位 hex 校验防 glob 展开 |
| `slash_catalog.py` | `/api` | GET `slash-catalog` 返回分组摘要；GET `slash-catalog/groups/{group_id}` 返回叶子 items；两条入口都按可选 `thread_id` 解析频道可见性。 |
| `slash_candidates.py` | `/api` | GET `slash-candidates`（commands + skills 合并候选列表）|
| `whiteboard.py` | `/api/whiteboard` | GET 白板快照 / POST 新建卡片 / PATCH 卡片内容 / DELETE 卡片 / PUT 布局更新 |
| `workspace/git.py` | `/api/threads` | GET `{id}/workspace-git/status` / `branches` / `commits` / `file-diff`；POST `stage` / `unstage` / `checkout` / `create-branch` / `commit` |
| `workspace/shell.py` | — | WS `/ws/workspace-shell?thread_id=...`（按 thread 绑定 workspace PTY shell；cookie 鉴权 + claude 命令检测 + session 自动绑定）|
| `manage.py` | `/api/manage` | GET cells / POST cells/{id}/stop |
| `dashboard/config/router.py` | `/api/manage/config` | GET `schema`（139 字段元数据 + group 划分）/ GET `effective`（runtime merged）/ GET `raw`（YAML 原文）/ POST `save`（patch dict round-trip 写回）/ POST `restart`（detached subprocess 调 start.sh）|
| `health.py` | `/api/health` | GET 公开探活端点（auth 白名单），重启探测语义；返回 `{status, version, started_at}` |

### 前端 `web/src/`

| 文件 | 职责 |
|------|------|
| `pages/Chat.tsx` | 主聊天页：左栏 LeftSidebar；右侧按 thread.backend_kind 分发：generic_chat 走 MessageList + Composer；claude_code 走 ClaudeCodeView |
| `pages/Login.tsx` | 登录页（密码 + 忘记密码重置） |
| `pages/Manage.tsx` | 管理页：左侧竖 tab 容器（配置 / 网络），右侧渲染对应 section；嵌入 `modules/dashboard/config/ConfigPage`（manage-config-tab）；破坏性变更：删 `/manage/runtime-status`，新增 `/manage/config[/:section]` + `/manage/network` |
| `pages/NotFound.tsx` | 404 页面 |
| `components/MessageList.tsx` | 消息列表渲染 + smart auto-scroll |
| `components/Composer.tsx` | 输入框组件；切模型采用 catalog 默认 effort，有控制合同才显示档位菜单，显式 `none` 原样发送；`onSubmit=false` 保留完整草稿，`draftSeed` 恢复新会话自动首发失败文本。 |
| `components/SlashMenu.tsx` | 单层斜杠搜索菜单：打开时通过现有 catalog routes 汇总全部叶子 items，按 exact/prefix/substring/ordered-subsequence 评分，Command/Skill/Workflow 作为结果标签；选择后交回 Composer 处理 item action。 |
| `components/EvolutionDecisionModal.tsx` | 展示 review/nutrient，并让用户逐条选择“采纳为记忆”“采纳为技能”或“忽略”；显式 Tool 只生成候选内容，继续复用同一决策与 apply 链。 |
| `components/ThreadList.tsx` | 通用 tab 下的 thread 列表 + 新建 / 重命名 / 删除 + 读 threadStatus store 渲染 PhaseIndicator |
| `components/LeftSidebar.tsx`（v0.2 new）| 左栏总壳：通用 / Claude 双 Tab + ThreadList / ClaudeProjectsTree 切换；mount 时无条件 fetchThreads（保证 Chat.tsx 拿到 backend_kind 决定 ws 路径）|
| `components/LeftSidebarTabs.tsx`（v0.2 new）| 通用 / Claude Tab 组件（shadcn Tabs）+ localStorage 持久化（`loadPersistedTab` / `persistTab`） |
| `components/ClaudeProjectsTree.tsx`（v0.2 new）| Claude tab 内容：projects 折叠树（最近一周自动展开）+ session 卡片（title / 相对时间 / 消息数 badge）+ 默认 6 条 + "显示更多"；SessionCard 渲染 PhaseIndicator（claude_thread_id → thread_id useMemo 映射） |
| `components/ClaudeCodeView.tsx`（v0.1.6 / v0.2 完善）| claude_code 路径主组件：mount 时拉 history → 14 kind 渲染（text/thinking/stream_delta/tool_use/...）+ ws 接力新消息；含双轨收口（hadStreamThisTurn dedupe + complete 兜底转 stream→text）|
| `features/approval-inbox/{types,useApprovalInbox,ApprovalToastQueue,ApprovalToastCard,index,senderRef}` | 全局审批 inbox：右下角跨 thread 聚合卡片；resolve 期间保持 submitting，失败回执展示错误并恢复按钮，remove/snapshot omission 才删除；late result 不会复活卡片。 |
| `features/thread-permissions/ThreadPermissionsManager.ts` + `internal/ThreadPermissionsView.tsx` | 当前 thread permissions 前端门户与内部视图；编辑 `{expression, scope_cwd}` 规则，封装 schema v2 GET/PUT、revision CAS、迁移提示、thread 切换隔离和加载/空/保存/冲突/失败态。 |
| `components/StatusLine.tsx` | 输入框下方常驻 token 用量显示 |
| `components/ThemeToggle.tsx` | dark 模式手动切换（light / dark / system 三态） |
| `components/ChatAvatar.tsx` | 聊天气泡头像（用户 / assistant 区分） |
| `components/Layout.tsx` | 全局布局壳（Sidebar + Main）+ 挂载 `useThreadStatusWS`，并按 `backend_kind` 渲染全局状态球与 Claude 专用连接状态球 |
| `components/PhaseIndicator.tsx` | 共享 phase 状态图标组件：idle→隐藏 / responding→绿脉动 / thinking→紫脉动 / tool_calling→wrench SVG / waiting_approval→黄脉动 / complete→✓ / error→✗ |
| `components/CellRow.tsx` | thread 列表单行渲染 |
| `components/NewThreadDialog.tsx` | 新建 thread 对话框 |
| `components/ErrorBoundary.tsx` | React 错误边界 |
| `stores/chat.ts` | ChatItem 展示兼容类型、usage 与 StatusLine；流式缓冲与调度已迁移到 `ChatTimelineStore` |
| `stores/threads.ts` | Thread 列表 + CRUD |
| `stores/threadStatus.ts` | 服务端 canonical 运行态投影；snapshot 按当前 connection generation 全量 replace，delta 按全局 sequence 单调应用，终态删除 active 项，旧连接帧被拒绝。 |
| `stores/threadDispatch.ts` | per-thread transport 交互态 `dispatching/error`；发送成功后清除，发送失败保留可诊断错误，独立于 canonical running。 |
| `stores/connectionStatus.ts` | header 连接状态 store；承接 `threadWs*`、`claudeWs*`、`statusWs*` 三组字段，供 `Layout` 渲染连接状态球 |
| `stores/auth.ts` | 登录/登出状态 |
| `stores/cells.ts` | Cell 列表状态（管理页） |
| `stores/theme.ts` | 主题状态（light / dark / system） |
| `hooks/useStreamingRender.ts` | 14 类 S2C 帧 → store dispatch（generic_chat 路径）|
| `hooks/useWS.ts` | ThreadSocket 管理（连接 + 指数退避重连，generic_chat 路径）|
| `hooks/useClaudeCodeWS.ts`（v0.1.6 new）| `ClaudeCodeSocket` 管理（claude_code 路径，与 `useWS` 平级），并同步写入 `connectionStatus` 中的 Claude 连接状态 |
| `hooks/useThreadStatusWS.ts` | 全局 `/ws/thread-status` WS hook（`Layout` 挂载）：每次真实连接分配 generation，先应用 snapshot，再应用 sequence delta；旧 socket late frame 无法写入当前 store。 |
| `lib/api.ts` | REST 封装（CSRF header + 401/429 拦截） |
| `lib/ws.ts` | `ThreadSocket` 类（generic_chat 路径 `/ws/threads/{id}`）|
| `lib/claude-ws.ts`（v0.1.6 new）| `ClaudeCodeSocket` 类（claude_code 路径 `/ws/claude-code?thread_id=...`，与 ThreadSocket 平级，状态机/重连复刻）|
| `lib/relative-time.ts`（v0.2 new）| `formatRelative(unixSeconds)` → 刚刚 / X 分钟前 / X 小时前 / YYYY-MM-DD |
| `lib/markdown.tsx` | Markdown 渲染组件 |
| `lib/router.tsx` | 路由配置 |
| `lib/debug.ts` | 调试工具 |
| `lib/design-tokens.ts` | 设计 token 常量 |
| `lib/utils.ts` | 通用工具函数 |
| `protocol.ts` | TS 协议公共入口：`ThreadStatusFrame` 重导出 Pydantic 生成类型；其余 DTO 暂时 1:1 复刻 Python，包含 schema v2 permissions 与审批规则合同。 |
| `chat/` | `ChatManager` / provider registry / timeline store | message-runtime-v0.1 聊天运行时。Manager 统一 send / history / inbound frame / interrupt / choice submit；设计见 [`docs/spec/web-chat/message-runtime-v0.1/`](../../spec/web-chat/message-runtime-v0.1/README.md) |
| `network/` | `NetworkManager` / `ChannelHandle` / Heartbeat | 前端 WebSocket 连接池和心跳层。业务 hook 通过 `openChannel()` 获取句柄；设计见 [`docs/spec/web-network-layer-v0.1/`](../../spec/web-network-layer-v0.1/README.md) |

### 连接相关 spec

- [Claude 连接保活 v0.1](../../spec/web-claude-session-keepalive-v0.1/)：已落地 Claude 专用连接球、`ping/pong` 心跳、`check-session-status` 续接；后续收口 `claude_history` 与 live merge

## 配置

| yaml 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `web.enabled` | bool | false | 是否启用 |
| `web.host` | str | "0.0.0.0" | bind IP |
| `web.port` | int | 60000 | 端口 |
| `web.dev_mode` | bool | false | 跳过登录鉴权 |
| `web.idle_timeout_seconds` | int | 1800 | cell 空闲超时 |
| `web.idle_check_interval_seconds` | int | 60 | 后台扫描周期 |
| `evolution.learning.enabled` | bool | false | 进化模块、公开 Tool 与 lifecycle 总开关 |
| `evolution.learning.auto_trigger_enabled` | bool | true | cadence 自动触发开关；false 时 generic_chat 主 runtime 仍可显式请求 review |

环境变量：`KONGMING_WEB_*` 前缀覆盖上述字段 + `KONGMING_WEB_SECRET` / `KONGMING_WEB_PASSWORD`。

当前仓库配置和 XSpace 打包配置都使用 `web.port=60000`。该值仍可通过当前 `setting.yaml`、`KONGMING_WEB_PORT` 或启动参数覆盖。

模型服务商配置路径：

| 路径 | 作用 |
|------|------|
| `config/model-providers.yaml` | 内置 provider 与 provider 下多模型列表 |
| `<KONGMING_HOME>/model-providers.yaml` | 用户完整 provider/model 定义；同 provider ID 完整替换 |
| `KONGMING_HOME/.env` | Web 管理页写入 provider API key |
| 当前 `setting.yaml` 的 `model.preset_id/reasoning_effort` | 默认运行选择 |

Web 状态持久化位于 `.kongming/web/`；thread permissions 位于 `.kongming/safety/thread_permissions/<sha256(thread_id)>.json`。

## 已知问题 / 待完成

**v0.2 修复（2026-05-02）**

- **claude_code 刷新页面丢历史**：v0.1.6 thread / sdk_session_id 1:1 绑定只在内存里维护。已修复：thread_metadata schema v3 加 sdk_session_id + cwd 落盘 + jsonl_history parser + claude_history endpoint，刷新后从 SDK jsonl 完整恢复 + 自动 `options.resume` 续上下文。
- **左栏 Tab 切换后刷新拿不到 thread metadata（race）**：Claude tab 下 ThreadList 不渲染 → fetchThreads 没调 → Chat.tsx backend_kind 默认值 generic_chat 触发错误的 `/ws/threads/{id}` 403。已修复：LeftSidebar mount 时 unconditional fetchThreads + Chat.tsx 等 thread metadata fetch 完才连 ws。
- **claude_code 流式 buffer 不收口**：normalizer 同时翻译 SDK partial messages（stream_delta）和 final AssistantMessage（text），导致同一句话双渲染。已修复：ClaudeCodeView 用 `hadStreamThisTurn` ref 标记 + complete 兜底转 stream→text。
- **cwd 解码错位（含 `-` / `_` / `.` 字符）**：原 plan.md 标记的 v0.3 修复项；上线后立即出 bug（"Working directory does not exist"）。已修复：`projects_scanner` 改从 jsonl entry 直接读 SDK 写的 `cwd` 字段（真值），目录名解码降级 fallback。详见 `dev-pipeline/tasks/claude-code-history-resume-v0.2/reports/fix/fix-note-20260502-144255.md`。已存在错误 thread 用户手动删了重导。

**v0.1.6 修复（2026-04-28）**

- **chat store stale closure / auto-scroll / 前端旧 build 不更新** 三个问题已修复（细节略，参考 git log）。

**当前待解决**

- **claude_code 历史 attachment 不渲染**：jsonl 中 `type=attachment` 的图片/文件被 parser skip。v0.3 视需求加 NormalizedMessage 图片 kind。
- **subagent 工具历史**（agent-*.jsonl）：v0.2 不展开。v0.3+ 视需求加。
- **chunk 体积警告**：`index.js` 超过 500KB，建议 code-split（`web/src/stores/auth.ts` 动态导入 vs 静态导入冲突）
- **history assistant turn=-1**：`setHistory` 从后端收到的历史消息没有正确 turn 编号（`ThreadHistoryFrame` 的 `HistoryMessageDTO` 中 `turn` 字段可能缺失），所有历史 assistant 都渲染为 `turn=-1`（generic_chat 路径）

## 参考

- 审批测试指南：[`docs/modules/安全/approval-testing-guide.md`](../安全/approval-testing-guide.md)
- WS 协议帧定义：`src/hosts/web/protocol/ws_frames.py`
- TS 协议公共入口：`web/src/protocol.ts`
- ThreadStatusFrame 生成命令：`make web-protocol-generate`
- ThreadStatusFrame 漂移门禁：`make web-protocol-check`
- v0.1.5 前端设计文档：[`docs/spec/web-multi-thread-frontend-v0.1.5/`](../../spec/web-multi-thread-frontend-v0.1.5/)
- claude_code v0.1（D2 后端）：[`dev-pipeline/tasks/claude-code-web-v0.1/`](../../../dev-pipeline/tasks/claude-code-web-v0.1/)
- claude_code thread UI v0.1.6（D3.1）：[`dev-pipeline/tasks/claude-code-thread-ui-v0.1/`](../../../dev-pipeline/tasks/claude-code-thread-ui-v0.1/)
- claude_code 历史回放 v0.2（D3.2）：[`dev-pipeline/tasks/claude-code-history-resume-v0.2/`](../../../dev-pipeline/tasks/claude-code-history-resume-v0.2/)
- 参考：claudecodeui `other/claudecodeui/server/projects.js`（jsonl 解析算法源）
- 安全模块（审批链路）：[`docs/modules/安全/README.md`](../安全/README.md)
- 宿主模块（HostAdapter）：[`docs/modules/宿主/README.md`](../宿主/README.md)
- 启动进度协议（Tauri 接入）：[`docs/modules/Web前端/启动进度协议.md`](启动进度协议.md)
- 智能审批设计文档：[`docs/spec/safety-approval-manager-v0.5/`](../../spec/safety-approval-manager-v0.5/)

---

*文档生成时间：2026-05-01 / v0.2 更新：2026-05-02 / v1 更新：2026-05-18*
