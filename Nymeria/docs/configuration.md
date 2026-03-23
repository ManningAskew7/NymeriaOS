# Nymeria Configuration

All configuration is done via environment variables. Copy `.env.minimal` to `.env` for a quick start, or `.env.example` for all options.

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Environment Variables

### LLM Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `anthropic` | LLM provider: `openrouter`, `anthropic`, `openai` |
| `LLM_MODEL` | Yes | `claude-sonnet-4-20250514` | Model identifier for the provider |
| `LLM_TEMPERATURE` | No | `1.0` | Sampling temperature (0.0 - 2.0) |

### Advanced LLM Settings (Optional)

These settings give power users fine-grained control over LLM behavior. All are optional and only sent to the API if explicitly set.

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `LLM_MAX_TOKENS` | (model limit) | 1 - 32000 | Maximum output tokens |
| `LLM_TOP_P` | (provider default) | 0.0 - 1.0 | Nucleus sampling threshold |
| `LLM_TOP_K` | (provider default) | 1 - 100 | Top-k sampling (limits vocabulary per step) |
| `LLM_FREQUENCY_PENALTY` | (provider default) | -2.0 - 2.0 | Reduce repetition of token sequences |
| `LLM_PRESENCE_PENALTY` | (provider default) | -2.0 - 2.0 | Encourage new topics |
| `LLM_REASONING_EFFORT` | (none) | low/medium/high | For reasoning models (o1, Claude with thinking) |
| `LLM_EXTENDED_THINKING` | `false` | true/false | Enable extended thinking/reasoning for compatible models |
| `LLM_USE_MODEL_DEFAULTS` | `false` | true/false | Use model-specific defaults for temperature, top_p, and frequency penalty instead of global values. When enabled, these params are not sent to the API — the provider applies the model's own optimal defaults. |
| `LLM_BASE_URL` | (provider default) | URL | Override API endpoint for `openrouter`, `openai`, or `anthropic` providers (e.g., `http://localhost:8317` for a local proxy). For `anthropic`, omit the `/v1` suffix — `ChatAnthropic` appends `/v1/messages` automatically. Leave unset to use the provider's standard URL. |

**Note:** For OpenRouter, Nymeria uses `supported_parameters` from model metadata to automatically skip unsupported params (e.g., reasoning config for non-reasoning models). This prevents silent failures.

### API Keys

Set the API key for your chosen provider:

| Variable | Provider | Required |
|----------|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic | If using `anthropic` provider |
| `OPENAI_API_KEY` | OpenAI | If using `openai` provider |
| `OPENROUTER_API_KEY` | OpenRouter | If using `openrouter` provider |
| `PERPLEXITY_API_KEY` | Perplexity | Required for `web_search` tool |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_BACKEND` | `sqlite` | Backend type: `sqlite`, `postgres`, or `memory` |
| `SQLITE_PATH` | `data/nymeria.db` | SQLite database file location |
| `POSTGRES_URI` | - | PostgreSQL connection string (if using postgres) |
| `USER_TIMEZONE` | `Australia/Sydney` | IANA timezone used for time context and absolute schedule parsing |

### API Server

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_API_KEY` | (required) | Bearer token for API authentication. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8000` | Server port |
| `CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins, or `*` for all |
| `NYMERIA_DATA_DIR` | (project)/data | Override data directory path (useful for Docker volumes) |

### Redis (Docker Only)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_ENABLED` | `false` | Enable Redis event bus for cross-container communication |
| `REDIS_URL` | - | Redis connection URL (e.g., `redis://localhost:6379`) |

### Messaging Platforms

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | - | Telegram bot token from @BotFather |
| `TELEGRAM_DEFAULT_CHAT_ID` | - | Default Telegram chat ID for notifications |
| `DISCORD_WEBHOOK_URL` | - | Discord webhook URL for notifications |
| `DISCORD_BOT_TOKEN` | - | Discord bot token for two-way communication |
| `DISCORD_MODE` | `gateway` | Discord connection mode: `gateway` or `webhook` |
| `DISCORD_RESPOND_MODE` | `mention` | Guild behavior: `mention` (only @Nymeria) or `all` |
| `SLACK_WEBHOOK_URL` | - | Slack webhook URL for notifications |
| `SLACK_BOT_TOKEN` | - | Slack bot token for two-way communication |
| `TWITCH_CLIENT_ID` | - | Twitch application Client ID |
| `TWITCH_CLIENT_SECRET` | - | Twitch application Client Secret |
| `TWITCH_BOT_ACCESS_TOKEN` | - | Bot's OAuth access token |
| `TWITCH_BOT_REFRESH_TOKEN` | - | Bot's OAuth refresh token |
| `TWITCH_BOT_USER_ID` | - | Bot's numeric Twitch user ID |
| `TWITCH_BROADCASTER_TOKEN` | - | Broadcaster's OAuth token (channel:bot scope) |
| `TWITCH_BROADCASTER_REFRESH_TOKEN` | - | Broadcaster's refresh token |
| `TWITCH_CHANNEL` | `silk` | Twitch channel to join |
| `TWITCH_BUFFER_SIZE` | `500` | Chat message ring buffer size (50-5000) |
| `TWITCH_PULSE_ENABLED` | `true` | Enable periodic chat evaluation |
| `TWITCH_PULSE_INTERVAL` | `300` | Seconds between pulse checks (60-3600) |
| `TWITCH_PULSE_MESSAGE_COUNT` | `100` | Messages to include in pulse context |
| `TWITCH_COMMAND_CONTEXT_COUNT` | `50` | Messages to include with !ask context |
| `TWITCH_RESPOND_MODE` | `command` | Response mode (command = only !commands) |
| `WEBHOOK_SECRET` | - | Secret for validating incoming webhooks |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AUDIT_LOG_ENABLED` | `true` | Log all tool executions to audit log |

### Autonomous Operation

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKER_POLL_INTERVAL` | `5` | Seconds between polls for due tasks (1-60) |
| `MAX_CONCURRENT_AUTONOMOUS` | `5` | Max concurrent autonomous tasks (`0` = unlimited) |
| `MAX_SELF_INVOKES_PER_HOUR` | `50` | Rate limit per user to prevent runaway loops |
| `LOCK_TIMEOUT` | `120` | Seconds to wait on per-thread lock before timing out |
| `TOOL_TIMEOUT` | `300` | Max seconds a tool/sub-agent invocation may run |

