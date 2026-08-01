# Web 消息收发时序图

本页描述 `generic_chat` 当前主链路。浏览器入帧先到 `websocket/routes.py`，普通输入进入 `ThreadManager` 的 pending input 状态机，实际运行统一通过 `HostDispatcher.submit(text, mode)` 投递 root agent mailbox。下行内容由 `WSEventSink` 把 `Runner` 事件转换成 WS 帧。

## 连接和补偿下行

WebSocket 建连后先补历史和 pending input 队列快照，再进入持续收帧循环。断线重连时，前端用这两类帧校准历史消息和后端队列真源。

```mermaid
sequenceDiagram
    autonumber
    participant Browser as 浏览器
    participant Routes as websocket/routes.py
    participant TM as ThreadManager
    participant Cell as ThreadCell
    participant Adapter as WebHostAdapter
    participant Sink as WSEventSink

    Browser->>Routes: WS /ws/threads/{thread_id}
    Routes->>Routes: cookie 和 thread_id 校验
    Routes->>TM: boot_or_attach(thread_id)
    TM-->>Routes: ThreadCell
    Routes-->>Browser: accept
    Routes->>Cell: attach_ws(websocket)
    Routes->>Browser: thread.history
    Routes->>TM: pending_input_snapshot(thread_id)
    TM-->>Routes: pending-input.snapshot
    Routes->>Browser: pending-input.snapshot
    Routes->>Routes: receive_loop 等待 C2S 帧

    Note over Adapter,Sink: 后续 runtime 事件直接由 WSEventSink 推给当前 WS
```

## 普通发送和排队

`user.input` / `choice.submit` 的生产路径都进入 `ThreadManager._submit_pending_input`。当前 thread 空闲且队列为空时直接启动 run；当前 run 占用执行权、drain 被阻断或已有队列时写入 `pending_inputs`，再通过 `pending-input.changed` 把完整快照广播给前端。

```mermaid
sequenceDiagram
    autonumber
    participant Browser as 浏览器
    participant Routes as websocket/routes.py
    participant TM as ThreadManager
    participant Cell as ThreadCell
    participant Dispatcher as HostDispatcher
    participant Manager as AgentManager
    participant AgentLoop as agent_loop
    participant Runtime as SessionEngine
    participant Runner as Runner
    participant LLM as LLMProvider
    participant Sink as WSEventSink

    Browser->>Routes: user.input(text, request_id, attachments, references)
    Routes->>Routes: GenericChatC2SAdapter 校验
    Routes->>TM: submit_user_input(thread_id, text, metadata)
    TM->>Cell: pending_input_lock 内创建 PendingInputDTO

    alt 空闲且队列为空
        TM->>Cell: current_run_task = _run_via_host_dispatcher task
        TM-->>Browser: pending-input.started
    else 正在运行或 drain 阻断
        TM->>Cell: pending_inputs 追加并递增 version
        TM-->>Browser: pending-input.changed(reason=added)
    else 空闲但已有队列
        TM->>Cell: 当前输入入队，取队头启动
        TM-->>Browser: pending-input.started
        TM-->>Browser: pending-input.changed(reason=drained)
    end

    opt 当前调用或后续 done callback 启动了一条 pending input
        TM->>Dispatcher: submit(content, QUEUE, attachments, references, metadata)
        Dispatcher->>Dispatcher: ensure_started
        Dispatcher->>Manager: submit(user_input, QUEUE, metadata)
        Manager->>AgentLoop: root mailbox 入队
        AgentLoop->>Runtime: mail_run_bridge(mail_text)
        Runtime->>Runner: run(mail_text, session_id, agent_id, event_context)
        Runner->>LLM: complete 或 stream
        LLM-->>Runner: assistant message / stream chunks
        Runner->>Sink: Event(turn.*, content.delta, reasoning.delta, tool.*, usage)
        Sink-->>Browser: turn.start / content.delta / reasoning.delta / tool.* / turn.end
        Runner-->>Runtime: Result
        Runtime-->>AgentLoop: Result
        AgentLoop->>Dispatcher: HostDispatcherDeliverSink 回填 Result future
        Dispatcher-->>TM: submit(QUEUE) 完成
        TM->>TM: _handle_pending_run_done

        alt 队列还有下一条
            TM->>Cell: pop_next_pending_input 并启动下一轮
            TM-->>Browser: pending-input.started
            TM-->>Browser: pending-input.changed(reason=drained)
        else 队列已清空
            TM->>Cell: current_run_task = None
        end
    end
```

