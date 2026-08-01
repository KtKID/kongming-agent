# kongming-agent

<p align="center">
  <img src="assets/logo.png" width="320" alt="kongming-agent logo">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/status-Alpha-orange.svg" alt="Status"></a>
</p>

> An async-first general-purpose agent kernel — a single run loop layered with LLM-selected multi-agent workflow orchestration, with a built-in proactive observer (SiTian), a self-evolution evidence chain, and a three-layer safety chain.

- **More than a coding agent** — the kernel is scene-agnostic; the tool layer can connect file / shell / memory / skill / MCP servers; desktop/mobile hosts via sidecar, computer use in development
- **The LLM picks its own orchestration strategy** — invoke `run_agent_workflow` mid-conversation and choose from parallel / map_reduce / deep_research / roundtable_review / task_flow to split a goal across sub-agents in independent sessions
- **SiTian: the agent that observes proactively** — without waiting for your prompt, it scans the workspace in the background, materializes `work_item`s and next-step suggestions
- **Self-evolution** — after each conversation, an evidence-window review runs in the background, writing reviews and evolution nutrients (memory / workflow / error) back into long-term memory
- **Three-layer safety chain + thread ledger** — DangerGuard → approval mode → per-thread allow/deny; cron tasks can also declare their own approval mode
- **Model-agnostic, zero-cost local start** — supports OpenAI-compat / Anthropic; local presets automatically skip remote key validation
- **Executable engineering discipline** — import-linter enforces architecture boundaries, single source of truth for contracts, mandatory `@override`, commit+push two-tier hooks

---

## Quick Start

### 1. Install dependencies

```bash
cd /path/to/kongming-agent
uv sync --all-extras
```

Or use the unified entry script:

```bash
./start.sh install        # macOS / Linux
```

> Windows has no equivalent entry script yet; use `uv sync --all-extras`.

### 2. Configure model key

Models are selected via a **preset catalog** (`config/model-providers.yaml`), no more hand-assembled base_url + model name. **The default preset is `local-gemma-4-e4b-it` (local)** — to work out of the box you need to start an OpenAI-compatible service listening on `127.0.0.1:62000` (LM Studio / Ollama / vLLM all work); local presets automatically skip remote key validation. To use a remote model, switch to `minimax-m3` / `deepseek` / `bigmodel-glm5-1m` and configure the corresponding provider key.

```bash
cp .env.example ~/.kongming/.env
# Edit ~/.kongming/.env and fill in real keys (only needed for remote presets):
#   MINIMAX_API_KEY=...
#   GLM_API_KEY=...
#   DEEPSEEK_API_KEY=...
```

API keys never go into `config/setting.yaml` (it would be committed to Git); put them only in the runtime home `.env` (default `~/.kongming/.env`), referenced by each provider's `key env` in the catalog. Real process env takes precedence; `.env` never overwrites already-set values. Production / CI can skip `.env` and inject the same env vars directly from a secret store.

### 3. Run

```bash
./start.sh cli                            # default local-gemma-4-e4b-it preset (local model)
./start.sh cli --model-preset minimax-m3  # switch to remote minimax-m3 (requires MINIMAX_API_KEY)
make smoke                                # smoke-test the full chain
```

> **CLI status**: The CLI host (`src/hosts/cli/`, based on click + prompt_toolkit) currently receives little maintenance and is planned for refactoring. It is only a synchronous shell — the agent loop, tools, safety chain, and sessions all live in the kernel and `application/`; refactoring the CLI does not affect core capabilities or the Web host. **For production / long-term use, prefer the Web host** (FastAPI + React + WS, multi-thread chat + global approval inbox). The CLI examples below remain CLI-focused because it's the fastest way to validate the kernel.

`make smoke` runs two steps: `--workflow-smoke` (no model, validates the workflow entry, approval chain, and map_reduce planner) + `--smoke` (assembly + one real model request). Note the smoke script hardcodes `--model-preset minimax-m3`, so the `--smoke` step **requires `MINIMAX_API_KEY`**; without a key you can run `bash scripts/smoke.sh` alone to validate only the parts that don't connect to a model. Seeing `[smoke] ok status=completed` means the full chain — config load → catalog resolution → provider → runner → session → approval — works.

