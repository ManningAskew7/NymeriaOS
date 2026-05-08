# Nymeria Configuration

All configuration is done via environment variables. Source checkouts load
`.env`, `config.env`, and `.env.docker` from the backend root if present;
when the same variable appears in multiple files, `.env.docker` has the
highest dotenv-file precedence. Packaged `nymeria` installs use
`~/.nymeria/config.env` by default. Copy `.env.docker.example` to
`.env.docker` for the full local/Docker template, run `nymeria init` for
packaged setup, or create `.env` manually for a lighter source-checkout setup.

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Environment Variables

### LLM Configuration

These variables are deployment-wide server defaults, not per-user account preferences. In a multi-user deployment, every thread that does not set a per-thread LLM override inherits the same `LLM_PROVIDER`, `LLM_MODEL`, API key, and `LLM_BASE_URL`; if an admin changes them through Settings → LLM, the change affects all users on that server. User-specific routing is currently done with per-thread overrides.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `anthropic` | LLM provider: `openrouter`, `anthropic`, `openai` |
| `LLM_MODEL` | Yes | `claude-sonnet-4-20250514` | Model identifier for the provider |
| `LLM_TEMPERATURE` | No | `1.0` | Sampling temperature (0.0 - 2.0) |

### Advanced LLM Settings (Optional)

These settings give power users fine-grained control over LLM behavior. All are optional and only sent to the API if explicitly set.

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `LLM_MAX_TOKENS` | (model limit) | 1 - 1,000,000 | Maximum output tokens |
| `LLM_TOP_P` | (provider default) | 0.0 - 1.0 | Nucleus sampling threshold |
| `LLM_TOP_K` | (provider default) | 1 - 100 | Top-k sampling (limits vocabulary per step) |
| `LLM_FREQUENCY_PENALTY` | (provider default) | -2.0 - 2.0 | Reduce repetition of token sequences |
| `LLM_PRESENCE_PENALTY` | (provider default) | -2.0 - 2.0 | Encourage new topics |
| `LLM_REASONING_EFFORT` | (none) | low/medium/high | For reasoning models (o1, Claude with thinking); invalid values fail settings validation |
| `LLM_EXTENDED_THINKING` | `false` | true/false | Enable extended thinking/reasoning for compatible models |
| `LLM_USE_MODEL_DEFAULTS` | `false` | true/false | Use model-specific defaults for temperature, top_p, and frequency penalty instead of global values. When enabled, these params are not sent to the API — the provider applies the model's own optimal defaults. |
| `LLM_BASE_URL` | (provider default) | URL | Override API endpoint for `openrouter`, `openai`, or `anthropic` providers. For `anthropic` CLIProxy, use the root URL with no `/v1` suffix because `ChatAnthropic` appends `/v1/messages`; for `openai`/Codex CLIProxy, use the OpenAI-compatible `/v1` URL. Leave unset to use the provider's standard URL. |
| `OPENAI_API_MODE` | `responses` | responses/chat_completions | API mode for OpenAI-compatible providers (`openai` and `openrouter`). `responses` is the default and recommended path for thinking/reasoning models; `chat_completions` is an explicit compatibility override and is not recommended if thinking is enabled. |
| `LLM_STREAM_MAX_RETRIES` | `2` | 0 - 10 | Retries for transient LLM call/stream failures. Streaming retries only happen before any model chunk is emitted. |
| `LLM_STREAM_RETRY_INITIAL_DELAY` | `1.0` | 0 - 60 | Initial retry backoff delay in seconds |
| `LLM_STREAM_RETRY_MAX_DELAY` | `8.0` | 0 - 300 | Maximum retry backoff delay in seconds |

**Note:** For OpenRouter, Nymeria uses `supported_parameters` from model metadata to automatically skip unsupported params (e.g., reasoning config for non-reasoning models). This prevents silent failures.