## 插队 send-now

前端只能对尚未启动的 pending input 发 `pending-input.send-now`。active run 存在时，后端校验该 pending input 只含可安全插入的纯文本参数，然后通过 `HostDispatcher.submit(IMMEDIATE)` 调用 `runtime.steer`；命中插入后移出队列并广播 `pending-input.steered`。没有可插入 run 时，该项会从队列移出并启动成普通 QUEUE run。

```mermaid
sequenceDiagram
    autonumber
    participant Browser as 浏览器
    participant Routes as websocket/routes.py
    participant TM as ThreadManager
    participant Cell as ThreadCell
    participant Dispatcher as HostDispatcher
    participant Runtime as SessionEngine
    participant Runner as Runner
    participant WSSink as WSEventSink
    participant SteerSink as PendingInputSteerEventSink

    Browser->>Routes: pending-input.send-now(pending_input_id)
    Routes->>TM: send_pending_input_now(thread_id, pending_input_id)
    TM->>Cell: pending_input_lock 内定位 queued pending input

    alt 当前有 active run 且 steer 命中
        TM->>Dispatcher: submit(content, IMMEDIATE)
        Dispatcher->>Runtime: steer(session_id, content)
        Runtime-->>Dispatcher: True
        Dispatcher-->>TM: SubmitReceipt(merged=True)
        TM->>Cell: 移出 pending_inputs，记录 send_now_claim
        TM-->>Browser: pending-input.steered
        TM-->>Browser: pending-input.changed(reason=sent_now)
        Runtime->>Runner: 安全边界注入 content
        Runner->>SteerSink: steer.injected
        SteerSink->>TM: _handle_runtime_event
        TM-->>Browser: pending-input.steered(run_id, turn)
        Runner->>WSSink: 后续 content.delta / turn.*
        WSSink-->>Browser: 常规 runtime 帧继续下行
    else 当前无 active run 或 steer 未命中
        TM->>Cell: 移出 pending_inputs
        TM->>Dispatcher: submit(content, QUEUE)
        TM-->>Browser: pending-input.started
        TM-->>Browser: pending-input.changed(reason=drained)
    end
```

## 临时 cell 旁路

少数无 `pending_input_lock` 的 ephemeral cell 仍由 `routes.py` 启动 `_start_run_once_task`。该旁路没有 pending input 队列；运行中收到新 `user.input` 会返回错误帧。它的执行入口仍是 `cell.host_dispatcher.submit(QUEUE)`。

```mermaid
sequenceDiagram
    autonumber
    participant Browser as 浏览器
    participant Routes as websocket/routes.py
    participant Cell as EphemeralCell
    participant Dispatcher as HostDispatcher
    participant Manager as AgentManager
    participant AgentLoop as agent_loop
    participant Runtime as SessionEngine
    participant Runner as Runner
    participant Sink as WSEventSink

    Browser->>Routes: user.input(text)
    Routes->>Routes: _supports_pending_input_queue 返回 false
    alt current_run_task 正在运行
        Routes-->>Browser: error 当前任务正在运行
    else 空闲
        Routes->>Cell: current_run_task = _run_once_safely task
        Cell->>Dispatcher: submit(text, QUEUE)
        Dispatcher->>Manager: submit(user_input, QUEUE, metadata)
        Manager->>AgentLoop: root mailbox 入队
        AgentLoop->>Runtime: mail_run_bridge(mail_text)
        Runtime->>Runner: run(mail_text, session_id, agent_id, event_context)
        Runner->>Sink: runtime events
        Sink-->>Browser: content.delta / turn.* / tool.*
    end
```

## 接收侧边界

- 历史补偿：建连时 `thread.history` 从 session 历史生成；`pending-input.snapshot` 从 `ThreadCell.pending_inputs` 生成。
- 实时内容：`Runner` 发 `content.delta` / `reasoning.delta` / `turn.*` / `tool.*` / usage 事件，`WSEventSink` 转成协议帧。
- 最终兜底：`WebHostAdapter.render_result` 对正常结果 no-op；错误结果补一条 assistant final，避免浏览器漏显错误。
