# src/network/ — 网络连接层

WebSocket 连接生命周期、心跳响应和网络诊断日志的共享底层模块。

## 设计理念

| 决策 | 理由 |
|------|------|
| `NetworkManager` 统一管理连接注册与发送 | 连接生命周期、心跳实例和写失败清理集中在一个管理器内 |
| `Heartbeat` 保持单连接状态机 | 每条连接独立持有心跳状态，多连接并发时避免共享可变状态 |
| `network.tools` 保持包内私有 | `make_connection_id` / `safe_send_json` 是 network 内部工具，业务层通过 `NetworkManager` 消费能力 |
| 诊断日志独立写入 JSONL / log 文件 | 运行时网络失败可观察，同时不污染主业务链路 |

## 核心流程

```text
WebSocket endpoint accepted
  -> get_network_manager().register(channel, websocket, thread_id)
  -> inbound frame first passes NetworkManager.handle_inbound(...)
  -> ping/pong consumed by Heartbeat
  -> business frame returns False for route-specific handler
  -> send failure triggers unregister(conn_id)
```

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `src/network/__init__.py` | `ConnectionInfo` / `Heartbeat` / `HeartbeatConfig` / `HeartbeatHooks` / `NetworkManager` / `get_network_manager` | 网络层公共入口 |
| `src/network/heartbeat.py` | `HeartbeatHooks` / `HeartbeatConfig` / `Heartbeat` | 单连接心跳状态机；v0.1 server 角色被动响应 `ping` 并返回 `pong` |
| `src/network/manager.py` | `ConnectionInfo` / `NetworkManager` / `get_network_manager` / `reset_network_manager_for_test` / `configure_heartbeat_log` | 连接注册、注销、心跳拦截、统一发送和测试重置 |
| `src/network/tools.py` | `make_connection_id` / `safe_send_json` | network 包私有工具；生成连接 ID，吞写失败并返回布尔结果 |
| `src/network/network_log.py` | `log_network_event` / `log_network_exception` | 写 `<kongming_home>/logs/network/network-events.jsonl` |
| `src/network/keepalive_log.py` | `append_keepalive_log` | 写 `.kongming/logs/claude-keepalive.jsonl` |

## 配置

| 配置 | 来源 | 说明 |
|------|------|------|
| `ws_heartbeat_interval_ms` / `ws_heartbeat_timeout_ms` / `ws_heartbeat_max_missed` | `cfg.web` | `HeartbeatConfig` 真源 |
| 心跳诊断日志目录 | Web app lifespan 注入 | `configure_heartbeat_log(log_dir)` 初始化 FileHandler |

## 已知问题 / 待完成

| 项 | 状态 |
|----|------|
| generic / codex / thread-status / scheduler / workspace-shell 统一纳入 `NetworkManager` | v0.2 范围 |
| server 主动 ping | v0.3 范围，当前 v0.1 只被动响应前端 ping |