**Also note:** `LLM_EXTENDED_THINKING`, `LLM_USE_MODEL_DEFAULTS`, `OPENAI_API_MODE`, and provider-aware `LLM_BASE_URL` overrides are implemented in settings and runtime behavior, so they are safe to rely on even though some older docs may mention proxy behavior separately. Nymeria does not automatically fall back from Responses API to Chat Completions if a provider rejects the request; switch `OPENAI_API_MODE=chat_completions` explicitly when you need the older endpoint. OpenRouter Responses reasoning is displayed only when the provider emits plaintext reasoning fields; malformed inline `<think>` text that arrives as normal answer text is stripped from display and replay.

### API Keys

Set the API key for your chosen provider:

| Variable | Provider | Required |
|----------|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic | If using `anthropic` provider |
| `ANTHROPIC_DIRECT_API_KEY` | Anthropic | Optional direct Anthropic `sk-ant-*` key. Used only when the effective Anthropic base URL is empty/direct; CLIProxy Anthropic calls continue using `ANTHROPIC_API_KEY` (`cpx-*`). |
| `OPENAI_API_KEY` | OpenAI | If using `openai` provider; optional otherwise for OpenAI image generation, STT, and OpenAI-backed tools |
| `GEMINI_API_KEY` | Google Gemini | Optional; enables Gemini image generation, Gemini TTS, and Gemini attachment extraction |
| `OPENROUTER_API_KEY` | OpenRouter | If using `openrouter` provider |
| `EMBEDDING_API_KEY` | OpenAI-compatible embeddings | Optional; enables semantic memory/skill search. Keep separate from CLIProxy `OPENAI_API_KEY` values. |
| `EMBEDDING_BASE_URL` | OpenAI-compatible embeddings | Optional custom `/v1` base URL for embeddings |
| `EMBEDDING_MODEL` | OpenAI-compatible embeddings | Optional; defaults to `text-embedding-3-small`; must return 1536-dimensional vectors |
| `PERPLEXITY_API_KEY` | Perplexity | Required for `web_search` tool |

`nymeria init` can collect these optional capability keys during first-run
setup. It validates the selected provider/model/key combination with a small
LLM API call before writing `config.env`. In non-interactive mode, pass
`--embedding-api-key`, `--openai-api-key`, `--gemini-api-key`, or
`--perplexity-api-key`; keys that are not supplied are left out of
`config.env`. Add `--skip-llm-test` only for deliberate offline/scripted
setup where provider access will be verified separately.

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_BACKEND` | `sqlite` | Backend type: `sqlite`, `postgres`, or `memory` |
| `SQLITE_PATH` | `<data_dir>/nymeria.db` | SQLite database file location. Relative custom paths resolve from the runtime project root |
| `POSTGRES_URI` | - | PostgreSQL connection string (if using postgres) |
| `USER_TIMEZONE` | `UTC` | IANA timezone used for time context and absolute schedule parsing. Docker also mirrors this into `TZ` so OS-level time output stays aligned. |

### API Server and Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_API_KEY` | - | **Deprecated / ignored.** Formerly a shared bearer token; authentication now uses per-user account tokens. Safe to delete from `.env.docker`. See `docs/accounts.md`. |
| `NYMERIA_SERVICE_TOKEN` | mode-required | Admin-role Nymeria account token used by bots, ticker, watchdog, trigger-fires, slash commands, and the public MCP thin client for X-Nymeria-Act-As calls. `run.py` fails fast without it for `worker`, `discord-bot`, `telegram-bot`, `twitch-bot`, `watchdog`, `mcp`, and `service run`; local `api`, `cli`, and `users` development can still start without it. Created via `python run.py users add --role admin`. See `docs/accounts.md`. |
| `NYMERIA_API_URL` | auto | Local API URL for thin clients and in-process tools (MCP server, `slash_command`). Defaults to Docker service URLs when applicable, otherwise `http://localhost:8000` |
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8000` | Server port |
| `NYMERIA_API_DOCS` | `false` | Expose FastAPI Swagger UI, ReDoc, and `/openapi.json`. Disabled by default for beta deployments; changing it requires an API restart |
| `NYMERIA_DEBUG` | `false` | Enables debug-only server behavior, including API docs/schema routes. Use only in trusted local development |
| `CORS_ORIGINS` | `http://localhost:1420,tauri://localhost,http://localhost:8000` | Comma-separated allowed CORS origins. Wildcard origins are rejected because credentialed CORS is enabled |
| `NYMERIA_DATA_DIR` | `<project_root>/data` | Override data directory path. For pipx/wheel installs, the project root defaults to `~/.nymeria`, so the effective default is `~/.nymeria/data` |
| `NYMERIA_PROJECT_ROOT` | auto-detected | Override runtime project root resolution. Source launches use the checkout's `Nymeria/` root; packaged/frozen launches default to `~/.nymeria` |

