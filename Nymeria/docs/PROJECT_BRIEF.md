# Nymeria Project Brief

> High-level overview of Nymeria's current architecture and capabilities (updated for the current codebase).

## What Nymeria Is

Nymeria is a personal AI assistant framework built on a LangGraph ReAct runtime, designed for:
- Stateful conversations with persistence
- Long-running autonomous task execution
- Tool-augmented workflows
- User-configurable behavior at thread and user scope

Core operating model:
- Interactive chat via API/CLI/desktop
- Scheduled TODO execution via ticker
- Event-driven execution via triggers

## System Shape

Nymeria is organized into:
- `nymeria/core/` runtime orchestration (`NymeriaAgent`, ticker, TODO manager, trigger manager, event bus)
- `nymeria/tools/` tool surface (18 core + 17 optional tools)
- `nymeria/agents/` sub-agent registry + direct tool wrappers
- `nymeria/triggers/` interfaces (API, CLI, webhooks, Discord bot)
- `nymeria/config/` settings and system prompts
- `nymeria/vendor/react_agent/` vendored ReAct runtime

## Core Runtime

### NymeriaAgent

`NymeriaAgent` is the orchestrator for:
- Graph construction and caching per `(user_id, thread_id)`
- Memory and TODO injection into system prompt
- Per-thread tool and LLM overrides
- Sync and async streaming paths
- Custom tool loading and hot reload

Execution limits:
- Main agent max iterations: `70`
- Sub-agent max iterations: `30`

### Time and Context

Each turn is prefixed with time metadata generated from configured timezone (`USER_TIMEZONE`).

Context management modes:
- `auto_compact` (default)
- `sliding_window`
- `none`

### Persistence

Primary stores:
- `data/nymeria.db`: LangGraph conversation/checkpoint persistence (SQLite by default)
- `data/todo_schedule.db`: scheduled TODO index for polling
- `data/todos/*.json`: TODO source of truth
- `data/users/*`: user profiles, thread configs, activity, triggers
- `data/custom_tools/*.json`: custom tool definitions

Legacy migration artifacts:
- `data/tasks.db` / `_deprecated/task_db.py` are retained for migration compatibility.

## Tooling Model

Nymeria currently exposes:
- **18 core tools** (`ALL_TOOLS`)
- **4 sub-agent wrappers** generated via `tool_factory.py`
- **17 optional tools** enabled per thread (`OPTIONAL_TOOLS`) — 13 Outlook email + 4 trigger

Default tool surface for runtime initialization:
- `get_all_tools_with_agents()` => 22 tools (18 core + 4 sub-agent wrappers)

Core categories include:
- system execution (`bash_execute`, file tools, web, think, claude_code)
- memory/rag (`memory_save`, `memory_forget`, `personality_set`, `rag_search`)
- tasking (`todo`, `todo_delete`, `todo_list`)
- agent ops (`clear_agent_context`, `reload_all`, `self_modify_rollback`)
- notifications (`notify`)

Optional (per-thread or via SelfModifyAgent):
- triggers (`trigger_create/list/update/delete`)

## Sub-Agents

Built-in registered sub-agents:
- `BrowserAgent`
- `OutlookAgent`
- `CalendarAgent`
- `SelfModifyAgent`

They are directly callable as tools (for example `BrowserAgent(task="...")`), not via a generic wrapper command.

## Autonomy Model

### Scheduled TODOs

Autonomous time-based work is driven by TODOs with `scheduled_for`.

Ticker behavior (`core/ticker.py`):
- Poll interval configurable (`TICKER_POLL_INTERVAL`, default 5s)
- Parallel autonomous execution via thread pool (`MAX_CONCURRENT_AUTONOMOUS`, default 5)
- Retry handling for failures
- Recurrence support (`5min`, `10min`, `15min`, `30min`, `hourly`, `daily`, `weekly`, `monthly`)

### Triggered Work

Event-driven automation is supported by trigger sources:
- `webhook`
- `outlook_email`

Triggers can perform actions:
- `agent_prompt`
- `notify`
- `create_todo`

### Watchdog

Watchdog monitors stale TODOs and can nudge execution/notify channels.

## Interfaces

### API (`run.py api`)

FastAPI service exposes:
- interactive chat (`/chat`, `/chat/sync`)
- autonomous stream (`/autonomous/stream`)
- TODO/dashboard endpoints
- settings/runtime diagnostics
- custom tool CRUD + test
- sub-agent CRUD + test
- trigger CRUD + fire
- unified tool management endpoints

### CLI (`run.py cli`)

Interactive local terminal interface for chat and operations.

### Worker (`run.py worker`)

Ticker-focused worker process for split deployments.

### MCP (`run.py mcp`)

Nymeria capability exposure over MCP (stdio or HTTP mode).

### Discord Bot (`run.py discord-bot`)

Two-way Discord integration in gateway mode.

## Frontend (nymeria-desktop)

Desktop app (Svelte + Tauri) provides:
- streaming chat with tool-step visualization
- thread management and per-thread configuration
- TODO/activity/notifications dashboard
- autonomous SSE integration
- custom tools and sub-agent management UI
- triggers and settings management

## Security and Safety Posture

Current safety controls include:
- API key authentication
- thread-level locking to avoid concurrent conversation collisions
- constrained self-modification file scope + backups + syntax/import validation
- optional per-user tool preference controls and category toggles
- rate limits for autonomous operations

Operational note:
- Core execution tools (`bash_execute`, `file_write`) are powerful by design; deployment hardening is still required for exposed environments.

## Current Strengths

- Cohesive runtime for interactive + autonomous work
- Strong inspectability (tool streams, activity log, context stats)
- Practical extensibility (custom HTTP/MCP tools, sub-agent CRUD)
- Durable scheduling and trigger orchestration in one system

## Current Gaps (Pragmatic)

- No first-class mobile-native UX; desktop is primary control surface
- Workflow-level pause/approve/resume semantics are less explicit than dedicated workflow engines
- Public ecosystem/package distribution for tools/agents is still early
- Security ergonomics for internet-facing self-hosting can be further productized

## Primary References

- `nymeria/core/agent.py`
- `nymeria/core/ticker.py`
- `nymeria/core/trigger_manager.py`
- `nymeria/tools/__init__.py`
- `nymeria/triggers/api.py`
- `docs/architecture.md`
- `docs/tools.md`
- `docs/api.md`