Common flags:

```bash
./start.sh cli --verbose                  # print turn/tool/approval event progress
./start.sh cli --session-id demo          # reuse a session
./start.sh cli --show-reasoning           # print model reasoning each turn
./start.sh cli --reasoning-effort high    # reasoning depth none/low/medium/high/max
./start.sh cli --list-sessions            # list existing sessions
./start.sh cli --resume-last              # resume the most recent active session
```

### Switch models

Switching models = switching presets (endpoint and key are declared in the catalog):

```bash
./start.sh cli --model-preset deepseek
KONGMING_MODEL_PRESET_ID=bigmodel-glm5-1m ./start.sh cli   # GLM provider default preset (glm-5.2)
```

Custom providers / models: copy the built-in catalog to `<kongming_home>/model-providers.yaml` (default `~/.kongming/`) and edit it; a provider with the same name fully replaces the built-in definition. See [config/README.md](config/README.md).

### Approval modes

Default `interactive`, asks `[y/N]` on every tool call:

| Mode | Behavior | Use case |
|---|---|---|
| `interactive` | Manual confirm (default) | Daily use |
| `auto_allow` | Auto-allow | Automation scripts, unattended (**mind the shell tool risk**) |
| `auto_deny` | Auto-deny | Stress-testing the deny branch, model chat without touching tools |

```bash
KONGMING_APPROVAL_MODE=auto_allow ./start.sh cli
```

> cron scheduled tasks have an independent task-level `approval_mode` (`trust` / `fail_closed`), separate from the CLI interactive approval. See [docs/modules/定时任务/](docs/modules/定时任务/README.md).

### Troubleshooting

| Symptom | Check |
|---|---|
| Can't connect / auth failed | For remote presets, check the provider key in `~/.kongming/.env`; for local presets, confirm an OpenAI-compatible service is listening on `127.0.0.1:62000` |
| Model / preset not found | `preset_id` in `model-providers.yaml` is the source of truth — spelling must match; custom catalog goes in `~/.kongming/model-providers.yaml` |
| `HTTP 4xx` auth | Provider-specific key not injected or expired; local models (127.x / localhost) skip remote key validation |
| Garbled output | Terminal encoding isn't utf-8, `export LANG=en_US.UTF-8` |

---

## Differentiating capabilities

These four directions are where kongming-agent truly separates itself from similar projects.

### SiTian: the agent that observes proactively

Most agents are **reactive** — you ask, they answer. SiTian (`src/sitian/`) makes the agent work while you're away: it proactively scans the workspace per config, materializes global state, and gives next-step suggestions based on that state, optionally calling an LLM for analysis. Currently it must be invoked manually (`run-once` / `loop`); the auto-trigger mechanism is in the [Roadmap](#roadmap) below.

- **4 source kinds**: generic_channel / claude_project / codex_project / claude_workspace, covering this repo and external agent project directories
- **Three layers of artifacts**: `observations.jsonl` (append-only raw observations) → `workspace_state.json` (overwrite-written merged state) → `latest_suggestions.json` + `latest_summary.md` (next-step suggestions)
- **work_item merging**: collapses scattered observations into actionable work_items, so each scan doesn't start from scratch
- **Three triggers**: CLI `run-once` / `loop` / `state`, the wrapper script `./sitian.sh`, or the config file `config/sitian.local.yaml`

Both platform entry points are equivalent — wrappers around `uv run kongming-sitian {run-once|loop|state}` (default config `config/sitian.local.yaml`, artifact root `<kongming_home>/sitian/`):

```bash
# macOS / Linux (sitian.sh, includes scan/state/loop/summary/clean; clean has accidental-delete protection)
./sitian.sh scan             # run-once + state + summary
./sitian.sh loop             # continuous scan, Ctrl+C to exit
./sitian.sh clean            # clear the artifact directory
```

```powershell
# Windows (SiTianRun.ps1)
.\SiTianRun.ps1              # run-once + state + print latest_summary.md
.\SiTianRun.ps1 -Action loop # continuous scan
```

### Multi-agent workflow: the LLM picks its own orchestration