Project-root auto-detection first honors `NYMERIA_PROJECT_ROOT`. Source
launches walk upward looking for Nymeria backend markers such as `run.py`,
`docker-compose.yml`, and `nymeria/config/soul.md`; the older fixed-depth path
from `nymeria/config/settings.py` remains only as a compatibility fallback.
Wheel/pipx installs and manual frozen backend builds without an explicit
override use the packaged runtime root, `~/.nymeria/`, for config and writable
data. Set `NYMERIA_PROJECT_ROOT` explicitly if a packaged backend deployment
needs checkout-style paths or separates the Python package from the runtime
config directory.

The `nymeria` console script bootstraps this automatically. When it runs from a
source checkout or editable install, it uses the checkout's `Nymeria/` backend
root. When it runs from a wheel/pipx install, it uses `~/.nymeria/` for
`config.env`, `data/`, and logs while loading bundled package assets such as
`nymeria/config/soul.md` from the installed Python package. `nymeria init`
writes an explicit `NYMERIA_DATA_DIR=<root>/data` line to `config.env`, so a
later root move should update that value or rerun `nymeria init --root ...`.
The beta Windows desktop release is client-only: it does not bundle a backend
executable, does not read or write backend config files, and does not set
`NYMERIA_PROJECT_ROOT` for a local backend process.

Run `nymeria doctor` after `nymeria init` or after manual config edits to check
the effective Python version, config files, data directory, LLM connectivity,
SQLite/Postgres state, optional Redis/voice setup, bundled frontend, and API
port. Use `nymeria doctor --skip-llm-test` when diagnosing an offline system or
when provider credentials are intentionally unavailable.

Runtime settings updates choose the highest-precedence existing config file:
`.env.docker`, then `config.env`, then `.env`. If no config file exists yet,
source checkouts create `.env`; packaged installs create `config.env`.

