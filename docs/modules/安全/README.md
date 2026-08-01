# src/safety/ — 安全

Safety v0.7 把一次工具调用的审批收敛为 `DangerGuard`、每 cwd 处置模式和当前顶层 thread 的 permissions 本子。对外运行入口保持 `SafetyGatedApproval`；模式状态入口是 `AutoApprovalManager`，权限持久状态入口是 `PermissionsManager`。

## 设计原则

| 原则 | 当前合同 |
|---|---|
| HardBlock 始终拒绝 | `DangerGuard` 位于决策链第一层；`.git/`、凭据路径、指令删除/清空均在处置模式之前返回拒绝。 |
| elevated 始终人审 | `.env*`、`.kongming/**`、`AGENTS.md`、`CLAUDE.md` 的内容编辑保留人工确认。 |
| FORCE_ASK 始终人审 | 递归删除等不可逆命令在 `full_trust` 之前进入 danger Consent，关闭 remember。 |
| 每 cwd 处置模式 | `AutoApprovalManager` 持久化 `user` / `llm` / `full_trust`；`setting.yaml` 只配置 `safety.approval.llm`。 |
| allow/deny 归属于顶层 thread | 每个 thread 拥有独立 JSON snapshot；子 agent 的 session id 只用于执行和审计。 |
| 状态门户唯一 | 跨模块只调用 `PermissionsManager`；`_ThreadPermissionsStore` 是 safety 私有实现。 |
| deny 优先 | `user_approval` 下按 deny → allow → 人工询问决策；同一表达式同时出现时 deny 生效。 |
| 记住动作绑定 pending 身份 | “允许并记住 / 拒绝并记住”只能写入 pending 冻结的 `thread_id`，调用方无法切换目标。 |
| Shell allow 精确绑定 cwd | Shell allow 的身份是 `(expression, scope_cwd)`，命令前缀和 prepared effective cwd 必须同时命中；无 scope Shell deny 对当前 thread 全 cwd 生效。 |
| Shell cwd 单一真源 | `SafetyRequestContext` 让 mode、DangerGuard、permissions、pending、audit 与 ShellTool 统一使用 `execution_scope.cwd`；缺失或相对 scope 直接 HardBlock。 |

## 运行流程

```text
ApprovalRequest
  │ metadata.thread_id；缺失时回落 session_id
  │ execution_scope.cwd = prepared canonical effective cwd
  ▼
DangerGuard.match(request)
  ├─ BLOCK → rejected(source=intrinsic)
  ├─ ELEVATED → ConsentResolver(severity=elevated, remember_allowed=false)
  ├─ FORCE_ASK → ConsentResolver(danger=true, remember_allowed=false)
  └─ ordinary
       ▼
    per-cwd disposition
       ├─ full_trust → approved(source=full_trust, audit_priority=high)
       ├─ llm → default:ask 交 LLM 复核，allow 后进入倒计时
       └─ user
             ▼
        PermissionsManager.resolve(thread_id, request)
             ├─ deny → rejected(source=permissions)
             ├─ allow → approved(source=permissions)
             └─ miss → ConsentResolver(danger=false, remember_allowed=true)
```

`SafetyDecisionEngine` 生成统一审计元数据：`decision_class`、`decision_source`、`matched_rule`、`reason`、`boundary_kind`、`danger`、`remember_allowed`。`SafetyGatedApproval` 捕获内部异常并转换为 `SafetyChainError`。

## 三种审批模式

| 模式 | 普通调用 | permissions | danger |
|---|---|---|---|
| `user` | deny 拒绝、allow 放行、未命中人工询问 | 消费当前 thread 本子 | HardBlock 拒绝，elevated 人审，破坏性命令人审 |
| `llm` | 仅 default:ask 交模型复核 | deny/allow 直接生效，LLM 不参与 | LLM allow 后仍保留用户倒计时拦截；模型异常回落人工审批 |
| `full_trust` | 直接放行并记录 `decision_source=full_trust` | 跳过 deny/allow | HardBlock 拒绝；elevated 与 FORCE_ASK 人审 |

