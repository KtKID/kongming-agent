# Safety v0.6 审批测试指南

本指南面向本地手工验证与自动化用例维护。运行真源为 `DangerGuard(BLOCK/ELEVATED/FORCE_ASK) → approval mode → thread permissions → Consent`。

## 准备

全局配置：

```yaml
safety:
  approval_mode: user_approval
  auto_judge:
    model_preset: null
```

thread 本子路径：

```text
<kongming_home>/safety/thread_permissions/<sha256(thread_id)>.json
```

优先通过 Web `GET/PUT /api/threads/{thread_id}/permissions` 或审批卡“记住”动作写入。直接编辑 JSON 会绕过 revision CAS 与门户校验。

## 决策预期

| 场景 | 结果 | 关键证据 |
|---|---|---|
| danger + 任意模式 | 强制人工审批 | `danger=true`、`remember_allowed=false` |
| FORCE_ASK + full_trust | 强制人工审批 | 无 `approval.full_trust.auto_allow`，用户决定前工具无副作用 |
| full_trust + 普通调用 | 静默放行 | `decision_source=full_trust` |
| user_approval + deny 命中 | 拒绝 | `decision_source=permissions`、`matched_rule` |
| user_approval + allow 命中 | 静默放行 | `decision_source=permissions`、`matched_rule` |
| user_approval + 未命中 | 人工审批 | pending 固定 `thread_id` 与 remember candidate |
| auto + 普通调用 | 告警并按 user_approval 执行 | fallback 告警事件 + 后续 permissions/Consent 结果 |

同一表达式同时存在 allow 与 deny 时，deny 生效。thread A 的表达式对 thread B 无影响；同一 root thread 的子 agent 继承 A 的本子。

Shell effective cwd 测试必须在同一请求使用冲突哨兵：`metadata.cwd=A`、`execution_scope.cwd=B`。mode resolver 记录并断言收到 B；DangerGuard 从 B 解析相对路径；pending、audit 和 ShellTool data 继续显示 B。A=`full_trust`、B=`user` 时，用户批准前 B 目录不得出现 subprocess 副作用。

## 人工交互

普通卡提供四个动作：

- 允许一次：`allow=true, remember=false`；
- 允许并记住：`allow=true, remember=true`；
- 拒绝一次：`allow=false, remember=false`；
- 拒绝并记住：`allow=false, remember=true`。

danger 卡显示强红状态，只提供一次性允许/拒绝，Enter 与快捷键不会确认。

记住后检查：

1. pending 所属 thread 的 revision 增加；
2. allow 或 deny 出现 canonical DSL；
3. 同 thread 再次请求命中 permissions；
4. 另一 thread 继续产生 pending；
5. 重启进程后目标 thread 继续命中。

## REST 验证

```text
GET /api/threads/{thread_id}/permissions
PUT /api/threads/{thread_id}/permissions
```

PUT body 固定包含 `thread_id`、`revision`、`allow`、`deny`。路径与 body 身份不一致返回 422，stale revision 返回 409，未知字段返回 422。冲突后再次 GET，磁盘和内存 snapshot 应保持已提交 revision。

## 删除与补偿

删除 thread 时检查：

- DELETE 主状态返回成功；
- 正常路径删除本子文件并记录 `thread_permissions_cleanup_completed`；
- 注入 store 错误后 DELETE 仍成功，本子进入 `pending_permissions_cleanup`；
- 后台重试或显式 `retry_permissions_cleanup()` 成功后文件消失；
- 服务启动时 `cleanup_orphans()` 删除无 metadata 的本子；
- 重命名与归档期间文件和内容保持不变。

## auto 断开验证

CLI、Web run、`ApprovalRuntimeManager` 均不注入旧 cwd 倒计时 policy。generic 与 Claude WS 收到旧 `auto-approval-toggle` / `auto-approval-query` 后返回：

```json
{
  "frame_type": "error",
  "error_code": "feature_disabled",
  "reason": "auto_approval_disabled"
}
```

前端不展示旧开关。`src/safety/auto_approval/` 与 `src/hosts/web/approvals/auto/` 仅作为未来 auto judge 插槽保留。

## 自动化入口

```powershell
uv run pytest tests/unit/safety/test_permissions_manager.py tests/unit/safety/test_thread_permissions_store.py tests/unit/safety/test_danger_guard.py tests/unit/test_safety_chain.py -v
uv run pytest tests/unit/safety/test_safety_contract_redlines.py tests/integration/safety/test_effective_cwd_single_source.py -v
uv run pytest tests/unit/web/test_thread_permissions_router.py tests/unit/test_web_protocol_round_trip.py -v
uv run pytest tests/unit/web/test_thread_permissions_lifecycle.py -v
uv run pytest tests/e2e/test_thread_permissions_isolation_e2e.py tests/e2e/test_safety_approval_modes_e2e.py tests/e2e/test_thread_permissions_lifecycle_e2e.py -v
```

前端：

```powershell
Set-Location web
npm run test:unit
npm run typecheck
```

静态与架构验证：

```powershell
uv run ruff check .
uv run mypy src scripts/migrate_permissions_v06.py
uv run lint-imports --config .importlinter
```

cron/scheduler 衔接属于后续阶段；当前非 cron 回归命令保持排除 scheduler 专属用例。