### Browser Tools

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_FORCE_FALLBACK` | `false` | Skip Playwright and use requests+BeautifulSoup fallback mode for browser navigation/content extraction |
| `BROWSER_HEADLESS` | auto | Force Playwright headless mode with `true` or visible mode with `false`; unset auto-detects Docker/headless Linux |
| `BROWSER_VERIFY_SSL` | `true` | Verify TLS certificates for fallback HTTP requests. Set to `false` only for trusted environments with known TLS interception |

### Redis (Docker Only)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_ENABLED` | `false` | Enable Redis event bus for cross-container communication |
| `REDIS_PASSWORD` | required in Docker | Redis password used by the Compose Redis service and the generated `REDIS_URL`. Use a URL-safe value such as `openssl rand -hex 32`. |
| `REDIS_URL` | - | Redis connection URL. Docker Compose generates `redis://:<REDIS_PASSWORD>@redis:6379/0`; local non-Docker development can use `redis://localhost:6379` if Redis auth is disabled. Startup logs redact credentials from this URL. |

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
| `TEAMS_TEAM_ID` | - | Microsoft Teams team ID for notifications |
| `TEAMS_CHANNEL_ID` | - | Microsoft Teams channel ID for notifications |
| `TEAMS_ACCOUNT_ID` | - | Outlook account ID for Teams (must have ChannelMessage.Send) |
| `OUTLOOK_DEFAULT_ACCOUNT_ID` | - | Default Outlook account for email tools |
| `MICROSOFT_MCP_CLIENT_ID` | `8ad36cab...` | Azure AD app client ID for Outlook/Teams OAuth |
| `GOOGLE_OAUTH_CREDENTIALS` | - | Path to Google OAuth credentials JSON file |
| `_PRV_A_SERVICE_ACCOUNT_FILE` | - | Path to a Google service account JSON file for _PRV_A reference Sheets |
| `PERPLEXITY_API_KEY` | - | Perplexity API key for web_search tool |
| `TWITCH_CLIENT_ID` | - | Twitch application Client ID |
| `TWITCH_CLIENT_SECRET` | - | Twitch application Client Secret |
| `TWITCH_BOT_ACCESS_TOKEN` | - | Bot's OAuth access token |
| `TWITCH_BOT_REFRESH_TOKEN` | - | Bot's OAuth refresh token |
| `TWITCH_BOT_USER_ID` | - | Bot's numeric Twitch user ID |
| `TWITCH_BROADCASTER_TOKEN` | - | Broadcaster's OAuth token (channel:bot scope) |
| `TWITCH_BROADCASTER_REFRESH_TOKEN` | - | Broadcaster's refresh token |
| `TWITCH_CHANNEL` | - | Twitch channel to join (required when `twitch-bot` runs) |
| `TWITCH_SYSTEM_PROMPT` | - | Optional initial system prompt for the Twitch thread; existing thread config takes precedence |
| `TWITCH_BUFFER_SIZE` | `500` | Chat message ring buffer size (50-5000) |
| `TWITCH_PULSE_ENABLED` | `true` | Enable periodic chat evaluation |
| `TWITCH_PULSE_INTERVAL` | `300` | Seconds between pulse checks (60-3600) |
| `TWITCH_PULSE_MIN_MESSAGES` | `10` | Minimum new messages before a pulse fires |
| `TWITCH_PULSE_MESSAGE_COUNT` | `100` | Messages to include in pulse context |
| `TWITCH_COMMAND_CONTEXT_COUNT` | `50` | Messages to include with !ask context |
| `TWITCH_RESPOND_MODE` | `command` | Response mode (command = only !commands) |

### Log File Rotation

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_LOG_FILE` | `service.log` | Rotating log filename (all modes) |
| `SERVICE_LOG_MAX_BYTES` | `10485760` | Rotate log after this many bytes |
| `SERVICE_LOG_BACKUP_COUNT` | `5` | Number of rotated log backups to keep |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AUDIT_LOG_ENABLED` | `true` | Log redacted HTTP/API primitive tool events to audit log |

### HTTP Tool Egress Policy

These settings apply to `http_request`, `api_discover`, and saved custom HTTP
tools. The model cannot override them per call.

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_INTERNAL_ALLOWLIST` | - | Comma-separated exact internal hosts or `host:port` pairs that HTTP tools may reach, e.g. `host.docker.internal:1420,homeassistant.local:8123` |
| `HTTP_DOMAIN_ALLOWLIST` | - | Optional comma-separated public-domain allowlist. Supports exact domains and `*.example.com` wildcards. Empty means public domains are allowed unless blocked. |
| `HTTP_DOMAIN_BLOCKLIST` | - | Comma-separated public-domain blocklist. Supports exact domains and `*.example.com` wildcards. |
| `HTTP_MAX_REDIRECTS` | `5` | Maximum redirects followed by HTTP tools (0-20) |
| `HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT` | `false` | Whether HTTP tools may follow redirects from `https://` to `http://` |

By default HTTP tools are public-internet-only. Loopback, private, link-local,
reserved, unspecified, multicast, and metadata targets are blocked after DNS
resolution unless the target is a non-metadata host explicitly listed in
`HTTP_INTERNAL_ALLOWLIST`.