`src/application/` turns orchestration strategies into tools. The main agent calls `run_agent_workflow` mid-conversation, picks a strategy, and splits the goal across multiple sub-agents to collaborate. Each sub-agent gets an independent session, an independent model snapshot, and tightened tool permissions; output lands in `agent-workflows/<workflow_id>/` (with audit logs, sub-reports, and the final `result.json`).

| Strategy mode | Name | Use case |
|---|---|---|
| `parallel` | Parallel subtasks | Dispatch multiple independent subtasks simultaneously, aggregate a report once all return |
| `map_reduce` | Map-Reduce code analysis | Split a large codebase analysis into stable shards; mapper sub-agents analyze, a deterministic reducer merges `code_findings` |
| `deep_research` | Deep Research workflow | Plan search lines around a research question, collect sources, extract facts, cross-check, generate a cited report |
| `roundtable_review` | Multi-agent roundtable review | Review module designs in parallel by role; query and arbitrate via a shared ReviewBoard; output consensus / dissent / risk |
| `task_flow` | Task Flow | General-purpose plan execution: split a goal into visible steps and complete them progressively, supports multi-option user confirmation; the catch-all layer when the above strategies don't fit |

Key boundaries:

- **Reuses the single run loop**: sub-agents also go through `core.Runner.run()`; no separate orchestration engine — workflow strategies only split, dispatch, and aggregate
- **Tool capability monotonically tightens**: a sub-agent's actually-available tools = `parent's actual tools ∩ requested tools ∩ scope-allowed tools`; defaults to inheriting the parent set, an explicit empty set keeps zero tools
- **Lifecycle is cancellable**: `ActiveWorkflowHandle` holds the underlying `asyncio.Task`; you can `cancel_workflow(id)` individually without having to cancel the entire parent run

The strategy catalog, parameter descriptions, and example payloads can be read by the LLM via the same tool's `list` / `describe` operations; adding a new strategy only requires registering a `WorkflowStrategy` implementation. Beyond workflows, the main agent can also spawn a single sub-agent directly via `spawn_subagent` (e.g., for code review / research), going through the same `AgentManager.spawn()` gateway.

### Self-evolution: review after each conversation

`src/evolution/` runs a background review after the main conversation ends: it trims a terminal-state evidence window, forks a child reviewer to do the review, and writes the review record and evolution nutrients back into long-term memory for the next conversation.

- **Three nutrient kinds**: `memory` (facts worth remembering long-term) / `workflow` (orchestration improvement points) / `error` (pitfalls hit)
- **Three triggers**: cadence auto-trigger, the main agent explicitly calling the `request_evolution_review` tool, the Web `/evolve` command
- **Five layers of artifacts**: `reviews/` + `evolution-nutrients.jsonl` + `decisions/` + `apply-jobs/` + `evolution.state.json`, all under `<kongming_home>/evolution/`
- **Single write entry point**: the private write tool `evolution_write`, structured persistence, not bypassable

### Three-layer safety chain + thread ledger

`src/safety/` collapses the approval of each tool call into a unified decision chain — the permission boundary for tool execution.

```
DangerGuard (hardcoded dangerous-set backstop)
  → Approval mode (user / llm / full_trust, configurable)
    → thread permissions ledger (per-thread independent allow/deny)
