# CLI 消息收发时序图

本页描述当前 CLI 普通文本链路。命令输入走 `CommandService.handle_command` 控制面，普通文本统一通过 `HostDispatcher.submit(text, mode)` 投递 root agent mailbox。

## 普通发送和排队

`CLIInteractiveLoop.send()` 对普通文本创建后台 submit task 并立即回到 REPL。`SubmitMode.QUEUE` 在 `HostDispatcher` 内等待本轮 `Result`，但等待发生在后台 task 里；用户侧可以继续输入。agent 忙时，`AgentManager` 的 root mailbox 保持 FIFO，上一轮结束后继续处理下一条。

```mermaid
sequenceDiagram
    autonumber
    participant User as 用户
    participant Adapter as CLIAdapter
    participant CliLoop as CLIInteractiveLoop
    participant Dispatcher as HostDispatcher
    participant Manager as AgentManager
    participant AgentLoop as agent_loop
    participant Runtime as SessionEngine
    participant Runner as Runner
    participant LLM as LLMProvider
    participant Sinks as EventSinks
    participant Stdout as 终端输出

    User->>Adapter: 输入普通文本
    Adapter->>CliLoop: read_input 返回文本
    CliLoop->>CliLoop: parse_input 判定 text
    CliLoop->>Dispatcher: 后台 submit(text, QUEUE)
    CliLoop-->>Adapter: 立即返回 QUEUED 回执
    Dispatcher->>Dispatcher: ensure_started 懒启动 root agent
    Dispatcher->>Manager: submit(user_input, QUEUE, metadata)
    Manager->>AgentLoop: root mailbox 入队

    alt root agent 空闲
        AgentLoop->>Runtime: mail_run_bridge(mail_text)
    else root agent 正在执行
        AgentLoop-->>AgentLoop: mailbox 等待上一轮完成
        AgentLoop->>Runtime: 取下一条 mail 后运行
    end

    Runtime->>Runner: run(mail_text, session_id, agent_id, event_context)
    Runner->>LLM: complete 或 stream
    LLM-->>Runner: assistant message / stream chunks
    Runner->>Sinks: turn.start / content.delta / reasoning.delta / tool.* / turn.end
    Sinks->>Stdout: CLIStreamSink 实时写 delta
    Runner-->>Runtime: Result
    Runtime-->>AgentLoop: Result
    AgentLoop->>Dispatcher: HostDispatcherDeliverSink 回填 FIFO future
    Dispatcher->>Adapter: queued_result_handler(render_result)
    Adapter->>Stdout: 非流式 final / error / usage 兜底输出
```

## 显式插队 send-now

`CLIInteractiveLoop.send_now()` 对普通文本先尝试 `SubmitMode.IMMEDIATE`。命中活跃 run 时，文本进入 `runtime.steer` 的插入缓冲；未命中时，CLI 创建普通 QUEUE submit task，回到上一节流程。

```mermaid
sequenceDiagram
    autonumber
    participant User as 用户
    participant Adapter as CLIAdapter
    participant CliLoop as CLIInteractiveLoop
    participant Dispatcher as HostDispatcher
    participant Runtime as SessionEngine
    participant Runner as Runner
    participant Manager as AgentManager
    participant AgentLoop as agent_loop
    participant Sinks as EventSinks
    participant Stdout as 终端输出

    User->>CliLoop: send_now(text)
    CliLoop->>CliLoop: parse_input 判定 text
    CliLoop->>Dispatcher: submit(text, IMMEDIATE)
    Dispatcher->>Runtime: steer(session_id, text)

    alt 当前 run 接收插入
        Runtime-->>Dispatcher: True
        Dispatcher-->>CliLoop: SubmitReceipt(merged=True)
        CliLoop->>Adapter: write_output("[send-now] merged into current run")
        Adapter->>Stdout: 插队确认
        Runtime->>Runner: 在安全边界注入 text
        Runner->>Sinks: steer.injected + 后续 content.delta
        Sinks->>Stdout: 正常流式输出
    else 没有可插入的活跃 run
        Runtime-->>Dispatcher: False
        Dispatcher-->>CliLoop: SubmitReceipt(merged=False)
        CliLoop->>Dispatcher: 后台 submit(text, QUEUE)
        Dispatcher->>Manager: submit(user_input, QUEUE, metadata)
        Manager->>AgentLoop: root mailbox 入队
    end
```

## 接收侧边界

- 实时文本：`Runner` 通过 `content.delta` / `reasoning.delta` 事件写入 `CLIStreamSink`，终端边生成边显示。
- 最终结果：`HostDispatcherDeliverSink` 只回填等待中的 `Result` future；`CLIAdapter.render_result` 负责非流式 final、错误和用量兜底输出。
- 命令输入：slash command 由 `CommandService.handle_command` 处理；普通文本链路不经过命令服务。