### Autonomous Operation

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKER_POLL_INTERVAL` | `5` | Seconds between polls for due tasks (1-60) |
| `MAX_CONCURRENT_AUTONOMOUS` | `5` | Max concurrent autonomous tasks (`0` = unlimited) |
| `LOCK_TIMEOUT` | `120` | Seconds to wait on per-thread lock before timing out |
| `TOOL_TIMEOUT` | `300` | Max seconds a tool/sub-agent invocation may run |
| `TOOL_OUTPUT_MAX_CHARS` | `100000` | Max characters stored for one tool result. Larger outputs keep the first ~75k and last ~25k characters with a truncation marker. |

### Context Management

Nymeria automatically manages conversation context to prevent overflow. The default `auto_compact` mode summarizes conversations when approaching the model's context limit.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_MANAGEMENT` | `auto_compact` | Strategy: `auto_compact`, `sliding_window`, or `none` |
| `COMPACT_THRESHOLD` | `0.8` | Trigger compaction at this fraction of the model context window (0.05-0.95). Example: `0.38` is about 400k tokens on GPT-5.5's 1.05M window. |
| `COMPACT_KEEP_MESSAGES` | `4` | Minimum messages before compaction is allowed |
| `COMPACT_MODEL` | (main model) | Optional cheaper model for summarization |
| `SLIDING_WINDOW_CYCLES` | `5` | Legacy: cycles to keep when using `sliding_window` mode |

**Context Management Modes:**

- **`auto_compact`** (default): When token usage reaches the threshold, Nymeria:
  1. Asks the agent to summarize the conversation (it already has full context)
  2. Agent saves important facts to persistent memory via `memory_add(scope="global", ...)`
  3. Clears the conversation and persists a visible compaction notice with the summary
  4. On async `/chat` streams, compacts before the next provider call when prior usage already crossed the trigger; post-turn async compaction emits `compacting`/`compacted` and streams the resumed assistant continuation. Sync/manual paths attach the summary to the next user message.

- **`sliding_window`**: Legacy mode that simply removes old messages, keeping the last N cycles

- **`none`**: No automatic context management (manual `/compact` still available)

### Watchdog, TODO, and Push Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_ENABLED` | `true` | Enable watchdog to monitor TODO staleness |
| `WATCHDOG_INTERVAL_MINUTES` | `5` | Minutes between watchdog checks (1-60) |
| `TODO_STALENESS_MINUTES` | `20` | Minutes without update before TODO is stale (5-1440) |
| `TODO_AUTO_ARCHIVE_DAYS` | `7` | Days after completion before the ticker removes completed TODOs from the active TODO JSON list (1-30) |
| `ACTIVITY_RETENTION_HOURS` | `12` | Hours to retain activity log entries (1-168) |
| `FCM_ENABLED` | `false` | Enable Firebase Cloud Messaging push notifications |
| `FCM_CREDENTIALS_JSON` | - | Path to Firebase service account JSON |
| `NYMERIA_WATCHDOG_DISABLED` | - | Set to `1` / `true` / `yes` at runtime to mute the watchdog without restarting. See also the file flag below. |

**Wear OS Firebase client config:**

The Wear OS companion app intentionally tracks
`nymeria-watch/app/google-services.json`. That file is the Android client
configuration consumed by the Google Services Gradle plugin for Firebase Cloud
Messaging, and it contains Firebase project/app identifiers plus the
Firebase-provisioned Android API key. Firebase documents these app config
values as safe to include in client code or checked-in configuration when the
key is restricted to Firebase services. Do not add server credentials or
non-Firebase Google API keys to this file; use a separate restricted key for
non-Firebase APIs.

The private server-side FCM credential remains
`Nymeria/firebase-service-account.json`, which is managed separately with
git-crypt and wired into Docker via `FCM_CREDENTIALS_JSON`.

**Watchdog runtime kill switches** (disable without restart):

- **Env var**: `NYMERIA_WATCHDOG_DISABLED=1` (re-read on every poll cycle)
- **File flag**: `{data_dir}/flags/watchdog-off` — persistent across container restarts because `/data` is a Docker volume. Create it with `docker exec nymeria-watchdog touch /data/flags/watchdog-off`; remove with `rm` to re-enable.