`ApprovalManager` 使用同一 pending 承载 LLM allow 的 `autoApproveAtMs`。用户在倒计时内的 resolve 会终止自动放行；`ApprovalLlmReviewer` 超时、调用失败、非 JSON 或非 allow 输出均保留原 pending。

## Thread permissions

文件路径：

```text
<kongming_home>/safety/thread_permissions/<sha256(thread_id)>.json
```

schema v2 的 JSON 固定字段为 `schema_version`、`thread_id`、`revision`、`allow`、`deny`、`updated_at`。`allow/deny` 元素为 `{expression, scope_cwd}`；文件内真实 `thread_id` 必须与请求身份一致，未知字段、错误 schema、非法 scope 和身份错配均失败关闭。

`PermissionsManager` 提供以下公共方法：

| 方法 | 职责 |
|---|---|
| `snapshot(thread_id)` | 读取 immutable snapshot；缺文件返回 revision 0 的空本子。 |
| `resolve(thread_id, request)` | 解析 canonical DSL 并按 deny → allow 返回命中结果。 |
| `build_entry(...)` / `write_entry(...)` | 校验结构化规则，并基于 pending candidate 与 expected revision 追加 allow/deny。 |
| `replace(...)` | REST 整本 CAS 替换；revision 漂移抛冲突错误。 |
| `delete_thread(thread_id)` | 删除指定 thread 本子。 |

并发策略为 per-thread `asyncio.Lock` 加跨进程文件锁。同 thread 写入串行，不同 thread 独立推进；磁盘操作经 `asyncio.to_thread` 执行，写入采用 fsync、临时文件和 `os.replace`。

DSL 复用 `rule_parser` 的七种 matcher：tool exact/glob、shell prefix、path prefix/glob、MCP exact/glob。文件所属 thread 提供顶层隔离；Shell 规则额外保存 `scope_cwd`，allow 强制 exact canonical cwd，deny 可用 null 表示全 cwd。非 Shell 规则固定使用 null。

## DangerGuard

`DangerGuard` 维护写死危险集，消费绝对凭据路径和 `project_relative` `.git/` block 规则。Shell 请求通过 `SafetyRequestContext` 以 prepared `execution_scope.cwd` 向上定位 Git top-level；非 Shell 请求继续使用 metadata cwd。嵌套目录访问 `../.git/HEAD` 仍会在决策链最前拒绝。保护规则包括：

- `rm` / `mv` 整个 `.git` 目录、任意 `.git/` 原生工具写入；
- `AGENTS.md` / `CLAUDE.md` 的删除、移动或清空；
- `.env*`、`.kongming/**`、指令内容编辑和 thread permissions 扩权的 elevated 人审。

HardBlock 命中直接拒绝；elevated 与 FORCE_ASK 在任何 mode 之前进入人工 Consent，持久记忆动作关闭，前端 danger 卡关闭 Enter/快捷键确认。缺失合法绝对 execution scope 的 Shell 请求命中 `shell-execution-scope-missing` HardBlock。

## Pending 与记忆

`ApprovalManager` 只维护 pending 生命周期、事件 fan-out、resolve/cancel 和记忆写入编排。创建 pending 时冻结 `thread_id`、candidate、revision、danger 与 remember 能力。`remember=true` 的 resolve 必须原样回传 `rememberRule {expression, displayText, scopeCwd}`；Manager 严格比对服务端冻结候选后调用同一 `PermissionsManager` 写入。

删除 thread 时，Web `ThreadManager` 先在 per-thread metadata mutation lock 内确认目标 metadata 存在，再提交 `exists → absent` 并复查文件已经消失，随后按显式 `thread_id` 清理本子。metadata 缺失或删除未提交时保留权限本；清理失败会记录 `thread_permissions_cleanup_failed`、进入重试队列，成功记录 `thread_permissions_cleanup_completed`。Web、CLI、cron 共享目录中的其他 thread 本子保持独立，重命名和归档保留本子。全局垃圾回收需要跨宿主完整 universe，并由独立维护流程负责。

