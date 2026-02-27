# Nymeria Roadmap

This document tracks planned and completed features.

---

## Completed Features

### Custom Tool Management (Implemented)

Users can create, edit, and manage custom tools through the desktop UI (Settings > Tools) or REST API without writing Python.

**Supported implementation types:**
- **HTTP**: REST API calls with parameter interpolation and secret injection (`${env:VAR_NAME}`)
- **MCP**: Connect to Model Context Protocol servers via JSON-RPC over stdio

**API endpoints:** `GET/POST /tools/custom`, `PUT/DELETE /tools/custom/{id}`, `POST /tools/custom/{id}/test`

Storage: JSON files in `data/custom_tools/`

---

### Callable Threads (Implemented, replaces Sub-Agent System)

Any thread with `callable=True` becomes a directly invocable tool. Replaces the old sub-agent registry with a thread-based approach where each callable thread has its own system prompt, LLM overrides, and tool configuration.

**Built-in callable threads:** BrowserAgent, OutlookAgent, CalendarAgent, SelfModifyAgent

**Features:**
- Configure via thread settings UI or `PATCH /threads/{id}/config`
- Direct tool invocation: `ResearchAgent(task="...")` — no wrapper needed
- Live SSE streaming of callable thread activity to frontend
- Cascading abort support (parent→child)
- Thread title always equals callable_name — renaming syncs both

---

### Event-Driven Triggers (Implemented)

Trigger system that fires agent prompts or actions in response to events.

**Source types:** `webhook` (generic incoming), `outlook_email` (polls for new emails)

**Components:** `core/trigger_manager.py`, `triggers/sources/base.py`, CRUD tools (`trigger_create/list/update/delete`)

---

### Per-Thread Configuration (Implemented)

Each thread can have custom instructions, disabled/enabled tools, and LLM settings overrides.

**Component:** `core/thread_config.py`, UI in ThreadSettingsPanel

---

### Server-Side Thread Metadata (Implemented)

Thread metadata (titles, pins, platform) is now server-authoritative instead of frontend-only localStorage. All surfaces (desktop, Discord, Telegram, Slack, webhooks) share the same view.

**Key features:**
- `thread_metadata.py`: Per-user JSON storage with thread-safe locks
- Auto-title generation from first message (mirrors frontend logic)
- Title sources: `auto`, `user` (manual rename), `callable` (synced from callable_name)
- Platform field stored in metadata instead of inferred from ID prefixes
- All trigger sources create metadata on first message
- `PATCH /threads/{id}/metadata` syncs renames back to callable_name and rebuilds tool registry
- Frontend syncs from backend metadata on startup via `GET /threads`

---

## Planned Features

### Python Snippet Tools (Sandboxed)
Allow custom tools to execute sandboxed Python code for simple transformations.

### Composite Tools / Workflows
Chain multiple tools together into reusable workflows.

### Automatic Sub-Agent Routing
Detect when to route messages to sub-agents based on intent classification or keyword matching.

### Tool Sharing / Marketplace
Central repository for community-created custom tools and sub-agent configurations.

---

## Open Questions

1. **Tool versioning**: How to handle changes to tool definitions?
2. **Sharing**: Should users be able to share tools/agents?
3. **Marketplace**: Central repository of community tools?
4. **Testing**: Automated testing for custom tools?
5. **Permissions**: Fine-grained access control for tools?

---

## Related Documents

- [Architecture Overview](./architecture.md)
- [Tools Reference](./tools.md)
- [API Documentation](./api.md)