In Docker deployments the watchdog runs in its own container (`nymeria-watchdog`, defined in `docker-compose.yml`). It's a thin client that calls the API over HTTP — no `NymeriaAgent` in the watchdog process. To disable it entirely, set `WATCHDOG_ENABLED=false` and restart, or simply don't start the service (`docker compose stop watchdog`). See `docs/architecture.md` §4.2 for details.

### Voice (TTS / STT)

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_PROVIDER` | `none` | TTS provider: `none`, `gemini`, `openai`, `qwen3` |
| `TTS_BASE_URL` | (per provider) | TTS API base URL. Not used for Gemini (uses SDK). Defaults: OpenAI=`https://api.openai.com/v1`, Qwen3=`http://localhost:8880/v1` |
| `TTS_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for OpenAI/Qwen3 TTS. Gemini uses `GEMINI_API_KEY` instead |
| `TTS_MODEL` | `tts-1-hd` | Model name. Gemini: `gemini-3.1-flash-tts-preview`, OpenAI: `tts-1` / `tts-1-hd` |
| `TTS_VOICE` | `nova` | Voice identifier. Gemini: `Kore`, `Puck`, `Charon`, `Algenib`, `Leda`, `Orus`, `Zephyr` (30 total). OpenAI: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |
| `TTS_OUTPUT_FORMAT` | `mp3` | Output format: mp3, wav, opus, aac. Gemini always outputs MP3 (converted from WAV server-side) |
| `TTS_SPEED` | `1.0` | Playback speed 0.25-4.0. Not applicable for Gemini |
| `STT_PROVIDER` | `none` | STT provider: `none`, `openai`, `faster-whisper` |
| `STT_BASE_URL` | (per provider) | STT API base URL |
| `STT_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for STT |
| `STT_MODEL` | `gpt-4o-mini-transcribe` | STT model name |
| `STT_LANGUAGE` | - | Language hint (ISO 639-1, e.g., `en`) |
| `VOICE_DEFAULT_THREAD_ID` | - | Default thread for voice/watch interactions (falls back to `watch-default`) |

