# src/memory/ — 长期记忆

跨会话长期记忆的读写底层：多文件快照 + 活态条目双分区、内容安全扫描、原子写入。不直接对 agent 暴露；调用方（CLI 装配、MemoryTool、MemoryRefreshSink）通过 import 本包消费。

## 设计理念

| 决策 | 理由 |
|------|------|
| `memory/` 只 import `core.contracts`，不依赖任何 sibling | 同 `sessions/` / `prompting/` 一样的纯消费层约束；`tools/memory_tool.py` / `hosts/shared/memory_refresh_sink.py` / `hosts/cli/main.py` 单向消费 memory，memory 不反过来认识他们 |
| 冻结态 snapshot + 活态 entries **双分区** | system prompt 里注入的是冻结 snapshot（普通 turn 稳定），memory tool 操作的是活态 entries（写后立即可读）；两者只在 `load_from_disk()` 时对齐，避免"写一条就动 prompt"导致 provider 缓存反复失效 |
| `load_from_disk()` 是唯一冻结态刷新入口 | 启动时一次、`history.compact` 后由 `MemoryRefreshSink` 触发一次；不走其它隐式路径，便于 trace 归因 |
| 写入路径永远经过 `safety_write.execute_write` | 内容扫描（prompt injection / secret / 不可见 Unicode / 控制字符）是第一道防线；绕过它直接 `path.write_text` 的话第二次 import 就会被 scanner 拒回 |
| 写入用 `tempfile + os.replace()` 原子替换 | 写失败时不破坏旧文件；Windows 下用 `delete=False` 避免 rename 冲突 |
| Agent 只能通过 `MemoryTool.target` 参数（memory/user/errors）访问记忆 | 防止 Agent 用 `write_file` / `shell` 手动建 MEMORY.md 这类"野文件"——手写文件不会被框架识别，也不会进下一次 prompt |

## 核心概念

### 三个分区文件

全部位于 `.kongming/memory/`（默认，可由 `evolution.memory.root_path` 配置成绝对路径）：

| 文件 | 分区 | 用途 | 注入 system prompt？ |
|------|------|------|----------------------|
| `MEMORY.md` | `memory` | agent 自己的笔记（工作约定、环境事实、工具路径） | ✅ |
| `USER.md` | `user` | 用户画像（偏好、角色、当前目标） | ✅ |
| `ERRORS.md` | `errors` | 错误模式与修复记录 | ❌（仅 MemoryTool 可访问） |

条目之间用 `ENTRY_DELIMITER = "\n§\n"` 分隔（与 Hermes 一致）。

### 冻结态 vs 活态

- **冻结态 `MemorySnapshot`**：`load_from_disk()` 捕获一次，含 `memory_text` / `user_text` / `captured_at_ms` / `checksum` (`sha256:<hex>`)，`render_prompt()` 输出 `══...MEMORY══` 包裹的 block 供 system prompt 注入。
- **活态 `MemoryEntry[]`**：按 `ENTRY_DELIMITER` 拆分，去重保序。MemoryTool 写入后 `refresh_entries_for(target, ...)` 立即更新活态，冻结快照保持不变。

### 写入动作三件套

`MemoryWriteAction` 是统一写入描述：

- `add(target, content)` — 追加一条；entry-level exact duplicate 返回 `skipped`
- `replace(target, old_text, new_text)` — 第一处匹配替换；`old_text` 不存在返回 `not_found`
- `remove(target, text)` — 第一处匹配删除；自动清理 3+ 连续换行到 2 个

## 核心流程

### 启动阶段（CLI 装配）

```
cli/main.py _assemble_instructions
  └─ MemoryStore(memory_dir=..., read_max_chars=...)
     └─ await store.load_from_disk()         # 创建目录 + 读三文件 + 捕获快照
  └─ if inject_prompt:
       snapshot.render_prompt()
         → InstructionSource(origin="memory", content=...)
         → 汇入 InstructionLoader.render()
```

### Agent 调用 MemoryTool（一次 add）

```
MemoryTool.execute(action=add, target=memory, content=...)
  └─ execute_write(memory_dir, MemoryWriteAction, event_sinks, run_id)
       ├─ scan_content(content)              # prompt injection / secret / 不可见 Unicode
       ├─ _read_file(target_path)            # 重读磁盘最新状态
       ├─ _do_add → _atomic_write(tmp, replace)
       └─ _emit_write_event → memory.write.{success,rejected,error} 入 trace
  └─ store.read_target(target) → refresh_entries_for  # 活态对齐磁盘
```

### `history.compact` 后快照刷新（`MemoryRefreshSink`）

