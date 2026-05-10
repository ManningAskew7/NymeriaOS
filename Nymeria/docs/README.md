# Nymeria Documentation

Nymeria is a personal AI assistant framework built on LangGraph's ReAct architecture.

## Key Features

- **Persistent Memory**: Remembers user facts and preferences across conversations
- **Self-Modification**: Can add, remove, and fix its own tools at runtime
- **Custom Tools**: Create HTTP and MCP tools via UI or API without writing Python
- **Callable Threads**: Define specialized callable threads with custom prompts and tool restrictions
- **Autonomous Operation**: Operates 24/7 via scheduled TODOs with `scheduled_for` parameter
- **Multi-User Support**: Isolated profiles per user with thread-safe operations
- **Auto-Compact Context**: Automatically summarizes conversations when approaching context limits, preserving important facts in memory
- **Rate Limiting**: Prevents runaway autonomous loops (configurable limit per hour)
- **Layered Tool System**: Core tools, optional per-thread tools, custom tools, and callable-thread-backed tools for specialized workflows

## Quick Start

See [QUICKSTART.md](./QUICKSTART.md) for detailed setup instructions.

```bash
# Install dependencies
pip install -r requirements.txt
# requirements.txt includes the default SQLite backend extras.
# Optional local PostgreSQL backend:
# pip install -r requirements-postgres.txt

# Configure environment
cp .env.docker.example .env.docker
# Fill in an LLM provider API key. User auth now uses per-user account tokens:
# on first boot, the bootstrap admin token is written to data/BOOTSTRAP_TOKEN.txt.
# See docs/accounts.md.

# Run API server
python run.py api

# Or run CLI
python run.py cli
```

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## CLI Modes

The interactive CLI supports three renderer modes and two transport families:

```bash
python run.py cli --renderer auto --transport auto
python run.py cli --renderer full --transport api --api-url http://localhost:8000 --api-key <token>
python run.py cli --renderer plain --transport local
```