```

- **DangerGuard**: a hardcoded set of dangerous operations, not bypassable in any mode
- **Approval mode**: `user` (manual confirm) / `llm` (LLM review) / `full_trust` (auto-allow), configurable per cwd
- **Thread ledger**: `PermissionsManager` saves allow/deny per thread to `<kongming_home>/safety/thread_permissions/<sha256(thread_id)>.json`; sub-agents inherit the root thread_id
- **Task-level approval**: cron scheduled tasks can declare their own `approval_mode` (`trust` / `fail_closed`); if undeclared, the global default `trust` applies
- **Three layers of evidence**: approval decisions, permission reads/writes, and audit events are persisted separately, derivable into usage / audit

### Claude Code / Codex SDK integration (experimental)

Beyond the generic_chat mainline, the Web host connects two external agent channels as peers, validating the kernel's provider / transport extension boundary — the same thread, approval inbox, state machine, history normalization, and usage-stats framework can mount completely different agent backends:

- **Claude Code channel**: `src/hosts/web/integrations/claude_code/`, invoked **in-process** via `claude_agent_sdk`, over the `/ws/claude-code` WebSocket; normalizes SDK streaming messages into `NormalizedMessage` and bridges its `can_use_tool` callback to the global approval inbox
- **Codex channel**: `src/hosts/web/integrations/codex/`, driven by spawning a `codex exec --json` **subprocess**, over the `/ws/codex` WebSocket, with full pydantic wire frames (`CodexCommandFrame` / `CodexC2S` / `CodexS2C` unions)

The frontend has a corresponding three-way `ChatProvider` registry (`generic` / `claude` / `codex`), routing by the thread's `backend_kind` to different main views (`ClaudeCodeView` / `CodexView` / generic MessageList); normalized messages reuse the same `ChatEvent` rendering.

> **Status**: the underlying integration (backend WS channels + frontend providers + views) is complete and can serve as a reference implementation for "driving external agents within a unified framework." But the persistent tab-switching UI (`LeftSidebarTabs.tsx`) is written yet not wired into the main UI — currently you switch by selecting `backend_kind` when creating a session. This area receives limited maintenance overall and is positioned as an **experimental capability**; it will be refined as the channel protocol is consolidated.

---

## Architecture overview

```
src/core/                  ← agent runtime semantics + cross-module protocol source of truth + single runner (the base of all capabilities)
src/application/           ← application orchestration: workflows / sub-agent tree / scheduled-run bridge / agent roles
src/tools/                 ← ToolRegistry + builtin tools + memory / skill / schedule / mcp tools
src/sessions/              ← memory / sqlite / file three-backend session + history compaction + input assembly
src/prompting/             ← prompt assembly subdomain (assembly / instructions / compaction / skills / context_sources)
src/safety/                ← DangerGuard → approval mode → thread permissions three-layer safety chain
src/infrastructure/        ← llm_providers (OpenAI/Anthropic + reasoning adapter + streaming) + mcp + config + tracing
src/runtime_assembly/      ← SessionEngine global composition root, assembles provider/tools/session/safety/runner
src/scheduler/             ← cron scheduled tasks (domain + store + ticker + execution_bridge + parser)
src/memory/  src/evolution/← long-term memory frozen snapshots / self-evolution after-run evidence chain
src/sitian/                ← SiTian workspace observer
src/hosts/cli/             ← CLI host (click + prompt_toolkit)
src/hosts/web/  + web/     ← FastAPI + React + WS multi-thread chat + global approval inbox
src/hosts/shared/          ← host adapter + HostDispatcher delivery gateway
```

**Dependency direction (enforced by import-linter)**:

- All modules may import `core`
- `core` may not import any sibling module
- Cross-module shared protocols (`Session` / `Tool` / `ApprovalProvider` / `LLMProvider` / `EventSink`, etc.) have a **single source of truth** in `core.contracts`
- Modules only call gateways (`*Manager` / `Protocol` / `core.contracts`); internal helpers don't cross modules

### Data flow of one CLI conversation

```
cli/main.py           parse --config / env → load_config(cfg)
  ├─ build_session(cfg, sid, bootstrap)         ← session three backends
  ├─ JsonlTraceSink(cfg.trace.output_path)      ← trace to disk
  ├─ _assemble_instructions(cfg, files)         ← multi-source system prompt
  ├─ registry.register(build_memory_tool(...))  ← memory / skill tools
  └─ SessionEngine.build(...)                   ← assemble provider / tools / session / safety / runner
        ├─ provider dispatch: openai_compatible → OpenAIResponsesProvider
        │                      anthropic        → AnthropicMessagesProvider
        ├─ SafetyGatedApproval(DangerGuard → approval mode → thread permissions → Consent)
        └─ InputAssembler(compactor) takes over system injection + compact