由 `hosts/shared/memory_refresh_sink.py` 承接：订阅 `kind=history.compact`，命中时调 `store.load_from_disk()`，再向 downstream sinks emit `memory.snapshot.refreshed`（带新 checksum + 字符数）。失败静默（load 失败不污染 runner fan-out）。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `__init__.py` | `ENTRY_DELIMITER` / `MEMORY_MAX_CHARS` / `USER_MAX_CHARS` / `MemoryEntry` / `MemorySnapshot` / `MemoryStore` / `MemoryTarget` / `MemoryWriteAction` / `MemoryWriteResult` / `execute_write` / `scan_content` | 包门面。严格约束写在 docstring：不 import 任何 sibling |
| `store.py` | `MemoryStore` / `MemorySnapshot` / `MemoryEntry` / `MemoryWriteAction` / `MemoryWriteResult` / `MemoryTarget` / `ENTRY_DELIMITER` / `MEMORY_MAX_CHARS` / `USER_MAX_CHARS` | 多文件读取 + 冻结快照 + 活态条目；`load_from_disk()` / `format_for_system_prompt()` / `refresh_entries_for()` / `read_target()` |
| `safety_write.py` | `execute_write` / `scan_content` | 安全写入内核：扫描 → 重读 → 原子写入 → emit event。`_PROMPT_INJECTION_PATTERNS` / `_SECRET_PATTERNS` / `_UNAUTHORIZED_SYSTEM_PATTERNS` / `_INVISIBLE_UNICODE_RE` / `_CONTROL_CHAR_RE` 五组扫描规则 |

## 配置

消费项来自 `infrastructure.config.models.EvolutionMemoryConfig`（`config.evolution.memory`）：

| 配置项 | 默认值 | 消费者 |
|--------|--------|--------|
| `evolution.memory.enabled` | `true` | CLI 在 `_assemble_instructions` 决定是否加载 memory、是否注册 MemoryTool / MemoryRefreshSink；MemoryTool 调用继续经过统一 SafetyDecisionEngine。 |
| `evolution.memory.root_path` | `".kongming/memory"` | 解析成绝对 `memory_dir`（支持 `~` 展开），传给 `MemoryStore(memory_dir=...)` |
| `evolution.memory.inject_prompt` | `true` | false 时仍加载活态 entries 供 MemoryTool 使用，但不向 system prompt 追加 `InstructionSource(origin="memory")` |
| `evolution.memory.read_max_chars` | `65536` | 单文件读取字符上限；防异常巨大的 memory 文件吃掉 prompt 预算 |
| `evolution.memory.view_max_chars` | `8000` | `MemoryTool.view` 返回的最大字符数 |

env 覆盖白名单：`KONGMING_EVOLUTION_MEMORY_ENABLED` / `_ROOT_PATH` / `_INJECT_PROMPT` / `_READ_MAX_CHARS` / `_VIEW_MAX_CHARS`。

## 事件契约

- `memory.snapshot.captured` — CLI 启动一次性 emit（源头：`hosts/cli/main.py`，不在本模块内 emit）
- `memory.snapshot.refreshed` — `MemoryRefreshSink` 响应 `history.compact` 后 emit
- `memory.write.success` / `memory.write.rejected` / `memory.write.error` — `safety_write.execute_write` 统一 emit（status→kind 映射在 `_STATUS_TO_EVENT_KIND`）

这四个 kind 均已登记在 `core.contracts.EventKind`。

## 已知问题 / 待完成

- **单进程原子替换**：v0.1.3 用 `os.replace()` 实现进程内原子性，未上 `flock`；多进程同时写入可能出现最后一个赢家覆盖前者。当前只有 CLI 单进程调用 MemoryTool，暂不是问题。
- **条目去重只做 entry-level exact match**：语义相同但措辞不同的条目会各自存一份，后续可加模糊去重或 LLM 辅助合并。
- **扫描规则粗粒度**：`_UNAUTHORIZED_SYSTEM_PATTERNS` 专门放过 "macOS system:" 这类常见记忆内容，但 prompt injection / secret 的 regex 都是经验式；未来可换结构化 scanner。
- **分区上限只做 usage 显示**：`MEMORY_MAX_CHARS` / `USER_MAX_CHARS` 目前只在 `view` / `render_prompt` 中显示使用率，不强制截断。超过上限仍可写入；靠 agent 自己在 prompt 里读到 "100%" 触发整理。

## 参考

- 设计文档：[`docs/spec/self-evolution-v0.1.3/README.md`](../../spec/self-evolution-v0.1.3/README.md)（v0.1.3 Memory Snapshot 冻结快照方案）
- 协议：`core.contracts.EventSink` / `Event` / `EventKind`（memory.write.* / memory.snapshot.*）
- 调用方：[`src/tools/builtin/memory_tool.py`](../../../src/tools/builtin/memory_tool.py) / [`src/hosts/shared/memory_refresh_sink.py`](../../../src/hosts/shared/memory_refresh_sink.py) / [`src/hosts/cli/main.py`](../../../src/hosts/cli/main.py) `_assemble_instructions`
- 灵感源：`other/hermes-agent/` 的冻结快照模型