## 代码索引

| 文件 | 门户/职责 |
|---|---|
| `_request_context.py` | frozen `SafetyRequestContext`，统一 mode 与 DangerGuard 的 cwd 真源和 Shell fail-closed。 |
| `approval/chain.py` | `SafetyGatedApproval` / `build_safety_chain`，唯一高层装配入口。 |
| `approval/decision_engine.py` | `SafetyDecisionEngine`，执行三层决策并生成审计元数据。 |
| `approval/permissions_manager.py` | `PermissionsManager`，thread 本子公共门户。 |
| `approval/_thread_permissions_store.py` | 私有 schema v2 JSON store、文件锁、CAS、v1 备份迁移、原子替换与 v2→v1 安全回滚。 |
| `approval/rule_models.py` | frozen `PermissionRuleRecord`、snapshot、migration summary 与 remember candidate。 |
| `approval/permissions_errors.py` | store、schema、identity、revision 冲突错误。 |
| `approval/manager.py` | `ApprovalManager` / `make_manager_prompt_fn`，pending 与 remember 编排。 |
| `approval/events.py` | immutable pending/event 视图。 |
| `approval/rule_parser.py` | canonical DSL parser 与 matcher。 |
| `guards/danger.py` | `DangerGuard` 与固定危险规则。 |
| `guards/consent.py` | `ConsentResolver`，四动作与 remember 约束。 |
| `inbox/event_sink.py` | pending 事件投影到全局 Web inbox。 |
| `approval/llm_reviewer.py` | `ApprovalLlmReviewer`，default:ask 的脱敏 LLM 复核门户。 |
| `auto_approval/` | `AutoApprovalManager`、per-cwd 处置模式与规则配置门户。 |

已删除的旧运行层包括 TrustResolver、GrantStore、BoundaryResolver、CapabilityPolicy、PermissionPolicy、ApprovalRuleManager、RulePersister、RuleSource、RememberScope、RuleScope 与 `RuleBehavior.ASK`。

## 配置与迁移

```yaml
safety:
  approval:
    llm:
      provider: openai_compatible
      model: MiniMax-M2.5
      base_url: https://api.minimaxi.com/v1
      api_key: ""
      timeout_seconds: 15.0
```

loader 对旧 safety 全局规则字段和草案 `safety.permissions` 返回明确错误，并指向定向迁移命令：

```powershell
uv run python scripts/migrate_permissions_v06.py --config <old-setting.yaml> --thread-id <thread-id> --dry-run
uv run python scripts/migrate_permissions_v06.py --config <old-setting.yaml> --thread-id <thread-id> --apply
```

Store 首次读取 schema v1 时在文件锁内备份原文并迁移：非 Shell 规则保留，Shell deny 转为全 cwd deny，缺少历史 effective cwd 的 Shell allow 失效并发出 `permissions.migrated.v2` 审计。迁移脚本只写显式目标 thread；旧 Shell allow 同样安全失效，重复 apply 幂等。回滚 helper 丢弃 scoped Shell allow、保留 deny，并返回损失清单。

## 验证边界

- Manager/store：thread 隔离、exact cwd、重启恢复、deny 优先、revision CAS、v1→v2 迁移/备份/失败回滚、v2→v1 损失清单、并发分桶。
- 决策链：三模式乘 danger/deny/allow/miss 矩阵。
- 宿主合同：root thread_id 在主 agent、子 agent、generic、Claude、Codex 间保持一致。
- Web 合同：schema v2 GET/PUT、409、非法 scope 422、迁移失败 503、`RememberRule.scopeCwd`、显式 thread 删除补偿和跨宿主本子隔离。
- 前端交互：普通四动作、danger 强红与禁快捷确认、thread 切换隔离、加载/空/保存/冲突/失败态。

## 相关文档

- [Safety v0.6 spec](../../spec/safety-approval-three-modes-v0.6/README.md)
- [审批架构导航](../../architecture/approval.md)
- [Web 前端模块](../Web前端/README.md)
- [配置加载模块](../配置加载/README.md)