### Context Management

Nymeria automatically manages conversation context to prevent overflow. The default `auto_compact` mode summarizes conversations when approaching the model's context limit.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_MANAGEMENT` | `auto_compact` | Strategy: `auto_compact`, `sliding_window`, or `none` |
| `COMPACT_THRESHOLD` | `0.8` | Trigger compaction at this % of context window (0.5-0.95) |
| `COMPACT_KEEP_MESSAGES` | `4` | Minimum messages before compaction is allowed |
| `COMPACT_MODEL` | (main model) | Optional cheaper model for summarization |
| `SLIDING_WINDOW_CYCLES` | `5` | Legacy: cycles to keep when using `sliding_window` mode |

**Context Management Modes:**

- **`auto_compact`** (default): When token usage reaches the threshold, Nymeria:
  1. Asks the agent to summarize the conversation (it already has full context)
  2. Agent saves important facts to persistent memory via `memory_save`
  3. Clears the conversation and injects the summary
  4. Agent continues working without interruption

- **`sliding_window`**: Legacy mode that simply removes old messages, keeping the last N cycles

- **`none`**: No automatic context management (manual `/compact` still available)

### Watchdog & TODO System

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_ENABLED` | `true` | Enable watchdog to monitor TODO staleness |
| `WATCHDOG_INTERVAL_MINUTES` | `5` | Minutes between watchdog checks (1-60) |
| `TODO_STALENESS_MINUTES` | `20` | Minutes without update before TODO is stale (5-1440) |
| `TODO_AUTO_ARCHIVE_DAYS` | `7` | Days after completion before auto-archive (1-30) |
| `ACTIVITY_RETENTION_HOURS` | `12` | Hours to retain activity log entries (1-168) |

---

## Data Directories

Nymeria uses the following directories under the project root:

| Directory | Purpose |
|-----------|---------|
| `data/nymeria.db` | SQLite conversation database |
| `data/todo_schedule.db` | SQLite scheduled TODO index for polling |
| `data/tasks.db` | Legacy scheduled tasks database (deprecated) |
| `data/todos/` | TODO list storage (`{user_id}.json`) |
| `data/logs/` | Audit logs (`audit_YYYYMMDD.jsonl`) |
| `data/users/` | User profiles, memories, thread configs, activity logs, triggers |
| `data/backups/` | Self-modification backups |
| `data/custom_tools/` | Custom tool definitions (`{tool_id}.json`) |
| `data/notifications/` | User notification storage |

These directories and files are created automatically on first run.

---

## System Prompt (soul.md)

Located at `nymeria/config/soul.md`. This file defines Nymeria's:
- Personality and tone
- Capabilities and limitations
- Guidelines for tool usage
- Autonomous behavior rules

Edit this file to customize how Nymeria responds. Changes take effect on agent restart.

---

## Example .env

```bash
# LLM Configuration
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
LLM_TEMPERATURE=1.0
ANTHROPIC_API_KEY=sk-ant-...

# Advanced LLM Settings (all optional)
# LLM_BASE_URL=                       # Override API endpoint (e.g., local proxy)
# LLM_MAX_TOKENS=4096
# LLM_TOP_P=0.95
# LLM_TOP_K=40
# LLM_FREQUENCY_PENALTY=0.0
# LLM_PRESENCE_PENALTY=0.0
# LLM_REASONING_EFFORT=medium  # For reasoning models
# LLM_USE_MODEL_DEFAULTS=false # Let provider use model-specific optimal defaults

# Web search (optional but recommended)
PERPLEXITY_API_KEY=pplx-...

