# tests/manual — 手动测试清单

放需要**人工交互**或**真 LLM** 才能跑的测试。CI **不会**自动跑这里的东西。

## 目录索引

| 文件 | 用途 |
|---|---|
| `ospn_prompt_demo.py` | 不接 LLM、不接 SafetyDecisionEngine，直接看 ospn 4 档审批 prompt UX |

---

# ospn 审批测试 — 4 个层级

`ospn` = `[o]nce / [s]ession / [p]ersist / [n]o` 4 档审批输入（v0.1.4 引入）。底层是 `ApprovalAction` 枚举，上层 UX 实现在 `src/cli/approval.py::build_cli_action_prompt`。

按"看到 prompt 真实长啥样"由浅到深：

## 层级 1 — 手动交互式 demo（2 分钟）

```bash
uv run python tests/manual/ospn_prompt_demo.py
```

演示 2 个场景，每个让你实际输入：

1. **standard** — `[y]=once  [s]=session  [p]=persist  [n]=no` 三按钮 UX
2. **elevated** — 要求输入 `confirm_token=a3f7c2e0`（8 位 hex；真实 token 由 `sha256(call_id + matched_rule)[:8]` 派生）

**适合**：第一次想看 prompt 长啥样、试 4 档输入的回显行为。

---

## 层级 2 — CLI prompt 单元测试（自动化，patch stdin）

`tests/unit/test_cli_approval.py` 共 17 个用例，模拟用户输入各字符看 prompt 怎么处理。

### 整文件

```bash
rtk uv run pytest tests/unit/test_cli_approval.py -v
```

### 单档行为

| 命令 | 测什么 |
|---|---|
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_y_returns_accept_once -v` | `y` → ACCEPT_ONCE |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_s_returns_accept_for_session -v` | `s` → ACCEPT_FOR_SESSION |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_p_with_confirmed_persist -v` | `p` + 二次确认 `y` → ACCEPT_PERSIST |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_p_with_rejected_persist_downgrades -v` | `p` + 二次确认 `N` → 降级到 SESSION |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_n_returns_reject -v` | `n` → REJECT |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_empty_input_treated_as_reject -v` | 空回车 → REJECT |
| `pytest tests/unit/test_cli_approval.py::test_tty_standard_invalid_input_loops_until_valid -v` | 垃圾字符（`abc`）→ 循环再问 |

### elevated 行为

| 命令 | 测什么 |
|---|---|
| `pytest tests/unit/test_cli_approval.py::test_tty_elevated_correct_token_returns_accept_once -v` | token 正确 → ACCEPT_ONCE |
| `pytest tests/unit/test_cli_approval.py::test_tty_elevated_wrong_token_rejects_no_retry -v` | token 错 → 立刻 REJECT，不重试 |
| `pytest tests/unit/test_cli_approval.py::test_tty_elevated_missing_token_rejects_safely -v` | 完全没传 token → REJECT（fail-safe）|
| `pytest tests/unit/test_cli_approval.py::test_tty_elevated_does_not_offer_session_or_persist -v` | elevated 不允许 `[s]/[p]` |

### 非 TTY / 异常路径

| 命令 | 测什么 |
|---|---|
| `pytest tests/unit/test_cli_approval.py::test_non_tty_y_returns_accept_once -v` | 重定向 stdin 时只接受 y/n |
| `pytest tests/unit/test_cli_approval.py::test_eof_treated_as_reject -v` | Ctrl-D → REJECT |
| `pytest tests/unit/test_cli_approval.py::test_keyboard_interrupt_treated_as_reject -v` | Ctrl-C → REJECT |

---

## 层级 3 — 底层协议单元测试（不依赖 CLI）

`tests/unit/test_tools_approval_action.py` — 测 `ApprovalAction` 枚举到 `ApprovalDecision` 的映射，跟 UX 无关。

```bash
rtk uv run pytest tests/unit/test_tools_approval_action.py -v
```

5 个 TestXxx 类：

| 命令 | 测什么 |
|---|---|
| `pytest tests/unit/test_tools_approval_action.py::TestBoolPromptCompat -v` | 老 yes/no prompt 是否还兼容 |
| `pytest tests/unit/test_tools_approval_action.py::TestActionPrompt -v` | 4 档 ApprovalAction 全矩阵 → ApprovalDecision |
| `pytest tests/unit/test_tools_approval_action.py::TestElevatedMetadata -v` | elevated metadata 透传 |
| `pytest tests/unit/test_tools_approval_action.py::TestEdgeCases -v` | None prompt_fn / dataclass instance 等 |
| `pytest tests/unit/test_tools_approval_action.py::TestBuildDefaultApproval -v` | `build_default_approval` 工厂 |