CLIInteractiveLoop(host_dispatcher, command_service).run_loop()
  ↓ user_input → host_dispatcher.submit(QUEUE) → mailbox → agent_loop
  runtime.run(mail_text, session_id=sid)
    ↓
    core.Runner.run(...)
      ├─ run_index = session.advance_run_index()  →  run_id = f"{sid}-{run_index}"
      ├─ _seed_messages           user into session
      ├─ while turn < max_turns:
      │    assembled = input_assembler.assemble(history, instruction_sources)
      │    resp = llm.complete(llm_request)
      │    if resp.tool_calls:
      │        for call in tool_calls:
      │            decision = approval.decide(call)   ← Danger→mode→thread permissions→Consent
      │            if approved: result = tool.execute()
      │            session.append(tool_result)
      │    else: break                               ← terminal state
      └─ emit events → list[EventSink]               ← all Events carry run_id
```

### Orchestration flow

```text
main agent tool call (run_agent_workflow)
  └─ AgentWorkflowManager              ← workflow facade: catalog / describe / exec dispatch
       ├─ AgentWorkflowStrategyManager ← strategy registry (dispatches by mode)
       │    └─ Parallel / MapReduce / DeepResearch / RoundtableReview / TaskFlow
       └─ strategy run() borrows WorkflowRuntime capabilities:
            ├─ run_subagent_task(...)
            │    └─ AgentManager.spawn(SpawnAgentRequest)
            │         ├─ parent-child AgentCell attached to AgentTree, each child gets a mailbox
            │         ├─ clip_child_tool_snapshot(): parent tools ∩ requested ∩ scope
            │         ├─ ModelCatalogResolver resolves an independent immutable snapshot for the child
            │         └─ SessionEngine.run() → Runner.run() (reuses the single run loop)
            ├─ write_workflow_manifest / write_result   ← audit and terminal-state closure
            └─ SessionTaskProgressManager               ← optional Progress popup
  → AgentWorkflowResult (runs / reports / reports/index.json / result.json)
