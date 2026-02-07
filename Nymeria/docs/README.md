# Nymeria Documentation

Nymeria is a personal AI assistant framework built on LangGraph's ReAct architecture.

## Key Features

- **Persistent Memory**: Remembers user facts and preferences across conversations
- **Self-Modification**: Can add, remove, and fix its own tools at runtime
- **Custom Tools**: Create HTTP and MCP tools via UI or API without writing Python
- **Sub-Agents**: Define specialized agents with custom prompts and tool restrictions
- **Autonomous Operation**: Operates 24/7 via scheduled TODOs with `scheduled_for` parameter
- **Multi-User Support**: Isolated profiles per user with thread-safe operations
- **Auto-Compact Context**: Automatically summarizes conversations when approaching context limits, preserving important facts in memory
- **Rate Limiting**: Prevents runaway autonomous loops (configurable limit per hour)
- **45+ Built-in Tools**: Shell execution, file operations, web search, memory, self-modification, TODO management, sub-agents, browser automation, Outlook email integration, RAG semantic search

## Quick Start

See [QUICKSTART.md](./QUICKSTART.md) for detailed setup instructions.

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment (minimal setup)
cp .env.minimal .env
# Generate API key: python -c "import secrets; print(secrets.token_urlsafe(32))"
# Edit .env with your NYMERIA_API_KEY and LLM provider API key

# Run API server
python run.py api

# Or run CLI
python run.py cli
```

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Documentation

- [Architecture Overview](./architecture.md) - How Nymeria works internally
- [Tools Reference](./tools.md) - Built-in tools, custom tools, and sub-agents
- [Configuration](./configuration.md) - Environment variables and settings
- [API Reference](./api.md) - REST API endpoints including custom tools and sub-agents API

## Project Structure

```
C:\Nymeria\
├── nymeria/
│   ├── core/               # Agent implementation
│   │   ├── agent.py        # Main NymeriaAgent class
│   │   ├── user_profile.py # User memory & profile management
│   │   ├── self_agent.py   # Self-modification sub-agent
│   │   ├── todo_manager.py # TODO list management (JSON file storage)
│   │   ├── todo_schedule_db.py  # SQLite schedule index for polling
│   │   ├── ticker.py       # Global polling thread for scheduled TODO execution
│   │   ├── event_bus.py    # Pub/sub for streaming autonomous events to frontend
│   │   ├── response_handler.py  # Response visibility control
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
│   ├── tools/              # Tool definitions (45+ tools)
│   │   ├── bash.py         # Shell command execution
│   │   ├── filesystem.py   # File read/write/list
│   │   ├── web.py          # Web search via Perplexity
│   │   ├── claude_code.py  # Claude Code integration
│   │   ├── memory.py       # User memory + RAG search tools
│   │   ├── self_modify.py  # Self-modification tools
│   │   ├── todo.py         # TODO management with scheduling
│   │   ├── subagent.py     # Sub-agent invocation (also direct tools)
│   │   ├── visibility.py   # Response visibility control (mute_response)
│   │   ├── browser.py      # Native Playwright browser automation (8 tools)
│   │   ├── outlook_auth.py # Microsoft OAuth authentication (3 tools)
│   │   └── outlook_email.py # Outlook email via Graph API (10 tools)
│   ├── agents/             # Sub-agent definitions
│   │   ├── __init__.py     # Agent registry
│   │   ├── tool_factory.py # Generates direct tool bindings for agents
│   │   └── *.py            # Individual agent files (e.g., browser_agent.py)
│   ├── triggers/           # CLI and API interfaces
│   │   ├── cli.py          # Interactive terminal
│   │   └── api.py          # FastAPI REST server + /autonomous/stream SSE
│   └── config/             # Settings and prompts
│       ├── settings.py     # Pydantic settings
│       ├── soul.md         # System prompt
│       └── self_agent_prompt.md
├── data/
│   ├── nymeria.db          # SQLite conversation storage
│   ├── schedules.db        # SQLite scheduled TODOs
│   ├── tasks.db            # Legacy scheduled tasks (deprecated)
│   ├── logs/               # Audit logs (tool executions)
│   ├── users/              # User profiles and memories
│   ├── backups/            # Self-modification backups
│   └── custom_tools/       # Custom tool definitions (JSON)
├── docs/                   # Documentation
├── run.py                  # Entry point
├── test_nymeria.py         # Test suite
├── requirements.txt        # Dependencies
└── .env.example            # Configuration template
```

## Tool Categories

| Category | Tools | Purpose |
|----------|-------|---------|
| **Core** | 6 | Shell execution, file operations, web search, Claude Code |
| **Memory** | 5 | User memories and personality preferences |
| **RAG** | 2 | Semantic search across conversations and memories |
| **Self-Modification** | 4 | Modify Nymeria's own tools at runtime |
| **TODO** | 5 | Task management with scheduled autonomous execution |
| **Sub-Agents** | 4 | Invoke specialized sub-agents (also available as direct tools) |
| **Visibility** | 1 | Control response display (mute_response) |
| **Browser** | 8 | Native Playwright browser automation |
| **Outlook** | 13 | Microsoft Graph email and OAuth authentication |
| **Custom** | ∞ | User-defined HTTP or MCP tools |

See [Tools Reference](./tools.md) for detailed documentation.
