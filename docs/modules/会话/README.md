# src/sessions/ — 会话持久化层

承接当前 run 可见的多轮会话历史，把 `core.session.InMemorySession` 工程化升级为 SQLite / File 两种持久化实现。`Session` Protocol 真源仍在 `core.contracts`，本层只提供实现、恢复和发现能力。

## 设计理念

| 决策 | 理由 |
|---|---|
| `Session` Protocol 只在 `core.contracts` | 防止持久化层长出第二套会话抽象 |
| 三种 backend 并存：`memory` / `sqlite` / `file` | memory=进程内；sqlite=跨进程可恢复；file=append-only JSONL + 消息链 |
| `SQLiteSession` 每次操作现场 open / exec / close | stdlib sqlite3 同线程约束下保持实现简单 |
| `FileSession` 首次 `append()` 才 materialize | 避免空 session 产生磁盘残留 |

## 核心流程

1. `cli/main.py` 构造 `SessionBootstrap(agent_name, model_name, instruction_sources, instruction_text_hash, created_at, cwd, app_version)`。
2. `sessions.build_session(cfg, session_id, bootstrap=bootstrap)` 按 `cfg.session.backend` 分派到 `InMemorySession` / `SQLiteSession` / `FileSession`。
3. `SessionEngine.build(session_factory=...)` 只消费 `core.contracts.Session`，不关心具体 backend。
4. CLI / Web 启动恢复通过 `session_discovery.py` 扫描 file 或 sqlite session，再选择最近活跃或显式 `session_id`。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|---|---|---|
| `__init__.py` | `SQLiteSession` / `FileSession` / `build_session` / `SessionBootstrap` / `SessionSummary` / discovery helpers | 会话层公共入口 |
| `session_store.py` | `SQLiteSession` / `build_session` / `_message_to_dict` / `_message_from_dict` | SQLite 表 `messages(session_id, seq, payload, created_at)`；`asyncio.to_thread` 包 stdlib sqlite3 |
| `file_session.py` | `FileSession` / `ValidationResult` | append-only JSONL 存储；`manifest.json` 记录 bootstrap；`message_id` / `parent_message_id` 维护链 |
| `session_bootstrap.py` | `SessionBootstrap` | 首次 materialize 写入 manifest 的稳定元数据 |
| `session_discovery.py` | `SessionSummary` / `discover_file_sessions` / `discover_sqlite_sessions` / `find_session_by_id` / `most_recent_session` | 启动前 session 发现和恢复候选选择 |

## 配置

| 配置项 | 默认值 | 谁消费 |
|---|---|---|
| `session.backend` | `"memory"` | `build_session` 分派实现类 |
| `session.store_path` | `".kongming/sessions.db"` | `SQLiteSession` |
| `session.file_store_path` | `".kongming/sessions"` | `FileSession` |

## 参考

- [`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md) — Session 边界
- [`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) — sessions / prompting 目录职责
- `src/core/contracts/` — `Session` Protocol 真源