**Gemini TTS** requires `GEMINI_API_KEY` (also used for document extraction). Supports 200+ inline audio tags for expressive speech — e.g., `[whispers]`, `[excitedly]`, `[sighs]`. See [Gemini TTS prompting guide](https://ai.google.dev/gemini-api/docs/speech-generation).

---

## Data Directories

Nymeria uses the following directories under `settings.data_dir`. In source
checkouts that defaults to `Nymeria/data/`; in packaged installs it defaults to
`~/.nymeria/data/`. `nymeria init` creates this layout, writes
`~/.nymeria/config.env`, and initializes the first admin token.

| Directory | Purpose |
|-----------|---------|
| `data/nymeria.db` | SQLite conversation database |
| `data/accounts.db` | Account users, tokens, and chat-app bindings |
| `data/todo_schedule.db` | SQLite scheduled TODO index for polling |
| `data/BOOTSTRAP_TOKEN.txt` | First-run admin bootstrap token, written with mode 0600 |
| `data/todos/` | TODO list storage (`{user_id}.json`) |
| `data/logs/` | HTTP/API primitive tool audit logs (`audit_YYYYMMDD.jsonl`) |
| `data/users/` | User profiles, memories, thread configs, activity logs, triggers |
| `data/backups/` | Self-modification backups |
| `data/custom_tools/` | Custom tool definitions (`{tool_id}.json`) |
| `data/mcp_servers/` | MCP server configuration storage |
| `data/notifications/` | User notification storage |

These directories and files are created automatically on first run.

---

## System Prompt (soul.md)

Located at `nymeria/config/soul.md` in the source tree or installed package.
This file defines Nymeria's:
- Personality and tone
- Capabilities and limitations
- Guidelines for tool usage
- Autonomous behavior rules

For source checkouts, edit this file to customize how Nymeria responds.
Packaged installs load the bundled package copy; keep local personality edits in
source or a custom package build until user-editable packaged prompts are added.
Changes take effect on agent restart.

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
# OPENAI_API_MODE=responses           # OpenAI-compatible mode: responses or chat_completions
# LLM_STREAM_MAX_RETRIES=2            # Retry transient failures before chunks stream
# LLM_STREAM_RETRY_INITIAL_DELAY=1.0
# LLM_STREAM_RETRY_MAX_DELAY=8.0
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
# SQLITE_PATH=/path/to/nymeria.db  # Uncomment to customize path

# API Server
# Authentication uses per-user account tokens; the bootstrap admin token is
# written to <data_dir>/BOOTSTRAP_TOKEN.txt on first boot. See docs/accounts.md.
# NYMERIA_SERVICE_TOKEN is the admin service token used by bots/ticker/watchdog
# (with X-Nymeria-Act-As) for per-user routing.
NYMERIA_SERVICE_TOKEN=nym_<admin-service-token>
API_HOST=0.0.0.0
API_PORT=8000
# NYMERIA_API_DOCS=false            # Set true only in trusted local development
# NYMERIA_DEBUG=false               # Also enables API docs when true

# Logging
LOG_LEVEL=INFO
AUDIT_LOG_ENABLED=true

# HTTP tool egress policy (optional - defaults shown)
# HTTP_INTERNAL_ALLOWLIST=          # e.g. host.docker.internal:1420
# HTTP_DOMAIN_ALLOWLIST=
# HTTP_DOMAIN_BLOCKLIST=
# HTTP_MAX_REDIRECTS=5
# HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT=false

# Autonomous Operation (optional - defaults shown)
# TICKER_POLL_INTERVAL=5
# MAX_CONCURRENT_AUTONOMOUS=5

# Context Management (optional - defaults shown)
# CONTEXT_MANAGEMENT=auto_compact  # auto_compact, sliding_window, or none
# COMPACT_THRESHOLD=0.8            # Trigger at 80% of context limit (0.05-0.95)
# COMPACT_MODEL=                   # Use cheaper model for summarization
# SLIDING_WINDOW_CYCLES=5          # For legacy sliding_window mode

# Tool output safety
# TOOL_OUTPUT_MAX_CHARS=100000     # Max stored characters per tool result

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
# CORS_ORIGINS=http://localhost:1420,tauri://localhost,http://localhost:8000
# Add exact LAN/production frontend origins as needed. Wildcard origins are rejected.
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
- `claude-opus-4-7` (current flagship — see `docs/cliproxy.md` "Claude 4.7 compatibility" for the thinking/sampling-param caveats the provider already handles)
- `claude-opus-4-6` (previous flagship)
- `claude-opus-4-20250514`
- `claude-sonnet-4-20250514` (balanced)
- `claude-haiku-3-5-20241022` (fastest)

### OpenAI

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
EMBEDDING_API_KEY=sk-...   # Optional; used by memory/skill semantic search
```

### OpenRouter

```bash
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-sonnet-4
OPENROUTER_API_KEY=sk-or-...
```

OpenRouter provides access to many models from different providers through a unified API.

Nymeria defaults OpenRouter to OpenRouter's beta Responses API (`/api/v1/responses`) with `store=false`. OpenRouter's Responses API is stateless, so Nymeria sends the full checkpointed conversation history on each request instead of using `previous_response_id`. To use the older `/chat/completions` endpoint, set `OPENAI_API_MODE=chat_completions` globally or choose **Chat Completions** in the global/per-thread API Mode selector. Errors from the beta Responses endpoint are surfaced directly so the mode choice stays explicit.

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
LLM_MODEL=claude-opus-4-7             # Must match a model in proxy's Claude registry
LLM_BASE_URL=http://localhost:8317    # No /v1 suffix — ChatAnthropic appends /v1/messages
ANTHROPIC_API_KEY=nymeria-local-dev-key  # Proxy auth key (matches api-keys in proxy config)
```

When `LLM_BASE_URL` is set for the `anthropic` provider, `ChatAnthropic` is configured with:
- `anthropic_api_url` pointed at the proxy
- A `User-Agent: claude-cli/2.1.113` header that tells CLIProxyAPI v6.9.36 to skip system prompt cloaking (so Nymeria's own `soul.md` is preserved)
- Loop-local async HTTP clients so cached graph/model objects are safe when regular API chat and callable/autonomous bridge execution use different asyncio event loops

#### OpenAI-Compatible / Codex OAuth

Uses Nymeria's `ChatOpenAIWithReasoning` subclass pointed at the proxy's OpenAI-compatible endpoint. The base URL must include `/v1`; otherwise Responses mode posts to `/responses` and CLIProxy returns `404 page not found`.

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.5                      # Model name from the proxy's /v1/models
OPENAI_API_MODE=responses
OPENAI_API_KEY=cpx-latest-local-test   # Proxy gatekeeper key, not a hosted OpenAI key
LLM_BASE_URL=http://localhost:8317/v1  # Proxy endpoint (with /v1 suffix)
EMBEDDING_API_KEY=sk-...               # Optional hosted/local embeddings key; do not use cpx-* here
```

For GPT-5.5 through Codex OAuth, run the sidecar documented in `docs/cliproxy.md`. To route individual threads, use **Thread Settings → Model → OpenAI (Custom base URL)** and set the thread-level Base URL/API Key fields; the default OpenAI API mode is `Responses API`, with `Chat Completions` available only as a compatibility override and not recommended if thinking is enabled. To route the whole deployment, use **Settings → LLM → OpenAI (Custom base URL)** and keep `OPENAI_API_MODE=responses` in `.env.docker`. Per-thread overrides honor `provider`, `base_url`, `api_key`, and `openai_api_mode` — the API key is the CLIProxy gatekeeper key (e.g. `cpx-latest-local-test`), not an upstream OpenAI key.

OpenAI-compatible providers also receive Nymeria-managed loop-local `http_async_client` pools. This bypasses LangChain's process-global async `httpx` client cache so direct OpenAI, OpenRouter, and CLIProxy/Codex models remain safe when a cached thread graph is used from both the FastAPI event loop and the sync stream-bridge loop.

**Provider-aware base URL**: When `LLM_BASE_URL` is set globally, it applies to all threads using the global provider. Threads with a per-thread provider override to a *different* provider (e.g., `openrouter`) ignore the global base URL and use the provider's standard endpoint. This allows callable threads to route through OpenRouter while the main thread uses the proxy.

The global LLM settings are intentionally not account-scoped. Two users on the same Nymeria server cannot have different "global" providers; the last admin save wins for the deployment. To give one user's thread a different provider, configure that thread's LLM override instead.

**CLIProxyAPI tool name prefixing**: CLIProxyAPI can add a `proxy_` prefix to tool names with OAuth tokens. To disable this, add top-level `"tool_prefix_disabled": true` to the Claude OAuth token file in the auth directory (e.g., `~/.cli-proxy-api/claude-<email>.json`) and restart CLIProxy. `Nymeria/tools/check_cliproxy_cloak.py --auth-dir <auth-dir>` fails loudly when an active Claude auth file is missing the flag.

---

## Database Backends

### SQLite (Default)

No additional configuration needed. The database is created at
`<data_dir>/nymeria.db`, which is `Nymeria/data/nymeria.db` in a source
checkout and `~/.nymeria/data/nymeria.db` in a packaged install unless
`NYMERIA_DATA_DIR` or `SQLITE_PATH` overrides it.

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

Docker installs the PostgreSQL checkpoint dependencies through
`requirements-docker.txt`, which includes `requirements-postgres.txt` rather
than duplicating those package entries. Docker images install runtime
requirements only; backend tests, coverage, and linting use
`requirements-dev.txt` so `pytest`, `pytest-cov`, and `ruff` do not ship in
production images. For local development, install the relevant files
explicitly after the base requirements:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install -r requirements-postgres.txt
```

The default SQLite checkpoint and local vector-search dependencies live in
`requirements-sqlite.txt`, and `requirements.txt` includes that file so the
zero-setup local backend remains the default install path.

### Memory (Testing)

State is lost on restart. Use for testing only:

```bash
DATABASE_BACKEND=memory
```