```

For deeper architecture diagrams, dependency directions, and module responsibilities, see [docs/architecture/overview.md](docs/architecture/overview.md) and the [AGENTS.md code map](AGENTS.md).

---

## Capability overview

| Capability | Description |
|---|---|
| Single-agent run loop | The single `core.Runner`, async-first, turn advancement + tool refill + stop-condition closure |
| Multi-agent workflow orchestration | `AgentWorkflowManager` exposes 5 strategies; the LLM selects and executes via the `run_agent_workflow` tool |
| Sub-agent tree | `AgentManager` + `TaskRegistry` unify spawn and workflow child management; tool capability tightens monotonically by parent; each child has an independent session and model snapshot |
| Agent role presets | Built-in and user-defined roles, reusable by workflows and plain spawn |
| Multi-provider access | `infrastructure/llm_providers/`, OpenAI-compatible (LM Studio / Ollama / vLLM / official OpenAI) + Anthropic, unified reasoning adapter mapping |
| Built-in tools | `read_file` / `write_file` / `list_dir` / `run_shell` / `memory` / `skill` / `schedule` / `web_fetch`, all async; `web_search` goes through the provider's official MCP tool (minimax/glm), not built into the kernel |
| MCP tool access | stdio MCP client registers external MCP servers as Kongming Tools |
| Three-layer safety chain | DangerGuard → approval mode → thread permissions (assembled in `SafetyGatedApproval`), task-level `approval_mode` |
| Session engineering | memory / sqlite / file three backends, configurable switching, cross-process recovery, history compaction |
| Trace to disk | `JsonlTraceSink` appends run/tool/approval events to JSONL, derivable into usage / audit; raw LLM dump with key redaction |
| Scheduled tasks | cron module (domain + store + ticker + execution_bridge), task-level approval, concurrency admission, and real cancellation |
| Long-term memory & self-evolution | `memory` module frozen snapshots + live entries; `evolution` module after-run evidence + child reviewer + evolution artifacts |
| SiTian observer | Proactively scans 4 source kinds, materializes observations / work_items / suggestions |
| Unified config entry | YAML + multiple `KONGMING_*` env var overrides; local models can run without an api_key |
| Dual hosts | CLI (click + prompt_toolkit) and Web (FastAPI + React + WS multi-thread chat + global approval inbox); the Web host also connects two experimental channels: Claude Code SDK / Codex SDK |
| Architecture boundary enforcement | import-linter contracts + pytest architecture contract tests |

### Inspect the raw LLM provider request / response

With the env switch on, each provider call drops a complete JSON to `.kongming/debug/raw-llm-<timestamp>.json` — including request payload, request headers (Authorization redacted), response status, response headers, and the **full response body** (gzip auto-decompressed, structured).

```bash
KONGMING_TRACE_RAW_LLM=1 uv run python -m hosts.cli.main
ls -t .kongming/debug/raw-llm-*.json | head -1 | xargs jq '.response.body'
```

Off by default; nothing is written when off (production / privacy friendly). **The Authorization header is always redacted** — even if a dump file is accidentally shared, no API key leaks.

### Persistent sessions

Sessions have three backends: `memory` / `sqlite` / `file`, default `file` (cross-process):

```bash
KONGMING_SESSION_BACKEND=sqlite ./start.sh cli --session-id my-project
```

Start next time with the same `--session-id` to resume history; runtime data is derived under `<kongming_home>` (default `~/.kongming`).

---

## Development commands

```bash
make install        # uv sync --all-extras
make install-hooks  # enable commit/push two-tier hooks (run once after first clone)
make cli            # launch the CLI (local model baseline)
make smoke          # smoke test
make fmt            # ruff format
make lint           # ruff check + lint-imports (architecture boundaries)
make typecheck      # mypy
make precommit      # run pre-commit full-repo scan manually
make prepush-test   # run the pre-push isolated unit fast gate manually
make test-unit      # full unit tests
make test-e2e       # e2e tests (real-model cases skip by default)
make nightly-local  # local real e2e nightly, default port 60999
make test           # run both unit + e2e
make clean          # clear caches
```

Or the unified entry script: `./start.sh help` / `lint` / `typecheck` / `test-unit` / `test-e2e` / `smoke`.

### Check tiers

| When | Trigger | What runs | Speed | Real key |
|---|---|---|---|---|
| Pre-commit | `git commit` | `ruff check --fix`, `ruff format`, `lint-imports`, `mypy src` | Fast | No |
| Pre-push | `git push` / `make prepush-test` | Affected `tests/unit` in an isolated env, real `KONGMING_*` cleared, uses `.kongming/prepush-home` | Fast, target 1-3 min | No |
| PR CI | GitHub Actions | `fmt`, `lint`, `typecheck`, affected `tests/unit` in an isolated env | Fast, target 1-3 min | No |
| Local nightly | `make nightly-local` | `tests/integration`, `tests/e2e`, `tests/smoke`, reads `.env.e2e.local` | Slow, for overnight | Yes |
| Manual real-model | single `KONGMING_E2E_REAL_MODEL=1 uv run pytest ...` | Specific live/e2e scenarios | Slow, billed per case | Yes |

`make install-hooks` installs both commit and push hooks. The push gate runs only stable unit tests as a final fast intercept before submission; real models, real web server, and packaging smoke go in local nightly or manual verification.

```bash
# Local nightly
cp .env.example .env.e2e.local
# Edit .env.e2e.local, fill in real model provider / base_url / api_key
make nightly-local
```

`.env.e2e.local` is excluded by `.gitignore`.

### Opt-in real-model e2e

Skipped by default; only runs with a live model service:

```bash
KONGMING_E2E_REAL_MODEL=1 uv run pytest tests/e2e/test_local_model_config.py::test_local_model_real_request_roundtrip -v
```

### Verify current repo health

```bash
make install && make lint && make typecheck && make test-unit
```

---

## Design principles

1. **Single-agent kernel as the base**: `core/` defines the agent loop, turn advancement, and the protocol source of truth; multi-agent orchestration is built on top of the kernel by `application/`'s workflow and sub-agent mechanisms — no separate run loop
2. **Modularity and gateways**: each directory-level module has clear responsibilities; external gateways are uniformly named `<Domain>Manager`; modules only call gateways / `Protocol` / `core.contracts`; internal helpers don't cross modules
3. **Single source of truth for protocols**: cross-module Protocols are defined only in `core.contracts`; other modules do `from core.contracts import ...`
4. **async-first**: all core interfaces are async; the CLI shell is synchronous but internally `asyncio.run`
5. **Assembly vs. execution separation**: `SessionEngine.build()` assembles, `run()` executes
6. **The safety chain is a high-level interface**: the Tool Runtime only consumes the assembled `ApprovalProvider`; it doesn't touch low-level policy directly
7. **Event fan-out**: the runner holds a `list[EventSink]`; adding UsageSink / AuditSink is a parallel registration, no new protocol
8. **Local-first**: the default config points to a local OpenAI-compat service, zero cloud account cost to start
9. **Explicit types first**: function signatures state real types explicitly, converging `Any`; finite value sets use `StrEnum`, not bare strings

For fuller engineering constraints (constraint list, required tests, engineering pitfalls) see [AGENTS.md](AGENTS.md).

---

## Scope and status

The repo currently implements:

- **Single-agent kernel**: the single `core.Runner` run loop, cross-module protocol source of truth, async-first interfaces
- **Multi-agent orchestration**: 5 workflow strategies, sub-agent tree, `spawn_subagent`, agent role presets
- **Tools and extensions**: file / shell / memory / skill / schedule / web_fetch built-in tools, stdio MCP access; web_search via the provider's official MCP (minimax/glm)
- **Model access**: OpenAI-compatible + Anthropic providers, unified reasoning-effort mapping
- **Sessions and prompting**: memory / sqlite / file three backends, history compaction, input assembly, skill loading
- **Safety**: DangerGuard → approval mode → thread permissions three-layer chain, task-level `approval_mode`
- **Observability**: JSONL trace, raw LLM dump (key redaction), usage and audit events
- **Scheduled tasks**: cron module, execution_bridge, concurrency admission, and real cancellation
- **Long-term capabilities**: long-term memory, self-evolution evidence chain, SiTian workspace observer
- **Hosts**: CLI (click + prompt_toolkit), Web (FastAPI + React + WS multi-thread chat + global approval inbox)
- **Engineering base**: ruff / mypy (incl. `@override` checks) / pytest / import-linter / commit+push two-tier hooks / CI

---

## Roadmap

Directions currently evolving or planned, grouped by theme. For evolution milestones see the design docs under `docs/spec/`.

**Proactivity (in optimization)**
- **SiTian auto-trigger**: currently only manual (`run-once` / `loop`); automatic scheduling (cron / event-driven) is being optimized
- **Self-evolution cadence**: the auto-cadence trigger for evolution reviews is being optimized

**Model eval (exploratory)**
- **runtime eval**: [`evals/harness-runtime-v0.1/`](evals/harness-runtime-v0.1/) runs 12 tasks / 7 categories (instruction / coding / repo_fix / tool_execution / long_context / tau_tool_state) inside a real SessionEngine + Runner closed loop, fixture mode (fake LLM, CI-repeatable) + preset mode (real model). Each run collects token usage and cost accounting ([`evals/src/metrics.py`](evals/src/metrics.py) three-tier aggregation; with a pricing block it can convert to amounts), used to measure model running cost and capability across presets / strategies.
- **regression eval**: [`evals/regression-v0.1/`](evals/regression-v0.1/) draws from the project's historical bugs / fix reports to test "real engineering ability on this codebase," with 5 task-type designs (including an architecture-boundary type that reuses `lint-imports`); the MVP 3 tasks are selected, pending intake.

**Capability extension**
- **computer use** (in development): let the agent operate desktop apps, not limited to CLI tools
- **Cross-platform desktop/mobile hosts**: kongming connects to desktops via a sidecar model — this repo provides [`packaging/`](packaging/) (`kongming-web-backend` sidecar build + web dist + runtime config) and [`config/xspace/`](config/xspace/) (desktop runtime config samples) for downstream desktop clients to consume; the mobile side provides device pairing and auth backend via [`src/hosts/web/xspace_mobile/`](src/hosts/web/xspace_mobile/) (Android integrated, iOS pending). The desktop client itself (XSpace, built on Tauri) is a separate project, not in this repo
- More workflow strategies, richer agent role presets
- Multi-provider smart routing (auto-select by cost / latency / capability)

**Engineering governance**
- Modularization of guardrails / usage_meter / audit_log
- Once usage_meter lands, it shares usage data with the cost eval above

---

## License

MIT
