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

### MCP Paste-Install + Managed Runtime (Implemented)

Rather than filling out the MCP tool form by hand, users (and the agent) can paste an install source and Nymeria takes care of hosting.

**Accepted sources:**
- Claude Desktop `mcpServers` JSON blob
- Bare stdio command string (`npx -y @modelcontextprotocol/server-filesystem /tmp`)
- HTTP/SSE URL (covers Docker MCP Gateway: `docker mcp gateway run --transport streaming`)
- Official registry id (`io.github.modelcontextprotocol/server-filesystem`)
- npm and PyPI package pages
- Git repository URLs
- `.mcpb`, `.dxt`, and `.zip` bundles by upload or URL

**New components:**
- `core/mcp_installer.py` — paste parser
- `core/mcp_runtime.py` — preview, smart-confirm metadata, managed cache/source directories, Git/package/bundle preparation, encrypted config values, disabled failure drafts
- `core/mcp_registry_client.py` — clients for registry.modelcontextprotocol.io + Smithery
- `tools/search_mcp.py` — agent-facing `mcp_search` + `mcp_install`
- `POST /mcp-servers/install/preview`, `/preview-upload`, `/install`, and `/{server_id}/retry` — REST endpoints for the desktop paste box
- HTTP transport in `core/mcp_manager.py` alongside the existing stdio path
- Lifecycle fixes: process-group spawn + kill, stderr drain thread, per-phase timeouts (init 10s / list 30s / call 60s)

**Settings:** `MCP_REGISTRY_URL`, `SMITHERY_API_KEY` (optional).

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

**Components:** `core/trigger_manager.py`, `triggers/sources/base.py`, trigger tools (`trigger_config`, `trigger_info`)

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

### Automatic Callable-Thread Routing
Detect when to route messages to callable threads based on intent classification or keyword matching.

### Tool Sharing / Marketplace
Central repository for community-created custom tools and callable-thread configurations.

### MCP Paste-Install Phase 2
Follow-up work on top of the Phase 1 paste-install feature:
- **Docker MCP Gateway control**: programmatically enable/disable catalog entries, manage secrets via `docker mcp secret`, drive the OAuth flow for remote servers — so users do not have to run the gateway themselves.
- **OS keychain secrets**: optionally replace Fernet-on-disk values with `keyring`-backed storage on desktop hosts.
- **Stdio auto-reconnect**: exponential backoff + restart on unexpected server exit (today we only mark the connection dead).
- **Registry-driven config UX**: richer forms from registry metadata, OAuth handoff, and version update prompts.

---

## Open Questions

1. **Tool versioning**: How to handle changes to tool definitions?
2. **Sharing**: Should users be able to share tools/agents?
3. **Marketplace**: Central repository of community tools?
4. **Testing**: Automated testing for custom tools?
5. **Permissions**: Fine-grained access control for tools?

---

## Recently Implemented but not fully reflected elsewhere yet

- MCP server management endpoints are now part of the API surface.
- Voice endpoints (`/voice/chat`, `/voice/tts`, `/voice/stt`) and device registration are now first-class runtime features.
- A `twitch-bot` runtime exists alongside CLI, API, worker, MCP, and Discord modes.

## Related Documents

- [Architecture Overview](./architecture.md)
- [Tools Reference](./tools.md)
- [API Documentation](./api.md)
