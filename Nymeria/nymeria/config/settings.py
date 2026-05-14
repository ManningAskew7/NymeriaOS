"""Nymeria settings management using Pydantic."""

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

from .._runtime_paths import default_user_project_root


_PROJECT_ROOT_MARKERS: Tuple[Tuple[str, ...], ...] = (
    ("run.py", "nymeria/config/soul.md"),
    ("docker-compose.yml", "nymeria/config/settings.py"),
)


def _find_project_root(start: Path) -> Optional[Path]:
    """Find a Nymeria backend root by walking up from ``start``."""
    current = start.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        for markers in _PROJECT_ROOT_MARKERS:
            if all((candidate / marker).exists() for marker in markers):
                return candidate
    return None


def _get_project_root() -> Path:
    """Get project root, supporting PyInstaller frozen builds.

    Resolution order:
    1. NYMERIA_PROJECT_ROOT env var (set by Tauri launcher/package entrypoints)
    2. PyInstaller frozen exe: ~/.nymeria user runtime root
    3. Marker discovery above this module, then the current working directory
    4. Compatibility fallback: three levels up from this file
    """
    env_root = os.environ.get("NYMERIA_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return default_user_project_root()

    module_path = Path(__file__).resolve()
    discovered_root = _find_project_root(module_path.parent)
    if discovered_root:
        return discovered_root

    cwd_root = _find_project_root(Path.cwd())
    if cwd_root:
        return cwd_root

    return module_path.parent.parent.parent


PROJECT_ROOT = _get_project_root()
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ENV_FILENAMES = (".env", "config.env", ".env.docker")
DEFAULT_CORS_ORIGINS = (
    "http://localhost:1420,"
    "tauri://localhost,"
    "http://tauri.localhost,"
    "https://tauri.localhost,"
    "http://localhost:8000"
)
DEFAULT_USER_TIMEZONE = "UTC"
MAX_LLM_OUTPUT_TOKENS = 1_000_000
DEFAULT_LLM_FALLBACK_MODELS = "anthropic:claude-haiku-4-5-20251001"
ReasoningEffort = Literal["low", "medium", "high"]


def get_env_file_paths(project_root: Path | None = None) -> Tuple[Path, ...]:
    """Return environment files loaded for a runtime project root."""
    root = project_root or PROJECT_ROOT
    return tuple(root / filename for filename in ENV_FILENAMES)


def get_env_write_path(project_root: Path | None = None) -> Path:
    """Return the dotenv file that should receive runtime settings updates."""
    root = project_root or PROJECT_ROOT

    # Match load precedence: .env, config.env, then .env.docker. Updating the
    # highest-precedence existing file prevents lower files from being shadowed
    # after restart.
    for path in reversed(get_env_file_paths(root)):
        if path.exists():
            return path

    # Source checkouts conventionally use .env for light local setup. Packaged
    # installs use config.env under the writable runtime root.
    if _find_project_root(root) == root:
        return root / ".env"
    return root / "config.env"


class _NonEmptyEnvSource(EnvSettingsSource):
    """
    Env settings source that treats empty-string values as missing.

    Docker Compose expansions like ``${VAR:-}`` inject empty strings into
    the container environment even when the variable is unset upstream.
    Because pydantic-settings prioritizes env vars over .env files, those
    empty strings shadow real values in the .env file and silently break
    optional integrations (e.g. web search). Treating empty strings as
    missing here lets the dotenv source win the merge.
    """

    def __call__(self):
        return {k: v for k, v in super().__call__().items() if v != ""}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=tuple(str(path) for path in get_env_file_paths(PROJECT_ROOT)),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            _NonEmptyEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="before")
    @classmethod
    def empty_strings_to_none(cls, values):
        """Convert empty string env vars to None for Optional fields."""
        if isinstance(values, dict):
            for key, value in values.items():
                if value == "":
                    values[key] = None
        return values

    @model_validator(mode="after")
    def reject_wildcard_cors_with_credentials(self):
        """Reject wildcard CORS origins because the API allows credentials."""
        if "*" in self.cors_origins_list:
            raise ValueError(
                "CORS_ORIGINS cannot include '*' while credentialed CORS is enabled. "
                "List explicit origins instead."
            )
        return self

    # API Authentication — legacy shared key, retired. Kept as a field so
    # existing `.env.docker` values don't raise validation errors, but the
    # server no longer accepts it; authentication is per-user account
    # tokens only. See docs/accounts.md.
    nymeria_api_key: Optional[str] = Field(
        default=None,
        description="Deprecated — per-user account tokens are authoritative. Safe to delete from .env.",
    )

    # Service token — an admin-role Nymeria account token used by bots, the
    # ticker, the watchdog, and other trusted internal callers. Combined with
    # an ``X-Nymeria-Act-As: <user_id>`` header it lets shared infrastructure
    # make API calls on behalf of each user without holding their raw tokens.
    # Created via ``python run.py users add bot-service --role admin``.
    nymeria_service_token: Optional[str] = Field(
        default=None,
        description="Admin-role service token used by bots/ticker/watchdog for act-as calls",
    )

    # Data directory override (for Docker volumes)
    nymeria_data_dir: Optional[str] = Field(
        default=None,
        description="Override data directory path (useful for Docker volumes)"
    )

    # Redis Event Bus Configuration
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis connection URL (e.g., redis://localhost:6379)"
    )
    redis_enabled: bool = Field(
        default=False,
        description="Enable Redis event bus for cross-container communication"
    )

    # CORS Configuration (for remote frontends)
    cors_origins: str = Field(
        default=DEFAULT_CORS_ORIGINS,
        description="Comma-separated list of allowed CORS origins"
    )

    # Worker mode flag
    worker_mode: bool = Field(
        default=False,
        description="Run in worker mode (ticker only, no API server)"
    )

    # _PRV_A Google Sheets reference-data access. This is intentionally separate
    # from GOOGLE_OAUTH_CREDENTIALS, which is a user OAuth client for the
    # general Google Docs/Drive/Sheets tools.
    _prv_a_service_account_file: Optional[str] = Field(
        default=None,
        description="Path to a Google service account JSON file for _PRV_A reference spreadsheets",
    )

    # Messaging Platform Credentials - Telegram
    telegram_bot_token: Optional[str] = Field(
        default=None,
        description="Telegram bot token from @BotFather"
    )
    telegram_default_chat_id: Optional[str] = Field(
        default=None,
        description="Default Telegram chat ID for notifications"
    )
    telegram_bot_username: Optional[str] = Field(
        default=None,
        description="Public username of the Telegram bot (without @). Used to "
                    "build t.me/<bot>?start=... deep links in the desktop wizard. "
                    "Optional — the bot reports it on startup if not set.",
    )

    # Messaging Platform Credentials - Discord
    discord_webhook_url: Optional[str] = Field(
        default=None,
        description="Discord webhook URL for notifications"
    )
    discord_bot_token: Optional[str] = Field(
        default=None,
        description="Discord bot token for two-way communication"
    )
    discord_mode: Literal["gateway", "webhook"] = Field(
        default="gateway",
        description="Discord connection mode: gateway (WebSocket) or webhook"
    )
    discord_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Guild behavior: mention (only @Nymeria) or all (every message)"
    )

    # Messaging Platform Credentials - Twitch
    twitch_client_id: Optional[str] = Field(default=None, description="Twitch application Client ID")
    twitch_client_secret: Optional[str] = Field(default=None, description="Twitch application Client Secret")
    twitch_bot_access_token: Optional[str] = Field(default=None, description="Twitch bot user access token")
    twitch_bot_refresh_token: Optional[str] = Field(default=None, description="Twitch bot refresh token")
    twitch_bot_user_id: Optional[str] = Field(default=None, description="Twitch bot numeric user ID")
    twitch_broadcaster_token: Optional[str] = Field(default=None, description="Broadcaster's OAuth token (channel:bot scope)")
    twitch_broadcaster_refresh_token: Optional[str] = Field(default=None, description="Broadcaster's refresh token")
    twitch_channel: Optional[str] = Field(default=None, description="Twitch channel to join")
    twitch_system_prompt: Optional[str] = Field(
        default=None,
        max_length=50000,
        description="Optional initial Twitch thread system prompt. Existing thread config takes precedence.",
    )
    twitch_buffer_size: int = Field(default=500, ge=50, le=5000, description="Chat message ring buffer size")
    twitch_pulse_enabled: bool = Field(default=True, description="Enable periodic chat pulse")
    twitch_pulse_interval: int = Field(default=300, ge=60, le=3600, description="Seconds between pulse checks")
    twitch_pulse_min_messages: int = Field(default=10, ge=1, le=100, description="Minimum new messages before pulse fires")
    twitch_pulse_message_count: int = Field(default=100, ge=10, le=500, description="Messages to include in pulse context")
    twitch_command_context_count: int = Field(default=50, ge=5, le=200, description="Messages to include with !ask context")
    twitch_respond_mode: str = Field(default="command", description="Response mode: command (only !commands)")

    # Messaging Platform Credentials - Slack
    slack_webhook_url: Optional[str] = Field(
        default=None,
        description="Slack webhook URL for notifications"
    )
    slack_bot_token: Optional[str] = Field(
        default=None,
        description="Slack bot token for two-way communication"
    )

    # Microsoft Teams notifications (uses Outlook OAuth token + Graph API)
    teams_team_id: Optional[str] = Field(
        default=None,
        description="Microsoft Teams team ID for notifications"
    )
    teams_channel_id: Optional[str] = Field(
        default=None,
        description="Microsoft Teams channel ID for notifications"
    )
    teams_account_id: Optional[str] = Field(
        default=None,
        description="Outlook account ID to use for Teams (must have ChannelMessage.Send scope)"
    )
    outlook_default_account_id: Optional[str] = Field(
        default=None,
        description="Default Outlook account ID for email tools (used when agent doesn't specify one)"
    )

    # LLM Configuration
    llm_provider: Literal["openrouter", "openai", "anthropic"] = Field(
        default="anthropic", description="LLM provider"
    )
    llm_model: str = Field(
        default="claude-sonnet-4-6", description="Model identifier"
    )
    llm_fast_model: Optional[str] = Field(
        default=None,
        description="Fast/cheap model used by the CLI /fast shortcut",
    )
    llm_fallback_models: Optional[str] = Field(
        default=DEFAULT_LLM_FALLBACK_MODELS,
        description=(
            "Comma-separated ordered fallback model chain. Entries may be "
            "model IDs for the active provider or provider:model for "
            "anthropic/openai/openrouter."
        ),
    )
    llm_temperature: float = Field(default=1.0, ge=0.0, le=2.0)

    # Advanced LLM settings (optional - only sent if explicitly set)
    llm_max_tokens: Optional[int] = Field(
        default=None, ge=1, le=MAX_LLM_OUTPUT_TOKENS, description="Maximum output tokens"
    )
    llm_top_p: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="Nucleus sampling threshold"
    )
    llm_top_k: Optional[int] = Field(
        default=None, ge=1, le=100, description="Top-k sampling"
    )
    llm_frequency_penalty: Optional[float] = Field(
        default=None, ge=-2.0, le=2.0, description="Reduce repetition of token sequences"
    )
    llm_presence_penalty: Optional[float] = Field(
        default=None, ge=-2.0, le=2.0, description="Encourage new topics"
    )
    llm_reasoning_effort: Optional[ReasoningEffort] = Field(
        default=None, description="Reasoning effort for compatible models: low, medium, high"
    )
    llm_extended_thinking: bool = Field(
        default=False, description="Enable extended thinking/reasoning for compatible models"
    )
    llm_use_model_defaults: bool = Field(
        default=False,
        description="Use model-specific defaults for temperature/top_p/frequency_penalty instead of global values"
    )
    llm_base_url: Optional[str] = Field(
        default=None,
        description="Override base URL for LLM API (e.g., local proxy at http://localhost:8317/v1)"
    )
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = Field(
        default="responses",
        description="Default OpenAI-compatible API mode when no per-thread override is set: 'responses' or 'chat_completions'"
    )
    llm_stream_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Max retries for transient LLM call/stream failures before any model output is emitted"
    )
    llm_stream_retry_initial_delay: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        description="Initial backoff delay in seconds for transient LLM call/stream retries"
    )
    llm_stream_retry_max_delay: float = Field(
        default=8.0,
        ge=0.0,
        le=300.0,
        description="Maximum backoff delay in seconds for transient LLM call/stream retries"
    )

    # API Keys
    openai_api_key: Optional[str] = Field(default=None)
    anthropic_api_key: Optional[str] = Field(default=None)
    anthropic_direct_api_key: Optional[str] = Field(default=None, description="Direct Anthropic API key (pay-per-token), used when base_url is empty")
    openrouter_api_key: Optional[str] = Field(default=None)
    perplexity_api_key: Optional[str] = Field(default=None)
    perplexity_search_model: str = Field(default="sonar-pro", description="Default Perplexity model for web search")
    wolfram_alpha_app_id: Optional[str] = Field(default=None, description="Wolfram|Alpha AppID for wolfram_alpha_query")
    searxng_base_url: Optional[str] = Field(default=None, description="Base URL for a SearXNG instance")
    nasa_api_key: Optional[str] = Field(default=None, description="NASA API key for nasa_apod")
    openweathermap_api_key: Optional[str] = Field(default=None, description="OpenWeatherMap API key for weather tools")
    npm_registry_url: str = Field(default="https://registry.npmjs.org", description="npm registry base URL for npm tools")
    github_token: Optional[str] = Field(default=None, description="GitHub API token fallback for developer platform tools")
    github_api_base_url: str = Field(default="https://api.github.com", description="GitHub API base URL")
    gitlab_token: Optional[str] = Field(default=None, description="GitLab API token fallback for developer platform tools")
    gitlab_base_url: str = Field(default="https://gitlab.com/api/v4", description="GitLab API base URL or instance URL")
    bitly_token: Optional[str] = Field(default=None, description="Bitly API token fallback")
    bitly_base_url: str = Field(default="https://api-ssl.bitly.com/v4", description="Bitly API base URL")
    brandfetch_api_key: Optional[str] = Field(default=None, description="Brandfetch API key fallback")
    brandfetch_base_url: str = Field(default="https://api.brandfetch.io/v2", description="Brandfetch API base URL")
    marketstack_api_key: Optional[str] = Field(default=None, description="Marketstack API key fallback")
    marketstack_base_url: str = Field(default="https://api.marketstack.com/v1", description="Marketstack API base URL")
    deepl_api_key: Optional[str] = Field(default=None, description="DeepL API key fallback")
    deepl_api_plan: str = Field(default="pro", description="DeepL API plan: pro or free")
    deepl_base_url: Optional[str] = Field(default=None, description="DeepL API base URL override")
    todoist_api_key: Optional[str] = Field(default=None, description="Todoist API key fallback")
    todoist_base_url: str = Field(default="https://api.todoist.com/api/v1", description="Todoist API base URL")
    trello_api_key: Optional[str] = Field(default=None, description="Trello API key fallback")
    trello_api_token: Optional[str] = Field(default=None, description="Trello API token fallback")
    trello_base_url: str = Field(default="https://api.trello.com/1", description="Trello API base URL")
    asana_access_token: Optional[str] = Field(default=None, description="Asana personal access token fallback")
    asana_base_url: str = Field(default="https://app.asana.com/api/1.0", description="Asana API base URL")
    linear_api_key: Optional[str] = Field(default=None, description="Linear API key fallback")
    linear_api_url: str = Field(default="https://api.linear.app/graphql", description="Linear GraphQL API URL")
    embedding_api_key: Optional[str] = Field(
        default=None,
        description="API key for semantic embeddings. Separate from OPENAI_API_KEY so CLIProxy gatekeeper keys do not break embeddings.",
    )
    embedding_base_url: Optional[str] = Field(
        default=None,
        description="Optional OpenAI-compatible base URL for embeddings",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="1536-dimensional embedding model name for memory and skill semantic search",
    )

    # Gemini (document extraction for email attachments)
    gemini_api_key: Optional[str] = Field(default=None, description="Google Gemini API key for document extraction")
    gemini_extraction_model: str = Field(default="gemini-3-flash-preview", description="Gemini model for attachment text extraction")

    # MCP discovery registries
    mcp_registry_url: str = Field(
        default="https://registry.modelcontextprotocol.io",
        description="Base URL of the official MCP registry used by search_mcp/install_mcp_server",
    )
    smithery_api_key: Optional[str] = Field(
        default=None,
        description="Optional Smithery API key; unlocks private/verified listings when set",
    )

    # Database - SQLite by default, PostgreSQL optional for power users
    database_backend: Literal["sqlite", "postgres", "memory"] = Field(
        default="sqlite", description="Database backend: sqlite (default), postgres, or memory"
    )
    sqlite_path: Optional[str] = Field(
        default=None, description="SQLite database path (default: data/nymeria.db)"
    )
    postgres_uri: Optional[str] = Field(
        default=None, description="PostgreSQL connection URI (only if database_backend=postgres)"
    )

    @property
    def db_path(self) -> Path:
        """Get the SQLite database path."""
        if self.sqlite_path:
            path = Path(self.sqlite_path).expanduser()
            if path.is_absolute():
                return path
            return self.project_root / path
        return self.data_dir / "nymeria.db"

    # User Timezone
    user_timezone: str = Field(
        default=DEFAULT_USER_TIMEZONE,
        description="IANA timezone for the user (e.g., 'America/New_York', 'Europe/London')"
    )

    # Scheduler Configuration
    ticker_poll_interval: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Seconds between ticker polls for due tasks"
    )
    max_concurrent_autonomous: int = Field(
        default=5,
        ge=0,
        description="Max concurrent autonomous tasks (0 = unlimited)"
    )
    # Context Management Settings
    context_management: Literal["auto_compact", "sliding_window", "none"] = Field(
        default="auto_compact",
        description="Strategy: auto_compact (default), sliding_window (legacy), none"
    )
    compact_threshold: float = Field(
        default=0.8,
        ge=0.05,
        le=0.95,
        description="Trigger auto-compact at this percentage of context window"
    )
    compact_keep_messages: int = Field(
        default=4,
        ge=2,
        le=20,
        description="Minimum messages before compaction is allowed (summary replaces all)"
    )
    compact_model: Optional[str] = Field(
        default=None,
        description="Model for summarization (defaults to main model, can use cheaper)"
    )
    # Legacy sliding window (renamed for clarity)
    sliding_window_cycles: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Legacy: cycles to keep when using sliding_window mode"
    )

    # Concurrency
    lock_timeout: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Seconds to wait for per-thread lock before timing out"
    )
    tool_timeout: int = Field(
        default=300,
        ge=30,
        le=900,
        description="Max seconds a single tool/agent invocation can run before being terminated (default 5 minutes)"
    )
    tool_output_max_chars: int = Field(
        default=100000,
        ge=1000,
        le=2000000,
        description="Maximum characters stored for a single tool result; oversized results keep head and tail with a truncation marker"
    )

    # Watchdog/TODO Configuration
    watchdog_enabled: bool = Field(
        default=True,
        description="Enable watchdog to monitor TODO staleness"
    )
    watchdog_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Minutes between watchdog checks for stale TODOs"
    )
    todo_staleness_minutes: int = Field(
        default=20,
        ge=5,
        le=1440,
        description="Minutes without update before a TODO is considered stale"
    )
    todo_auto_archive_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Days after completion before TODOs are auto-archived"
    )
    activity_retention_hours: int = Field(
        default=12,
        ge=1,
        le=168,
        description="Hours to retain activity log entries (1-168)"
    )

    # Server Configuration
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    nymeria_debug: bool = Field(
        default=False,
        description="Enable debug-only server behavior such as FastAPI documentation routes",
    )
    nymeria_api_docs: bool = Field(
        default=False,
        description="Expose FastAPI Swagger, ReDoc, and OpenAPI schema routes",
    )

    @property
    def api_docs_enabled(self) -> bool:
        """Return whether interactive API docs/schema routes should be exposed."""
        return self.nymeria_debug or self.nymeria_api_docs

    # Log file rotation (used by all modes for the persistent file handler)
    service_log_file: str = Field(
        default="service.log",
        description="Log filename for the rotating file handler"
    )
    service_log_max_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10MB
        description="Max log file size before rotation"
    )
    service_log_backup_count: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of backup log files to keep"
    )

    # Voice / TTS Configuration
    tts_provider: Literal["none", "openai", "qwen3", "gemini", "cartesia"] = Field(
        default="none", description="TTS provider: none, openai, qwen3, gemini, cartesia"
    )
    tts_base_url: Optional[str] = Field(
        default=None,
        description="TTS API base URL (e.g., https://api.openai.com/v1 or http://localhost:8880/v1)"
    )
    tts_api_key: Optional[str] = Field(
        default=None, description="TTS API key (falls back to OPENAI_API_KEY if not set)"
    )
    tts_model: str = Field(default="tts-1-hd", description="TTS model name")
    tts_voice: str = Field(default="nova", description="TTS voice identifier")
    tts_output_format: str = Field(default="mp3", description="TTS output format: mp3, wav, opus, aac")
    tts_speed: float = Field(default=1.0, ge=0.25, le=4.0, description="TTS playback speed")

    # Voice / STT Configuration
    stt_provider: Literal["none", "openai", "faster-whisper"] = Field(
        default="none", description="STT provider: none, openai, faster-whisper"
    )
    stt_base_url: Optional[str] = Field(
        default=None,
        description="STT API base URL (e.g., https://api.openai.com/v1 or http://localhost:8003/v1)"
    )
    stt_api_key: Optional[str] = Field(
        default=None, description="STT API key (falls back to OPENAI_API_KEY if not set)"
    )
    stt_model: str = Field(default="gpt-4o-mini-transcribe", description="STT model name")
    stt_language: Optional[str] = Field(
        default=None, description="STT language hint (ISO 639-1, e.g., 'en')"
    )

    # Voice default thread
    voice_default_thread_id: Optional[str] = Field(
        default=None,
        description="Default thread ID for voice/watch interactions (uses 'watch-default' if not set)"
    )

    # FCM Push Notifications
    fcm_enabled: bool = Field(default=False, description="Enable FCM push notifications to registered devices")
    fcm_credentials_json: Optional[str] = Field(
        default=None, description="Path to Firebase service account JSON file"
    )

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    audit_log_enabled: bool = Field(default=True)

    # HTTP tool egress policy. Internal/private/link-local/metadata targets are
    # blocked by default; add exact host or host:port entries only when Nymeria
    # should intentionally reach local/self-hosted HTTP services.
    http_internal_allowlist: str = Field(
        default="",
        description="Comma-separated exact internal HTTP hosts or host:port pairs allowed for HTTP tools",
    )
    http_domain_allowlist: str = Field(
        default="",
        description="Optional comma-separated public domain allowlist for HTTP tools",
    )
    http_domain_blocklist: str = Field(
        default="",
        description="Comma-separated public domain blocklist for HTTP tools",
    )
    http_max_redirects: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Maximum redirects followed by HTTP tools",
    )
    http_allow_https_to_http_redirect: bool = Field(
        default=False,
        description="Allow HTTP tools to follow redirects from HTTPS to HTTP",
    )

    # Paths
    @property
    def project_root(self) -> Path:
        """Get the project root directory."""
        return PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        """
        Get the data directory.

        Uses nymeria_data_dir if set (for Docker volumes), otherwise falls back
        to project_root/data for local development.
        """
        if self.nymeria_data_dir:
            path = Path(self.nymeria_data_dir).expanduser()
            if path.is_absolute():
                return path
            return self.project_root / path
        return self.project_root / "data"

    @property
    def soul_path(self) -> Path:
        """Get the path to the soul.md system prompt."""
        return PACKAGE_ROOT / "config" / "soul.md"

    @property
    def logs_dir(self) -> Path:
        """Get the logs directory."""
        return self.data_dir / "logs"

    @property
    def users_dir(self) -> Path:
        """Get the users directory for profile storage."""
        return self.data_dir / "users"

    @property
    def backups_dir(self) -> Path:
        """Get the backups directory for self-modification."""
        return self.data_dir / "backups"

    @property
    def custom_tools_dir(self) -> Path:
        """Get the custom tools directory."""
        return self.data_dir / "custom_tools"

    @property
    def mcp_servers_dir(self) -> Path:
        """Get the MCP servers configuration directory."""
        return self.data_dir / "mcp_servers"

    @property
    def skills_dir(self) -> Path:
        """Get the Agent Skills data directory (user-installed + global skills)."""
        return self.data_dir / "skills"

    @property
    def bundled_skills_dir(self) -> Path:
        """Get the repo-bundled skills directory (ships with Nymeria)."""
        return PACKAGE_ROOT / "skills_bundled"

    @property
    def cors_origins_list(self) -> List[str]:
        """Get CORS origins as a list."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def get_api_key_for_provider(self) -> Optional[str]:
        """Get the API key for the configured LLM provider."""
        if self.llm_provider == "anthropic":
            if self.llm_base_url:
                return self.anthropic_api_key
            return self.anthropic_direct_api_key or self.anthropic_api_key

        key_map = {
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
        }
        return key_map.get(self.llm_provider)

    def load_soul(self) -> str:
        """Load the system prompt from soul.md."""
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8")
        return "You are Nymeria, a helpful AI assistant."

    def validate(self) -> Tuple[List[str], List[str]]:
        """
        Validate configuration settings.

        Returns:
            Tuple of (errors, warnings) lists.
            - errors: Critical issues that prevent startup
            - warnings: Non-critical issues that should be noted
        """
        errors: List[str] = []
        warnings: List[str] = []

        # NYMERIA_API_KEY is retired — authentication uses per-user account
        # tokens minted via ``python run.py users add``. The bootstrap admin
        # token is written to data/BOOTSTRAP_TOKEN.txt on first run. See
        # docs/accounts.md.
        #
        # Internal callers (bots, ticker, watchdog, slash_command, triggers)
        # need NYMERIA_SERVICE_TOKEN to call the API with X-Nymeria-Act-As.
        if not self.nymeria_service_token:
            warnings.append(
                "NYMERIA_SERVICE_TOKEN not set. Internal callers (bots, ticker, "
                "watchdog, trigger fires, slash commands) will be unable to "
                "authenticate. Create an admin account and paste its token:\n"
                "  python run.py users add bot-service@localhost --role admin --id bot-service"
            )

        # Check for LLM provider API key
        provider_key = self.get_api_key_for_provider()
        if not provider_key:
            provider_env_var = f"{self.llm_provider.upper()}_API_KEY"
            errors.append(
                f"No API key for LLM provider '{self.llm_provider}'.\n"
                f"  Set {provider_env_var} in your .env file.\n"
                f"  Get an API key from your provider's website."
            )

        # Check database backend configuration
        if self.database_backend == "postgres" and not self.postgres_uri:
            errors.append(
                "DATABASE_BACKEND is 'postgres' but POSTGRES_URI is not set.\n"
                "  Either set POSTGRES_URI or change DATABASE_BACKEND to 'sqlite'."
            )

        # Twitch bot warning
        if self.twitch_bot_access_token and not provider_key:
            warnings.append(
                "TWITCH_BOT_ACCESS_TOKEN is set but no LLM API key configured.\n"
                "  The Twitch bot will not be able to process messages."
            )

        # Discord bot warning
        if self.discord_bot_token and not provider_key:
            warnings.append(
                "DISCORD_BOT_TOKEN is set but no LLM API key configured.\n"
                "  The Discord bot will not be able to process messages."
            )

        # Warnings for optional features
        if not self.perplexity_api_key:
            warnings.append(
                "PERPLEXITY_API_KEY not set. Web search will be unavailable.\n"
                "  Get an API key from https://perplexity.ai if you want web search."
            )

        return errors, warnings


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
