# PROVENANCE — regression-v0.1 真题溯源

> 本文件登记每道真题的真实来源，保证可追溯、可重建。真题与 `harness-runtime-v0.1` 构造题物理隔离。

## 登记字段约定

每题 YAML 内嵌 `provenance` 块，本文件汇总：

| 字段 | 含义 |
|------|------|
| `source_fix_report` | 修复报告路径（`reports/fix/`、`reports/p0-`、`reports/bug/`） |
| `buggy_commit` | bug 修复前的 commit（`git show <commit>:path` 取回 buggy 源码） |
| `fix_commit` | 修复 commit（`git show <commit>:tests/...` 取回回归测试） |
| `bug_summary` | bug 机制一句话描述 |
| `fix_summary` | 修复方案一句话描述 |
| `extraction_note` | 最小可运行摘录说明：保留了哪些真实逻辑、剥离了哪些无关实现 |

## 题目清单

### workflow_parallel_self_contained_001

| 字段 | 值 |
|------|------|
| `source_fix_report` | `reports/fix/fix-report-20260701-170000.md` |
| `buggy_commit` | `97cf954d^` |
| `fix_commit` | `97cf954d` |
| `bug_summary` | `ParallelWorkflowStrategy` 持有 `AgentWorkflowManager` 引用并通过 `self._manager.run_parallel_specs()` 反向调用，strategy → manager 反向依赖；manager 认识具体 parallel 类型（遗留 `run_parallel` / `run_parallel_specs` 方法） |
| `fix_summary` | 引入 `WorkflowRuntime` Protocol（依赖倒置），`ParallelWorkflowStrategy` 不再持有 manager，所有引擎层能力经 `context.runtime` 借用 |
| `extraction_note` | 从真实 workflow 短链路（manager + parallel strategy + subagent）摘出最小可运行骨架，保留真实 buggy 机制（strategy 持有 manager、`run()` 转调 `manager.run_parallel_specs`），剥离 provider / network / IO / session / 审计落盘等无关实现。`base_files` 内的 `context.py` / `subagents.py` / `manager.py` 是真实代码的精简骨架，`strategies/parallel.py` 保留真实 buggy 逻辑 |
| `source` | `real_commit:97cf954d`（loader 的 `source` 字段用此标记区分真题与构造题） |

### 验证方式

重建任一题目 buggy 版：

```bash
git show <buggy_commit>:<path>     # 取回 buggy 源码，应与 base_files 一致（骨架题除外）
git show <fix_commit>:<path>       # 取回修复后源码，用于校验 fixture_response
git show <fix_commit>:tests/...    # 取回真实回归测试，用于校验 test_files 断言点
```

骨架题（extraction_note 标注"精简骨架"）：base_files 是真实逻辑的最小摘录，非逐字复制，重点保留 bug 触发点和数据流。