# Database (SQLite is default - no additional config needed)
DATABASE_BACKEND=sqlite
# SQLITE_PATH=data/nymeria.db  # Uncomment to customize path

# API Server
NYMERIA_API_KEY=my-secret-key
API_HOST=0.0.0.0
API_PORT=8000

# Logging
LOG_LEVEL=INFO
AUDIT_LOG_ENABLED=true

# Autonomous Operation (optional - defaults shown)
# TICKER_POLL_INTERVAL=5
# MAX_CONCURRENT_AUTONOMOUS=5
# MAX_SELF_INVOKES_PER_HOUR=50

# Context Management (optional - defaults shown)
# CONTEXT_MANAGEMENT=auto_compact  # auto_compact, sliding_window, or none
# COMPACT_THRESHOLD=0.8            # Trigger at 80% of context limit
# COMPACT_MODEL=                   # Use cheaper model for summarization
# SLIDING_WINDOW_CYCLES=5          # For legacy sliding_window mode

# Activity Log (optional)
# ACTIVITY_RETENTION_HOURS=12      # Hours to retain activity log entries

# Messaging Platforms (optional - for notify tool)
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_DEFAULT_CHAT_ID=
# DISCORD_WEBHOOK_URL=
# DISCORD_BOT_TOKEN=
# DISCORD_MODE=gateway             # gateway or webhook
# SLACK_WEBHOOK_URL=

# CORS (optional - for remote frontends)
# CORS_ORIGINS=*                   # Comma-separated origins, or * for all
```

---

## Provider-Specific Configuration

### Anthropic (Recommended)

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
```

Available models:
- `claude-opus-4-20250514` (most capable)
- `claude-sonnet-4-20250514` (balanced)
- `claude-haiku-3-5-20241022` (fastest)

### OpenAI

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
```

### OpenRouter

```bash
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-sonnet-4
OPENROUTER_API_KEY=sk-or-...
```

OpenRouter provides access to many models from different providers through a unified API.

**Tested Compatible Models:**
- `anthropic/claude-sonnet-4.5` - Recommended
- `minimax/minimax-m2.1` - Fast responses
- `deepseek/deepseek-v3.2` - Good performance
- `google/gemini-3-pro-preview` - Functional
- `z-ai/glm-4.7` - Basic compatibility

Most OpenRouter models work with Nymeria's agent harness, including tool calling.

### Local Proxy (e.g., CLIProxyAPI)

Route requests through a local proxy to use a subscription plan (e.g., Claude Max) instead of per-API-call billing. Two approaches:

#### Native Anthropic (Recommended)

Uses `ChatAnthropic` with native `/v1/messages` format. No format translation — tool calling, streaming, and extended thinking work identically to direct API usage. Requires CLIProxyAPI with Claude OAuth login (`-claude-login`).

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-opus-4-6-20250612    # Must match a model in proxy's Claude registry
LLM_BASE_URL=http://localhost:8317    # No /v1 suffix — ChatAnthropic appends /v1/messages
ANTHROPIC_API_KEY=nymeria-local-dev-key  # Proxy auth key (matches api-keys in proxy config)
```

When `LLM_BASE_URL` is set for the `anthropic` provider, `ChatAnthropic` is configured with:
- `anthropic_api_url` pointed at the proxy
- A `User-Agent: claude-cli/nymeria` header that tells CLIProxyAPI to skip system prompt cloaking (so Nymeria's own `soul.md` is preserved)

#### OpenAI-Compatible (Legacy)

Uses `ChatOpenAI` pointed at the proxy's OpenAI-compatible endpoint. Works for non-Claude models routed through the proxy.

```bash
LLM_PROVIDER=openai
LLM_MODEL=claude-sonnet-4-6           # Model name from the proxy's /v1/models
OPENAI_API_KEY=not-required            # Proxy ignores this, but validation requires it
LLM_BASE_URL=http://localhost:8317/v1  # Proxy endpoint (with /v1 suffix)
```

**Provider-aware base URL**: When `LLM_BASE_URL` is set globally, it applies to all threads using the global provider. Threads with a per-thread provider override to a *different* provider (e.g., `openrouter`) ignore the global base URL and use the provider's standard endpoint. This allows callable threads to route through OpenRouter while the main thread uses the proxy.

**CLIProxyAPI tool name prefixing**: CLIProxyAPI can add a `proxy_` prefix to tool names with OAuth tokens. To disable this, add `"tool_prefix_disabled": true` to the Claude OAuth token file in the auth directory (e.g., `~/.cli-proxy-api/claude-<email>.json`).

---

## Database Backends

### SQLite (Default)

No additional configuration needed. Database created at `data/nymeria.db`.

```bash
DATABASE_BACKEND=sqlite
# Optional: customize path
# SQLITE_PATH=/path/to/nymeria.db
```

### PostgreSQL

For production deployments with multiple instances:

```bash
DATABASE_BACKEND=postgres
POSTGRES_URI=postgresql://user:password@localhost:5432/nymeria
```

### Memory (Testing)

State is lost on restart. Use for testing only:

```bash
DATABASE_BACKEND=memory
```