`--renderer auto` uses the full-screen terminal UI in an interactive TTY and falls back to plain output for pipes, CI, and dumb terminals. `--transport auto` uses the REST/SSE API when API credentials are configured and otherwise falls back to the local in-process agent. See [UI Knowledge Base](./ui-knowledgebase.md#8-interactive-cli) for the complete CLI command and keyboard reference.

## Documentation

- [Architecture Overview](./architecture.md) - How Nymeria works internally
- [UI Knowledge Base](./ui-knowledgebase.md) - User-facing desktop, mobile, bot, Outlook, CLI, and API behavior reference
- [Tools Reference](./tools.md) - Built-in tools, optional tools, custom tools, and callable threads
- [Configuration](./configuration.md) - Environment variables and settings
- [API Reference](./api.md) - REST API endpoints including settings, tools, TODOs, MCP servers, voice, and callable threads
- [Beta Quickstart](./BETA_QUICKSTART.md) - Tester install, first-run setup, bundled web UI sign-in, first message, and Windows desktop notes
- [Beta Access Control](./BETA_ACCESS_CONTROL.md) - Maintainer runbook for GitHub Release access, private-index credentials, tester invites, and revocation
- [Beta Private Python Index](./BETA_PRIVATE_INDEX.md) - Maintainer setup and tester `pipx` commands for private beta package installs
- [Beta Troubleshooting](./BETA_TROUBLESHOOTING.md) - Install diagnostics, `nymeria doctor`, auth, provider, and SQLite troubleshooting for beta testers
- [Release Workflow](./release-workflow.md) - Tagged beta package builds, bundled frontend artifacts, GitHub Release uploads, and private Python index publishing
- [Compaction & Checkpoints](./compaction-and-checkpoints.md) - How `/compact` trims state and prunes the checkpointer, the display filter's internal-message handling, and troubleshooting for blank threads or slow `/history`
- [OpenRouter Provider Integration](./openrouter.md) - OpenRouter Responses beta, Chat Completions compatibility mode, reasoning/tool replay, and debugging checklist

## Project Structure

```
Nymeria/
├── nymeria/
│   ├── core/               # Agent implementation
│   │   ├── agent.py        # Main NymeriaAgent class
│   │   ├── user_profile.py # User memory & profile management
│   │   ├── thread_agent_executor.py # Callable thread execution bridge
│   │   ├── todo_manager.py # TODO list management (JSON file storage)
│   │   ├── todo_schedule_db.py  # SQLite schedule index for polling
│   │   ├── ticker.py       # Global polling thread for scheduled TODO execution
│   │   ├── event_bus.py    # Pub/sub for streaming autonomous events to frontend
│   │   ├── response_handler.py  # Autonomous response data model
│   │   ├── activity_log.py # Activity feed logging
│   │   ├── notifications.py # Push notification storage
│   │   ├── thread_deletion.py # Thread cascade deletion (TODOs, triggers, callable bindings)
│   │   ├── backup.py       # File backup system
│   │   ├── validator.py    # Python code validation
│   │   ├── prompts.py      # System prompt templates (extracted)
│   │   ├── time_utils.py   # Time parsing utilities (extracted)
│   │   ├── todo_constants.py    # TODO display constants (extracted)
│   ├── tools/              # Tool definitions (core + optional + custom integration)
│   │   ├── bash.py         # Shell command execution
│   │   ├── filesystem.py   # File read/write/list
│   │   ├── web.py          # Web search via Perplexity
│   │   ├── think.py        # Internal reasoning tool
│   │   ├── claude_code.py  # Claude Code integration
│   │   ├── memory.py       # User memory + RAG search tools
│   │   ├── todo.py         # TODO management with scheduling
│   │   ├── custom_tools.py # Custom tool CRUD/runtime support
│   │   ├── notify.py       # Unified Telegram/Discord/Slack notifications
│   │   ├── triggers.py     # Event-driven trigger CRUD
│   │   ├── browser.py      # Native Playwright browser automation (9 tools)
│   │   ├── outlook_auth.py # Microsoft OAuth authentication (4 tools)
│   │   └── outlook_email.py # Outlook email via Graph API (13 tools)
│   ├── agents/             # Callable-thread tool helpers and built-in agent presets
│   │   ├── __init__.py
│   │   ├── tool_factory.py # Generates direct tool bindings for callable threads
│   │   ├── browser_agent.py
│   │   ├── outlook_agent.py
│   │   ├── calendar_agent.py
│   │   └── self_modify_agent.py
│   ├── triggers/           # CLI, API, and event-driven interfaces
│   │   ├── cli/            # Interactive terminal (app.py, input.py, state.py, commands/, rendering/)
│   │   ├── api.py          # FastAPI REST server + /autonomous/stream SSE
│   │   ├── trigger_api.py  # Trigger CRUD endpoints (mounted on the FastAPI app)
│   │   ├── webhook.py      # Incoming webhook handlers (Telegram/Discord/Slack)
│   │   ├── discord_bot.py  # Discord gateway bot
│   │   ├── telegram_bot.py # Telegram polling bot (shared + per-user BYO)
│   │   ├── twitch_bot.py   # TwitchIO bot with pulse/moderation support
│   │   ├── watchdog_worker.py # Thin-client watchdog worker
│   │   └── sources/        # Event-driven trigger source plugins
│   │       ├── base.py     # Abstract TriggerSource
│   │       ├── webhook_source.py
│   │       ├── outlook_email_source.py
│   │       ├── rss_source.py
│   │       ├── http_poll_source.py
│   │       ├── slack_source.py
│   │       └── teams_source.py
│   └── config/             # Settings and prompts
│       ├── settings.py     # Pydantic settings
│       ├── soul.md         # System prompt
│       └── self_agent_prompt.md
├── data/
│   ├── nymeria.db          # SQLite conversation storage
│   ├── todo_schedule.db    # SQLite scheduled TODO index
│   ├── logs/               # Audit logs (tool executions)
│   ├── users/              # User profiles and memories
│   ├── backups/            # Self-modification backups
│   └── custom_tools/       # Custom tool definitions (JSON)
├── docs/                   # Documentation
├── pyproject.toml          # Python package metadata and optional extras
├── README.md               # Backend package overview
├── run.py                  # Entry point
├── requirements.txt        # Base dependencies plus SQLite extras include
├── requirements-dev.txt    # Development/test dependencies
├── requirements-sqlite.txt # Default local SQLite/checkpoint dependencies
├── requirements-postgres.txt # Optional PostgreSQL checkpoint dependencies
├── requirements-docker.txt # Docker-only supplemental dependencies
├── Dockerfile.full         # Agent executor image with Kali/browser/CLI tooling
├── Dockerfile.slim         # HTTP thin-client image without workstation tooling
└── .env.docker.example     # Configuration template
```

## Tool Categories

| Category | Count | Purpose |
|----------|-------|---------|
| **Core System** | Current code-defined set | Shell, file, web, reasoning, notification, and runtime utilities |
| **Profile & RAG** | Current code-defined set | User memories, personality preferences, semantic search |
| **TODO** | Current code-defined set | Task management with scheduled autonomous execution |
| **Callable Threads** | Dynamic | Per-thread callable tools such as browser or calendar helpers |
| **Optional Tool Families** | Current code-defined set | Outlook, browser, trigger, Twitch, self-modify, and related optional tools |
| **Custom** | ∞ | User-defined HTTP or MCP tools |

See [Tools Reference](./tools.md) for detailed documentation.
