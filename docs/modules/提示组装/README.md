# src/prompting/ — 提示组装层

负责把系统指令、运行时上下文、skill listing 和会话历史压缩成模型请求前的输入结构。它只消费 `core.contracts` 的协议，不依赖 tools / executors / safety / host / cli。

## 设计理念

| 决策 | 理由 |
|---|---|
| `InputAssembler` 接管 system 注入 + compact | `Runner` 只推进 turn，输入加工集中在 prompting |
| `HistoryCompactor` 用 head + tail + filtered middle | v1 先防止历史过长；LLM summarize 作为后续独立能力 |
| `InstructionLoader` 读取 agent_spec / extra files / env，`assemble_instructions` 统一收口动态来源 | workflow catalog、skill listing、memory prompt 都在公共入口转成 `InstructionSource` |
| `prompts_loader` 只物化模板文件 | Python prompt assembly 与 `src/prompts/` 纯模板包分离 |
| `SkillSpec` 装配期发现，body 调用期读取 | listing 进 system prompt，正文按需 progressive disclosure |
| Web skill chip 发送前展开为路径链接 | skill 正文已随 listing/system prompt 暴露，单轮引用直接写入 user message content |

## 核心流程

1. `materialize_and_load_prompts(get_kongming_home())` 物化并读取 `.kongming/prompts/{AGENT,TOOLS,USER}.md`。
2. `build_runtime_context_text(cwd, kongming_home)` 生成运行时上下文文本，以 `InstructionSource(origin="runtime")` 注入。
3. 宿主准备动态数据：workflow formatter 输出短 listing，skill loader 输出 listing，CLI 侧可传入已加载的 MemoryStore。
4. `assemble_instructions(...)` 统一生成来源，渲染顺序固定为 `runtime -> pre_file_sources -> workflow_catalog -> agent_spec/prompts -> extra files -> env -> sitian -> skills -> memory`。
5. `InstructionLoader.load()` 收集 prompt 模板、额外文件与 `KONGMING_EXTRA_INSTRUCTIONS`，`render()` 输出带 `# <origin>` 的 system prompt。
6. `InputAssembler.assemble(history, instruction_sources)` 在缺 system 时注入 system message，并调用 `HistoryCompactor` 或 noop compactor。
7. `load_skill_specs(home, workspace, event_sinks)` 双源扫描 SKILL.md，返回 `dict[str, SkillSpec]` 给装配层注册 SkillTool。
8. Web Composer 发送 skill chip 时，将 `skill + inject_context` 展开为 `[$skill-name](.../SKILL.md)` 并写入 user message content；非 skill 的 command/workflow references 继续走结构化 `references`。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|---|---|---|
| `__init__.py` | `HistoryCompactor` / `InstructionLoader` / `InputAssembler` / `SkillSpec` 等 | 提示组装层公共入口 |
| `assembly/input_assembler.py` | `InputAssembler` / `AssembledInput` | 每 turn 组装 LLM 输入，metadata 记录 compact 前后数量和附件引用 |
| `assembly/runtime_context.py` | `build_runtime_context_text` | cwd + kongming_home 的运行时上下文文本 |
| `instructions/instruction_loader.py` | `InstructionSource` / `InstructionLoader` / `assemble_instructions` | 多来源 system prompt 加载与渲染；统一装配 workflow catalog、skill listing、memory prompt |
| `instructions/prompts_loader.py` | `TEMPLATE_FILENAMES` / `materialize_and_load_prompts` | 三段内置 prompt 模板物化与读取 |
| `compaction/history_compactor.py` | `CompactorConfig` / `HistoryCompactor` | 结构化满足 `MessageCompactor` Protocol；截断超长 tool result，保留最近 N 条 |
| `skills/skill_loader.py` | `SkillSpec` / `load_skill_specs` / `format_skill_listing` | 双源 skill 发现、frontmatter 解析、装配期事件 |
| `context_sources/conversation_reference_manager.py` | `ConversationReferenceContext` / `ConversationReferenceManager` / `ResolvedConversationReference` | 兼容后端收到结构化 skill references 时的 prompt context 解析；前端发送路径链接是主链路 |
| `context_sources/sitian_context.py` | `build_sitian_context_text` | 从司天 state/summary 生成额外上下文来源 |

## 配置

| 配置项 | 默认值 | 谁消费 |
|---|---|---|
| `compactor.enabled` | `false` | `SessionEngine.build` 决定装 `HistoryCompactor` 或 noop compactor |
| `compactor.max_messages` / `keep_recent` / `keep_system` / `tool_result_max_chars` | `50` / `20` / `true` / `2000` | `CompactorConfig` |
| `KONGMING_EXTRA_INSTRUCTIONS` | 无默认 | `InstructionLoader.load(include_env=True)` |
| CLI `--instructions-file <path>` | 空 | `InstructionLoader(extra_files=...)` |

## 参考

- [`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md) — Prompt / SessionEngine 边界
- [`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) — sessions / prompting 目录职责
- `src/core/contracts/` — `MessageCompactor` / `AssembledInput` / `InstructionSource` 相关协议和数据结构