---

## 层级 4 — 真 CLI 接 LLM（最完整，但要本地模型）

### 准备

`config/setting.yaml` 或 `.env` 里 `KONGMING_MODEL_BASE_URL` 指向能用的本地 LLM；`approval.mode` 保持默认 `interactive`。

### 启动

```bash
./start.sh cli
# 或带工作目录
./start.sh -w ~/Notes cli
```

### 必触发 ospn prompt 的对话剧本

| 对 LLM 说什么 | 期望 prompt 类型 | 现象 |
|---|---|---|
| `在 docs/ospn-test.md 写一行 hello` | standard | `[y][s][p][n]` 4 档 |
| `跑 shell 命令 ls -la` | standard | 同上（run_shell 必触发）|
| `把 .env 文件覆盖成 FOO=bar` | **elevated** | `confirm_token=xxxxxxxx` typed-confirm |
| `修改 CLAUDE.md 加一行` | **elevated** | 同上 |
| `在 ~/scratch/note.md 写 hello` | standard（项目外）| 4 档 |
| `读 ~/.ssh/config` | **hard_block** | 没 prompt，直接 deny |

### 4 档真实交互演练

```
LLM: write_file docs/ospn-test.md
prompt: 允许？[y]=once  [s]=session  [p]=persist  [n]=no  >

→ y / o：写入，下次同类还问
→ s：    写入 + 内存里记 (file_write, "/Volumes/.../docs/")，本进程同前缀不再问
→ p：    问 "Confirm? [y/N]"
         回 y：写入 + 写 .kongming/grants.jsonl 持久化
         回 N：降级 session（不写盘）
→ n：    拒绝执行
```

### 验证 grant 真写盘了

```bash
# session grant：看 trace
tail -f .kongming/trace.jsonl | grep silently_allowed

# persist grant：看磁盘
cat .kongming/grants.jsonl

# 全清 persist grant
rm .kongming/grants.jsonl
```

### 看默认放行的 trace（需开关）

```bash
export KONGMING_SAFETY_LOG_SILENT_READS=true
./start.sh cli
# 或 config/setting.yaml 里设 safety.log_silent_reads: true
```

之后 read 类的 silent_allow 也会写 trace，你能看到完整决策链。

---

# 一键全跑（自动化部分）

跑完所有 ospn 相关自动化测试（不含手动 demo / 真 LLM）：

```bash
rtk uv run pytest tests/unit/test_cli_approval.py \
                  tests/unit/test_tools_approval_action.py \
                  tests/e2e/test_safety_decision_engine.py \
                  tests/e2e/test_approval_ask.py \
                  tests/e2e/test_permission_deny.py \
                  -v
```

40+ 用例，约 1.5 秒。

---

# 推荐试错顺序（5 分钟）

1. `uv run python tests/manual/ospn_prompt_demo.py` — 看 prompt 长啥样
2. `rtk uv run pytest tests/unit/test_cli_approval.py::test_tty_standard_p_with_confirmed_persist -v` — 看 persist 路径自动跑通
3. `./start.sh cli` 让 LLM 跑一句 `创建 docs/test.md 写 hello` — 真实 prompt 弹给你
4. `cat .kongming/grants.jsonl` — 看选了 `p` 后的持久化结果

---

# 默认放行的"白名单"参考

ospn prompt **不会**对下面这些操作出现（它们直接 silent_allow）：

| # | 触发条件 | 决策来源 |
|---|---|---|
| 1 | `read_file` / `list_dir` 任何路径 | `intrinsic`（DEFAULT_ALLOW_TOOLS_SILENT）|
| 2 | 写 git tracked 文件 | `intrinsic`（BoundaryResolver TRUSTED zone）|
| 3 | 读/写 `.kongming/work/` `.kongming/tmp/` | `intrinsic`（DEFAULT_TRUSTED_WORKDIRS）|
| 4 | 用户自己加的 `safety.allow_writes` / `safety.trusted_workdirs` | `config` / `intrinsic` |

**绝对不放行**：

- 任何 `run_shell` 命令（即使是 `ls`）
- 写 untracked 项目文件 / 项目外路径
- 写 `.env*` / `CLAUDE.md` / `AGENTS.md` / `.kongming/config*`（elevated）
- 读/写 `~/.ssh/` `~/.gnupg/` `~/.aws/credentials` 等（hard_block）
- `rm -rf /` / `dd of=/dev/sd*` / 管道注入（hard_block）

详见 [`docs/modules/安全/README.md`](../../docs/modules/安全/README.md) 的"配置"和"核心流程"两节。
