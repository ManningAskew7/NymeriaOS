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

# Configure environment
cp .env.docker.example .env.docker
# Generate API key: python -c "import secrets; print(secrets.token_urlsafe(32))"
# Edit your environment file with NYMERIA_API_KEY and an LLM provider API key

# Run API server
python run.py api

# Or run CLI
python run.py cli
```

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Documentation

- [Architecture Overview](./architecture.md) - How Nymeria works internally
- [Tools Reference](./tools.md) - Built-in tools, optional tools, custom tools, and callable threads
- [Configuration](./configuration.md) - Environment variables and settings
- [API Reference](./api.md) - REST API endpoints including settings, tools, TODOs, MCP servers, voice, and callable threads

## Project Structure

```
C:\Nymeria\
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
│   │   ├── backup.py       # File backup system
│   │   ├── validator.py    # Python code validation
│   │   ├── prompts.py      # System prompt templates (extracted)
│   │   ├── audit.py        # Tool execution logging (extracted)
│   │   ├── rate_limiter.py # Rate limiting (extracted)
│   │   ├── time_utils.py   # Time parsing utilities (extracted)
│   │   ├── todo_constants.py    # TODO display constants (extracted)
│   │   ├── graph_cache.py  # Graph caching utilities (extracted)
│   │   ├── migration.py    # Legacy task migration (extracted)
│   │   ├── scheduler.py    # Deprecated scheduler (rate limiter extracted)
│   │   └── _deprecated/    # Legacy modules (pending removal)
│   │       └── task_db.py  # Old task database (migrated to TODOs)
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
│   │   ├── outlook_auth.py # Microsoft OAuth authentication (3 tools)
│   │   └── outlook_email.py # Outlook email via Graph API (10 tools)
│   ├── agents/             # Callable-thread tool helpers and built-in agent presets
│   │   ├── __init__.py
│   │   ├── tool_factory.py # Generates direct tool bindings for callable threads
│   │   ├── browser_agent.py
│   │   ├── outlook_agent.py
│   │   ├── calendar_agent.py
│   │   └── self_modify_agent.py
│   ├── triggers/           # CLI, API, and event-driven interfaces
│   │   ├── cli.py          # Interactive terminal
│   │   ├── api.py          # FastAPI REST server + /autonomous/stream SSE
│   │   ├── webhook.py      # Incoming webhook handlers (Telegram/Discord/Slack)
│   │   ├── discord_bot.py  # Discord gateway bot
│   │   ├── twitch_bot.py   # TwitchIO bot with pulse/moderation support
│   │   └── sources/        # Event-driven trigger source plugins
│   │       ├── base.py     # Abstract TriggerSource
│   │       ├── webhook_source.py
│   │       └── outlook_email_source.py
│   └── config/             # Settings and prompts
│       ├── settings.py     # Pydantic settings
│       ├── soul.md         # System prompt
│       └── self_agent_prompt.md
├── data/
│   ├── nymeria.db          # SQLite conversation storage
│   ├── todo_schedule.db    # SQLite scheduled TODO index
│   ├── tasks.db            # Legacy scheduled tasks (deprecated)
│   ├── logs/               # Audit logs (tool executions)
│   ├── users/              # User profiles and memories
│   ├── backups/            # Self-modification backups
│   └── custom_tools/       # Custom tool definitions (JSON)
├── docs/                   # Documentation
├── run.py                  # Entry point
├── requirements.txt        # Dependencies
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
