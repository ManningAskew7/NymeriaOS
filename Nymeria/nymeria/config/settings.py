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
from .llm_providers import (
    get_llm_provider_spec,
    normalize_llm_provider,
    provider_requires_api_key,
    resolve_provider_api_key,
)


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
    nymeria_public_url: Optional[str] = Field(
        default=None,
        description=(
            "Public browser base URL for hosted credential setup links, "
            "for example https://nymeria.example.com"
        ),
    )
    nymeria_error_report_email: Optional[str] = Field(
        default=None,
        description=(
            "Destination address for the in-app 'report a problem' email, sent "
            "via the Outlook email tool. If unset, the report endpoint returns "
            "503 instead of emailing anyone."
        ),
    )
    account_token_ttl_days: int = Field(
        default=90,
        ge=1,
        description="Lifetime, in days, for newly issued Nymeria account tokens",
    )
    account_max_active_tokens_per_user: int = Field(
        default=10,
        ge=1,
        description="Maximum non-revoked, non-expired account tokens per user",
    )
    account_bootstrap_token_ttl_hours: int = Field(
        default=24,
        ge=1,
        description="Lifetime, in hours, for the first-run bootstrap admin token",
    )
    nymeria_allow_self_edit: bool = Field(
        default=True,
        description="Allow admin-only self_file_write/delete/reload tools to mutate Nymeria source",
    )
    nymeria_allow_unsandboxed_mcp_install: bool = Field(
        default=True,
        description="Allow managed MCP installs that execute downloaded package code without an external sandbox",
    )
    nymeria_enforce_mcp_stdio_allowlist: bool = Field(
        default=False,
        description="Restrict MCP stdio launches to the curated SAFE_STDIO_COMMANDS allowlist",
    )
    nymeria_confine_file_to_workspace: bool = Field(
        default=False,
        description="Restrict file_write and file_edit write targets to NYMERIA_WORKSPACE_DIR",
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
    slack_app_token: Optional[str] = Field(
        default=None,
        description="Slack app-level token for Socket Mode (xapp-...)",
    )
    slack_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Slack channel behavior: mention (default) or all",
    )
    slack_show_tool_events: bool = Field(
        default=False,
        description="Show Slack tool-call/tool-result messages during streaming",
    )

    # Messaging Platform Credentials - Matrix
    matrix_homeserver: Optional[str] = Field(
        default=None,
        description="Matrix homeserver URL, e.g. https://matrix.org",
    )
    matrix_access_token: Optional[str] = Field(
        default=None,
        description="Matrix access token for the bot account",
    )
    matrix_user_id: Optional[str] = Field(
        default=None,
        description="Matrix bot user ID, e.g. @nymeria:example.org",
    )
    matrix_password: Optional[str] = Field(
        default=None,
        description="Matrix bot password fallback when no access token is configured",
    )
    matrix_device_id: Optional[str] = Field(
        default=None,
        description="Optional stable Matrix device ID for password login",
    )
    matrix_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Matrix room behavior: mention (default) or all",
    )
    matrix_free_response_rooms: str = Field(
        default="",
        description="Comma-separated Matrix room IDs that respond without bot mention",
    )
    matrix_auto_join: bool = Field(
        default=False,
        description="Whether the Matrix bot should auto-join invited rooms",
    )
    mattermost_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Mattermost channel behavior: mention (default) or all",
    )
    mattermost_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Mattermost streaming",
    )
    zulip_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Zulip channel behavior: mention (default) or all",
    )
    zulip_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Zulip streaming",
    )
    rocketchat_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Rocket.Chat room behavior: mention (default) or all",
    )
    rocketchat_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Rocket.Chat streaming",
    )
    teams_bot_app_id: Optional[str] = Field(
        default=None,
        description="Microsoft Teams bot App ID for Bot Framework replies",
    )
    teams_bot_app_password: Optional[str] = Field(
        default=None,
        description="Microsoft Teams bot client secret for Bot Framework replies",
    )
    teams_bot_tenant_id: Optional[str] = Field(
        default=None,
        description="Microsoft Teams bot tenant ID used during Azure app setup",
    )
    teams_bot_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Microsoft Teams group/channel behavior: mention (default) or all",
    )
    teams_bot_validate_auth: bool = Field(
        default=True,
        description=(
            "Deprecated compatibility setting; Bot Framework JWT validation is always "
            "enforced on incoming Microsoft Teams webhooks"
        ),
    )
    teams_bot_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Microsoft Teams streaming",
    )
    teams_bot_token_url: str = Field(
        default="https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
        description="Bot Framework OAuth token URL for Microsoft Teams replies",
    )
    teams_bot_openid_config_url: str = Field(
        default="https://login.botframework.com/v1/.well-known/openidconfiguration",
        description="Bot Framework OpenID metadata URL for incoming Teams webhook JWT validation",
    )
    google_chat_service_account_json: Optional[str] = Field(
        default=None,
        description="Google Chat service account JSON content or path for app-auth replies",
    )
    google_chat_service_account_file: Optional[str] = Field(
        default=None,
        description="Google Chat service account JSON file path for app-auth replies",
    )
    google_chat_project_number: Optional[str] = Field(
        default=None,
        description="Google Cloud project number for Google Chat project-number auth",
    )
    google_chat_auth_audience: Optional[str] = Field(
        default=None,
        description="Google Chat request auth audience override; defaults to webhook URL",
    )
    google_chat_auth_audience_type: Literal["app-url", "project-number"] = Field(
        default="app-url",
        description="Google Chat request auth audience type",
    )
    google_chat_bot_name: str = Field(
        default="Nymeria",
        description="Google Chat bot display name used for mention stripping",
    )
    google_chat_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Google Chat space behavior: mention (default) or all",
    )
    google_chat_validate_auth: bool = Field(
        default=True,
        description=(
            "Deprecated compatibility setting; Google Chat bearer-token validation is "
            "always enforced on incoming webhooks"
        ),
    )
    google_chat_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Google Chat streaming",
    )
    google_chat_api_base_url: str = Field(
        default="https://chat.googleapis.com/v1",
        description="Google Chat REST API base URL",
    )
    google_chat_use_adc: bool = Field(
        default=False,
        description="Allow Application Default Credentials for Google Chat replies",
    )
    line_channel_access_token: Optional[str] = Field(
        default=None,
        description="LINE Messaging API channel access token for webhook replies",
    )
    line_channel_secret: Optional[str] = Field(
        default=None,
        description="LINE Messaging API channel secret for webhook signature validation",
    )
    line_bot_user_id: Optional[str] = Field(
        default=None,
        description="LINE bot user ID used for mention detection",
    )
    line_bot_name: str = Field(
        default="Nymeria",
        description="LINE bot display name used for mention stripping",
    )
    line_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="LINE group/room behavior: mention (default) or all",
    )
    line_validate_signature: bool = Field(
        default=True,
        description=(
            "Deprecated compatibility setting; LINE x-line-signature validation is "
            "always enforced on incoming webhooks"
        ),
    )
    line_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during LINE streaming",
    )
    line_api_base_url: str = Field(
        default="https://api.line.me/v2/bot",
        description="LINE Messaging API base URL",
    )
    signal_http_url: Optional[str] = Field(
        default=None,
        description="signal-cli-rest-api base URL for the Signal bot",
    )
    signal_account: Optional[str] = Field(
        default=None,
        description="Signal bot account number in international format",
    )
    signal_account_uuid: Optional[str] = Field(
        default=None,
        description="Optional Signal bot service UUID for self-message filtering",
    )
    signal_respond_mode: Literal["mention", "all"] = Field(
        default="mention",
        description="Signal group behavior: mention (default) or all",
    )
    signal_allowed_users: str = Field(
        default="",
        description="Comma-separated Signal senders allowed before Nymeria link resolution",
    )
    signal_allowed_groups: str = Field(
        default="",
        description="Comma-separated Signal group IDs allowed before Nymeria routing",
    )
    signal_show_tool_events: bool = Field(
        default=False,
        description="Show compact tool-call/tool-result messages during Signal streaming",
    )
    signal_http_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Signal JSON-RPC request timeout in seconds",
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
    microsoft_graph_access_token: Optional[str] = Field(
        default=None,
        description="Microsoft Graph OAuth access token fallback for native productivity tools",
    )
    microsoft_graph_base_url: str = Field(
        default="https://graph.microsoft.com/v1.0",
        description="Microsoft Graph API base URL",
    )
    outlook_default_account_id: Optional[str] = Field(
        default=None,
        description="Default Outlook account ID for email tools (used when agent doesn't specify one)"
    )

    # LLM Configuration
    llm_provider: str = Field(
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
            "model IDs for the active provider or provider:model for any "
            "known provider."
        ),
    )
    llm_fallback_hold_seconds: int = Field(
        default=7200,
        ge=0,
        le=604800,
        description=(
            "Seconds to keep a successful fallback provider/model active for a "
            "thread after primary retry exhaustion. 0 disables timed thread hold."
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
    dynamic_tool_binding: bool = Field(
        default=True,
        description=(
            "Resolve tools per-step in the model node instead of rebuilding the "
            "graph on enable. Eliminates the Command(goto=END) + tool_reload_resume "
            "round-trip when tools change mid-turn. Default; set False to use the "
            "legacy rebuild path as a fallback. The tool executor resolves "
            "newly-created or newly-installed tools from the live registry before "
            "rejecting post-build tool calls."
        ),
    )
    llm_use_model_defaults: bool = Field(
        default=False,
        description="Use model-specific defaults for temperature/top_p/frequency_penalty instead of global values"
    )
    llm_base_url: Optional[str] = Field(
        default=None,
        description="Override base URL for LLM API (e.g., local proxy at http://localhost:8317/v1)"
    )
    llm_context_length: Optional[int] = Field(
        default=None,
        ge=1_000,
        le=2_000_000,
        description="Manual context window override for the selected LLM model"
    )
    llm_ollama_num_ctx: Optional[int] = Field(
        default=None,
        ge=1_000,
        le=2_000_000,
        description="Ollama num_ctx override passed in extra_body.options.num_ctx"
    )
    llm_provider_route: Optional[Literal["native", "openai_compat"]] = Field(
        default=None,
        description=(
            "Default provider adapter route when the selected provider supports "
            "multiple routes. None uses the provider registry default."
        ),
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
    tavily_api_key: Optional[str] = Field(default=None, description="Tavily API key for web_search_tavily")
    exa_api_key: Optional[str] = Field(default=None, description="Exa API key for web_search_exa")
    wolfram_alpha_app_id: Optional[str] = Field(default=None, description="Wolfram|Alpha AppID for wolfram_alpha_query")
    searxng_base_url: Optional[str] = Field(default=None, description="Base URL for a SearXNG instance")
    nasa_api_key: Optional[str] = Field(default=None, description="NASA API key for nasa_apod")
    openweathermap_api_key: Optional[str] = Field(default=None, description="OpenWeatherMap API key for weather tools")
    google_books_api_key: Optional[str] = Field(default=None, description="Optional Google Books API key fallback")
    google_books_base_url: str = Field(default="https://www.googleapis.com/books/v1", description="Google Books API base URL")
    youtube_api_key: Optional[str] = Field(default=None, description="YouTube Data API key fallback")
    youtube_base_url: str = Field(default="https://www.googleapis.com/youtube/v3", description="YouTube Data API base URL")
    spotify_access_token: Optional[str] = Field(default=None, description="Optional Spotify bearer token fallback")
    spotify_client_id: Optional[str] = Field(default=None, description="Spotify client ID for client credentials fallback")
    spotify_client_secret: Optional[str] = Field(default=None, description="Spotify client secret for client credentials fallback")
    spotify_base_url: str = Field(default="https://api.spotify.com/v1", description="Spotify Web API base URL")
    spotify_accounts_base_url: str = Field(default="https://accounts.spotify.com", description="Spotify Accounts API base URL")
    reddit_access_token: Optional[str] = Field(default=None, description="Optional Reddit OAuth bearer token fallback")
    reddit_refresh_token: Optional[str] = Field(default=None, description="Optional Reddit OAuth refresh token fallback")
    reddit_client_id: Optional[str] = Field(default=None, description="Reddit OAuth client ID fallback")
    reddit_client_secret: Optional[str] = Field(default=None, description="Reddit OAuth client secret fallback")
    reddit_base_url: str = Field(default="https://oauth.reddit.com", description="Reddit OAuth API base URL")
    reddit_public_base_url: str = Field(default="https://www.reddit.com", description="Reddit public JSON API base URL")
    reddit_token_url: str = Field(default="https://www.reddit.com/api/v1/access_token", description="Reddit OAuth token URL")
    discourse_api_key: Optional[str] = Field(default=None, description="Discourse API key fallback")
    discourse_api_username: Optional[str] = Field(default=None, description="Discourse API username fallback")
    discourse_base_url: Optional[str] = Field(default=None, description="Discourse forum base URL")
    medium_access_token: Optional[str] = Field(default=None, description="Medium access token fallback")
    medium_base_url: str = Field(default="https://api.medium.com/v1", description="Medium API base URL")
    bamboohr_api_key: Optional[str] = Field(default=None, description="BambooHR API key fallback")
    bamboohr_subdomain: Optional[str] = Field(default=None, description="BambooHR company subdomain fallback")
    bamboohr_base_url: str = Field(default="https://api.bamboohr.com/api/gateway.php", description="BambooHR API gateway base URL")
    beeminder_access_token: Optional[str] = Field(default=None, description="Beeminder auth token fallback")
    beeminder_base_url: str = Field(default="https://www.beeminder.com/api/v1", description="Beeminder API base URL")
    clockify_api_key: Optional[str] = Field(default=None, description="Clockify API key fallback")
    clockify_base_url: str = Field(default="https://api.clockify.me/api/v1", description="Clockify API base URL")
    harvest_access_token: Optional[str] = Field(default=None, description="Harvest access token fallback")
    harvest_account_id: Optional[str] = Field(default=None, description="Harvest account ID fallback")
    harvest_base_url: str = Field(default="https://api.harvestapp.com/v2", description="Harvest API base URL")
    oura_access_token: Optional[str] = Field(default=None, description="Oura access token fallback")
    oura_base_url: str = Field(default="https://api.ouraring.com/v2", description="Oura API base URL")
    strava_access_token: Optional[str] = Field(default=None, description="Strava access token fallback")
    strava_base_url: str = Field(default="https://www.strava.com/api/v3", description="Strava API base URL")
    homeassistant_access_token: Optional[str] = Field(default=None, description="Home Assistant long-lived access token fallback")
    homeassistant_base_url: Optional[str] = Field(default=None, description="Home Assistant API base URL, usually http://host:8123/api")
    philips_hue_access_token: Optional[str] = Field(default=None, description="Philips Hue access token fallback")
    philips_hue_username: Optional[str] = Field(default=None, description="Philips Hue bridge username fallback")
    philips_hue_base_url: str = Field(default="https://api.meethue.com/route", description="Philips Hue routed API base URL")
    activecampaign_api_key: Optional[str] = Field(default=None, description="ActiveCampaign API key fallback")
    activecampaign_base_url: Optional[str] = Field(default=None, description="ActiveCampaign account API URL, e.g. https://account.api-us1.com")
    convertkit_api_secret: Optional[str] = Field(default=None, description="ConvertKit API secret fallback")
    convertkit_base_url: str = Field(default="https://api.convertkit.com/v3", description="ConvertKit API base URL")
    getresponse_api_key: Optional[str] = Field(default=None, description="GetResponse API key fallback")
    getresponse_base_url: str = Field(default="https://api.getresponse.com/v3", description="GetResponse API base URL")
    mailerlite_api_key: Optional[str] = Field(default=None, description="MailerLite API key fallback")
    mailerlite_base_url: str = Field(default="https://connect.mailerlite.com/api", description="MailerLite API base URL")
    mailerlite_classic_api: bool = Field(default=False, description="Use MailerLite Classic API authentication/header style")
    customerio_tracking_site_id: Optional[str] = Field(default=None, description="Customer.io tracking site ID fallback")
    customerio_tracking_api_key: Optional[str] = Field(default=None, description="Customer.io tracking API key fallback")
    customerio_app_api_key: Optional[str] = Field(default=None, description="Customer.io app API key fallback")
    customerio_region: str = Field(default="track.customer.io", description="Customer.io tracking region host")
    customerio_tracking_base_url: Optional[str] = Field(default=None, description="Customer.io tracking API base URL")
    customerio_app_base_url: Optional[str] = Field(default=None, description="Customer.io app API base URL")
    iterable_api_key: Optional[str] = Field(default=None, description="Iterable API key fallback")
    iterable_base_url: str = Field(default="https://api.iterable.com/api", description="Iterable API base URL")
    posthog_api_key: Optional[str] = Field(default=None, description="PostHog project API key fallback")
    posthog_base_url: str = Field(default="https://app.posthog.com", description="PostHog API base URL")
    segment_write_key: Optional[str] = Field(default=None, description="Segment write key fallback")
    segment_base_url: str = Field(default="https://api.segment.io/v1", description="Segment tracking API base URL")
    actionnetwork_api_key: Optional[str] = Field(default=None, description="Action Network API key fallback")
    actionnetwork_base_url: str = Field(default="https://actionnetwork.org/api/v2", description="Action Network API base URL")
    autopilot_api_key: Optional[str] = Field(default=None, description="Autopilot API key fallback")
    autopilot_base_url: str = Field(default="https://api2.autopilothq.com/v1", description="Autopilot API base URL")
    egoi_api_key: Optional[str] = Field(default=None, description="E-goi API key fallback")
    egoi_base_url: str = Field(default="https://api.egoiapp.com", description="E-goi API base URL")
    vero_auth_token: Optional[str] = Field(default=None, description="Vero auth token fallback")
    vero_base_url: str = Field(default="https://api.getvero.com/api/v2", description="Vero API base URL")
    lemlist_api_key: Optional[str] = Field(default=None, description="Lemlist API key fallback")
    lemlist_base_url: str = Field(default="https://api.lemlist.com/api", description="Lemlist API base URL")
    sendy_url: Optional[str] = Field(default=None, description="Sendy site URL fallback")
    sendy_base_url: Optional[str] = Field(default=None, description="Sendy base URL fallback")
    sendy_api_key: Optional[str] = Field(default=None, description="Sendy API key fallback")
    emelia_api_key: Optional[str] = Field(default=None, description="Emelia API key fallback")
    emelia_graphql_url: str = Field(default="https://graphql.emelia.io/graphql", description="Emelia GraphQL URL")
    npm_registry_url: str = Field(default="https://registry.npmjs.org", description="npm registry base URL for npm tools")
    graphql_endpoint: Optional[str] = Field(default=None, description="Generic GraphQL endpoint fallback")
    graphql_bearer_token: Optional[str] = Field(default=None, description="Generic GraphQL bearer token fallback")
    graphql_api_key: Optional[str] = Field(default=None, description="Generic GraphQL API key fallback")
    graphql_api_key_header: str = Field(default="x-api-key", description="Generic GraphQL API key header name")
    graphql_headers_json: str = Field(default="{}", description="Generic GraphQL default headers as a JSON object")
    totp_secret: Optional[str] = Field(default=None, description="Base32 TOTP secret fallback")
    crypto_hmac_secret: Optional[str] = Field(default=None, description="Crypto HMAC secret fallback")
    crypto_sign_private_key: Optional[str] = Field(default=None, description="Crypto signing private key PEM fallback")
    crypto_sign_private_key_passphrase: Optional[str] = Field(default=None, description="Crypto signing private key passphrase fallback")
    jwt_secret: Optional[str] = Field(default=None, description="JWT HMAC secret fallback")
    jwt_private_key: Optional[str] = Field(default=None, description="JWT signing private key PEM fallback")
    jwt_public_key: Optional[str] = Field(default=None, description="JWT verification public key PEM fallback")
    jwt_algorithm: str = Field(default="HS256", description="Default JWT signing/verification algorithm")
    github_token: Optional[str] = Field(default=None, description="GitHub API token fallback for developer platform tools")
    github_api_base_url: str = Field(default="https://api.github.com", description="GitHub API base URL")
    gitlab_token: Optional[str] = Field(default=None, description="GitLab API token fallback for developer platform tools")
    gitlab_base_url: str = Field(default="https://gitlab.com/api/v4", description="GitLab API base URL or instance URL")
    circleci_api_token: Optional[str] = Field(default=None, description="CircleCI API token fallback")
    circleci_base_url: str = Field(default="https://circleci.com/api/v2", description="CircleCI API base URL")
    travisci_api_token: Optional[str] = Field(default=None, description="Travis CI API token fallback")
    travisci_base_url: str = Field(default="https://api.travis-ci.com", description="Travis CI API base URL")
    jenkins_base_url: Optional[str] = Field(default=None, description="Jenkins instance base URL fallback")
    jenkins_username: Optional[str] = Field(default=None, description="Jenkins username fallback")
    jenkins_api_token: Optional[str] = Field(default=None, description="Jenkins API token fallback")
    dropbox_access_token: Optional[str] = Field(default=None, description="Dropbox access token fallback")
    dropbox_api_base_url: str = Field(default="https://api.dropboxapi.com/2", description="Dropbox API base URL")
    dropbox_content_base_url: str = Field(default="https://content.dropboxapi.com/2", description="Dropbox content API base URL")
    nextcloud_webdav_url: Optional[str] = Field(default=None, description="Nextcloud WebDAV URL fallback")
    nextcloud_username: Optional[str] = Field(default=None, description="Nextcloud username fallback")
    nextcloud_password: Optional[str] = Field(default=None, description="Nextcloud password or app password fallback")
    nextcloud_access_token: Optional[str] = Field(default=None, description="Nextcloud OAuth access token fallback")
    s3_access_key_id: Optional[str] = Field(default=None, description="AWS access key ID fallback")
    s3_secret_access_key: Optional[str] = Field(default=None, description="AWS secret access key fallback")
    s3_session_token: Optional[str] = Field(default=None, description="AWS session token fallback")
    s3_region: str = Field(default="us-east-1", description="AWS region fallback")
    s3_endpoint_url: Optional[str] = Field(default=None, description="S3-compatible endpoint URL fallback")
    s3_force_path_style: bool = Field(default=False, description="Use path-style S3 addressing")
    clearbit_api_key: Optional[str] = Field(default=None, description="Clearbit API key fallback")
    clearbit_company_base_url: str = Field(default="https://company-stream.clearbit.com", description="Clearbit company API base URL")
    clearbit_person_base_url: str = Field(default="https://person-stream.clearbit.com", description="Clearbit person API base URL")
    clearbit_autocomplete_base_url: str = Field(default="https://autocomplete.clearbit.com", description="Clearbit autocomplete API base URL")
    uplead_api_key: Optional[str] = Field(default=None, description="Uplead API key fallback")
    uplead_base_url: str = Field(default="https://api.uplead.com/v2", description="Uplead API base URL")
    dropcontact_api_key: Optional[str] = Field(default=None, description="Dropcontact API key fallback")
    dropcontact_base_url: str = Field(default="https://api.dropcontact.io", description="Dropcontact API base URL")
    humantic_api_key: Optional[str] = Field(default=None, description="Humantic AI API key fallback")
    humantic_base_url: str = Field(default="https://api.humantic.ai/v1", description="Humantic AI API base URL")
    lonescale_api_key: Optional[str] = Field(default=None, description="LoneScale API key fallback")
    lonescale_base_url: str = Field(default="https://public-api.lonescale.com", description="LoneScale API base URL")
    uproc_email: Optional[str] = Field(default=None, description="uProc account email fallback")
    uproc_api_key: Optional[str] = Field(default=None, description="uProc API key fallback")
    uproc_base_url: str = Field(default="https://api.uproc.io/api/v2", description="uProc API base URL")
    bitly_token: Optional[str] = Field(default=None, description="Bitly API token fallback")
    bitly_base_url: str = Field(default="https://api-ssl.bitly.com/v4", description="Bitly API base URL")
    brandfetch_api_key: Optional[str] = Field(default=None, description="Brandfetch API key fallback")
    brandfetch_base_url: str = Field(default="https://api.brandfetch.io/v2", description="Brandfetch API base URL")
    marketstack_api_key: Optional[str] = Field(default=None, description="Marketstack API key fallback")
    marketstack_base_url: str = Field(default="https://api.marketstack.com/v1", description="Marketstack API base URL")
    deepl_api_key: Optional[str] = Field(default=None, description="DeepL API key fallback")
    deepl_api_plan: str = Field(default="pro", description="DeepL API plan: pro or free")
    deepl_base_url: Optional[str] = Field(default=None, description="DeepL API base URL override")
    lingvanex_api_key: Optional[str] = Field(default=None, description="LingvaNex API key fallback")
    lingvanex_base_url: str = Field(default="https://api-b2b.backenster.com/b1/api/v3", description="LingvaNex API base URL")
    apitemplate_api_key: Optional[str] = Field(default=None, description="APITemplate API key fallback")
    apitemplate_base_url: str = Field(default="https://api.apitemplate.io/v1", description="APITemplate API base URL")
    onesimple_api_token: Optional[str] = Field(default=None, description="One Simple API token fallback")
    onesimple_base_url: str = Field(default="https://onesimpleapi.com/api", description="One Simple API base URL")
    dhl_api_key: Optional[str] = Field(default=None, description="DHL API key fallback")
    dhl_base_url: str = Field(default="https://api-eu.dhl.com", description="DHL API base URL")
    onfleet_api_key: Optional[str] = Field(default=None, description="Onfleet API key fallback")
    onfleet_base_url: str = Field(default="https://onfleet.com/api/v2", description="Onfleet API base URL")
    phantombuster_api_key: Optional[str] = Field(default=None, description="Phantombuster API key fallback")
    phantombuster_base_url: str = Field(default="https://api.phantombuster.com/api/v2", description="Phantombuster API base URL")
    erpnext_api_key: Optional[str] = Field(default=None, description="ERPNext API key fallback")
    erpnext_api_secret: Optional[str] = Field(default=None, description="ERPNext API secret fallback")
    erpnext_base_url: Optional[str] = Field(default=None, description="ERPNext site base URL")
    erpnext_subdomain: Optional[str] = Field(default=None, description="ERPNext cloud site subdomain fallback")
    erpnext_cloud_domain: str = Field(default="erpnext.com", description="ERPNext cloud domain")
    odoo_url: Optional[str] = Field(default=None, description="Odoo site URL fallback")
    odoo_username: Optional[str] = Field(default=None, description="Odoo username fallback")
    odoo_password: Optional[str] = Field(default=None, description="Odoo password or API key fallback")
    odoo_database: Optional[str] = Field(default=None, description="Odoo database name fallback")
    invoiceninja_api_token: Optional[str] = Field(default=None, description="Invoice Ninja API token fallback")
    invoiceninja_secret: Optional[str] = Field(default=None, description="Invoice Ninja v5 API secret fallback")
    invoiceninja_base_url: str = Field(default="https://invoicing.co", description="Invoice Ninja API base URL")
    invoiceninja_api_version: str = Field(default="v5", description="Invoice Ninja API version: v4 or v5")
    demio_api_key: Optional[str] = Field(default=None, description="Demio API key fallback")
    demio_api_secret: Optional[str] = Field(default=None, description="Demio API secret fallback")
    demio_base_url: str = Field(default="https://my.demio.com/api/v1", description="Demio API base URL")
    zoom_access_token: Optional[str] = Field(default=None, description="Zoom OAuth or bearer access token fallback")
    zoom_base_url: str = Field(default="https://api.zoom.us/v2", description="Zoom API base URL")
    gotowebinar_access_token: Optional[str] = Field(default=None, description="GoToWebinar OAuth access token fallback")
    gotowebinar_account_key: Optional[str] = Field(default=None, description="GoToWebinar account key fallback")
    gotowebinar_organizer_key: Optional[str] = Field(default=None, description="GoToWebinar organizer key fallback")
    gotowebinar_base_url: str = Field(default="https://api.getgo.com/G2W/rest/v2", description="GoToWebinar API base URL")
    todoist_api_key: Optional[str] = Field(default=None, description="Todoist API key fallback")
    todoist_base_url: str = Field(default="https://api.todoist.com/api/v1", description="Todoist API base URL")
    trello_api_key: Optional[str] = Field(default=None, description="Trello API key fallback")
    trello_api_token: Optional[str] = Field(default=None, description="Trello API token fallback")
    trello_base_url: str = Field(default="https://api.trello.com/1", description="Trello API base URL")
    raindrop_access_token: Optional[str] = Field(default=None, description="Raindrop access token fallback")
    raindrop_base_url: str = Field(default="https://api.raindrop.io/rest/v1", description="Raindrop API base URL")
    yourls_url: Optional[str] = Field(default=None, description="YOURLS site or API URL fallback")
    yourls_signature: Optional[str] = Field(default=None, description="YOURLS signature token fallback")
    yourls_username: Optional[str] = Field(default=None, description="YOURLS username fallback")
    yourls_password: Optional[str] = Field(default=None, description="YOURLS password fallback")
    asana_access_token: Optional[str] = Field(default=None, description="Asana personal access token fallback")
    asana_base_url: str = Field(default="https://app.asana.com/api/1.0", description="Asana API base URL")
    linear_api_key: Optional[str] = Field(default=None, description="Linear API key fallback")
    linear_api_url: str = Field(default="https://api.linear.app/graphql", description="Linear GraphQL API URL")
    jira_email: Optional[str] = Field(default=None, description="Jira Cloud account email fallback")
    jira_api_token: Optional[str] = Field(default=None, description="Jira Cloud API token fallback")
    jira_access_token: Optional[str] = Field(default=None, description="Jira OAuth/bearer token fallback")
    jira_base_url: Optional[str] = Field(default=None, description="Jira site base URL, e.g. https://example.atlassian.net")
    clickup_access_token: Optional[str] = Field(default=None, description="ClickUp access token fallback")
    clickup_base_url: str = Field(default="https://api.clickup.com/api/v2", description="ClickUp API base URL")
    monday_api_token: Optional[str] = Field(default=None, description="Monday API token fallback")
    monday_api_url: str = Field(default="https://api.monday.com/v2", description="Monday GraphQL API URL")
    taiga_auth_token: Optional[str] = Field(default=None, description="Taiga auth token fallback")
    taiga_username: Optional[str] = Field(default=None, description="Taiga username fallback")
    taiga_password: Optional[str] = Field(default=None, description="Taiga password fallback")
    taiga_base_url: str = Field(default="https://api.taiga.io/api/v1", description="Taiga API base URL")
    wekan_base_url: Optional[str] = Field(default=None, description="Wekan instance base URL")
    wekan_token: Optional[str] = Field(default=None, description="Wekan session token fallback")
    wekan_username: Optional[str] = Field(default=None, description="Wekan username fallback")
    wekan_password: Optional[str] = Field(default=None, description="Wekan password fallback")
    slack_bot_token: Optional[str] = Field(default=None, description="Slack bot token fallback")
    slack_access_token: Optional[str] = Field(default=None, description="Slack access token fallback")
    slack_base_url: str = Field(default="https://slack.com/api", description="Slack Web API base URL")
    notion_api_key: Optional[str] = Field(default=None, description="Notion API key fallback")
    notion_version: str = Field(default="2026-03-11", description="Notion API version")
    notion_base_url: str = Field(default="https://api.notion.com/v1", description="Notion API base URL")
    airtable_access_token: Optional[str] = Field(default=None, description="Airtable personal access token fallback")
    airtable_api_key: Optional[str] = Field(default=None, description="Airtable legacy API key fallback")
    airtable_base_url: str = Field(default="https://api.airtable.com/v0", description="Airtable API base URL")
    hubspot_access_token: Optional[str] = Field(default=None, description="HubSpot private app/OAuth token fallback")
    hubspot_base_url: str = Field(default="https://api.hubapi.com", description="HubSpot API base URL")
    zendesk_email: Optional[str] = Field(default=None, description="Zendesk email fallback for API token auth")
    zendesk_api_token: Optional[str] = Field(default=None, description="Zendesk API token fallback")
    zendesk_access_token: Optional[str] = Field(default=None, description="Zendesk OAuth access token fallback")
    zendesk_subdomain: Optional[str] = Field(default=None, description="Zendesk subdomain fallback")
    zendesk_base_url: Optional[str] = Field(default=None, description="Zendesk API base URL override")
    mailchimp_api_key: Optional[str] = Field(default=None, description="Mailchimp API key fallback")
    mailchimp_access_token: Optional[str] = Field(default=None, description="Mailchimp OAuth access token fallback")
    mailchimp_server_prefix: Optional[str] = Field(default=None, description="Mailchimp server prefix, e.g. us21")
    mailchimp_base_url: Optional[str] = Field(default=None, description="Mailchimp Marketing API base URL override")
    mautic_base_url: Optional[str] = Field(default=None, description="Mautic instance base URL fallback")
    mautic_access_token: Optional[str] = Field(default=None, description="Mautic OAuth/bearer access token fallback")
    mautic_username: Optional[str] = Field(default=None, description="Mautic basic-auth username fallback")
    mautic_password: Optional[str] = Field(default=None, description="Mautic basic-auth password fallback")
    freshdesk_api_key: Optional[str] = Field(default=None, description="Freshdesk API key fallback")
    freshdesk_domain: Optional[str] = Field(default=None, description="Freshdesk account subdomain fallback")
    freshdesk_base_url: Optional[str] = Field(default=None, description="Freshdesk API base URL override")
    freshservice_api_key: Optional[str] = Field(default=None, description="Freshservice API key fallback")
    freshservice_domain: Optional[str] = Field(default=None, description="Freshservice account subdomain fallback")
    freshservice_base_url: Optional[str] = Field(default=None, description="Freshservice API base URL override")
    servicenow_base_url: Optional[str] = Field(default=None, description="ServiceNow API base URL override")
    servicenow_instance: Optional[str] = Field(default=None, description="ServiceNow instance subdomain fallback")
    servicenow_access_token: Optional[str] = Field(default=None, description="ServiceNow OAuth access token fallback")
    servicenow_username: Optional[str] = Field(default=None, description="ServiceNow basic-auth username fallback")
    servicenow_password: Optional[str] = Field(default=None, description="ServiceNow basic-auth password fallback")
    zammad_base_url: Optional[str] = Field(default=None, description="Zammad API base URL fallback")
    zammad_token: Optional[str] = Field(default=None, description="Zammad token fallback")
    zammad_username: Optional[str] = Field(default=None, description="Zammad basic-auth username fallback")
    zammad_password: Optional[str] = Field(default=None, description="Zammad basic-auth password fallback")
    helpscout_access_token: Optional[str] = Field(default=None, description="Help Scout OAuth access token fallback")
    helpscout_base_url: str = Field(default="https://api.helpscout.net/v2", description="Help Scout API base URL")
    intercom_access_token: Optional[str] = Field(default=None, description="Intercom access token fallback")
    intercom_base_url: str = Field(default="https://api.intercom.io", description="Intercom API base URL")
    intercom_version: str = Field(default="2.11", description="Intercom API version")
    drift_access_token: Optional[str] = Field(default=None, description="Drift access token fallback")
    drift_base_url: str = Field(default="https://driftapi.com", description="Drift API base URL")
    salesforce_instance_url: Optional[str] = Field(default=None, description="Salesforce instance URL fallback")
    salesforce_access_token: Optional[str] = Field(default=None, description="Salesforce OAuth access token fallback")
    salesforce_base_url: Optional[str] = Field(default=None, description="Salesforce API base URL override")
    salesforce_api_version: str = Field(default="v59.0", description="Salesforce REST API version")
    zoho_crm_access_token: Optional[str] = Field(default=None, description="Zoho CRM OAuth access token fallback")
    zoho_crm_api_domain: Optional[str] = Field(default=None, description="Zoho CRM API domain fallback")
    zoho_crm_base_url: str = Field(default="https://www.zohoapis.com/crm/v2", description="Zoho CRM API base URL")
    freshworks_crm_api_key: Optional[str] = Field(default=None, description="Freshworks CRM API key fallback")
    freshworks_crm_domain: Optional[str] = Field(default=None, description="Freshworks CRM account domain fallback")
    freshworks_crm_base_url: Optional[str] = Field(default=None, description="Freshworks CRM API base URL override")
    salesmate_session_token: Optional[str] = Field(default=None, description="Salesmate session token fallback")
    salesmate_link_name: Optional[str] = Field(default=None, description="Salesmate link name fallback")
    salesmate_base_url: str = Field(default="https://apis.salesmate.io", description="Salesmate API base URL")
    pipedrive_api_token: Optional[str] = Field(default=None, description="Pipedrive API token fallback")
    pipedrive_access_token: Optional[str] = Field(default=None, description="Pipedrive OAuth access token fallback")
    pipedrive_base_url: str = Field(default="https://api.pipedrive.com/api/v2", description="Pipedrive API base URL")
    copper_api_key: Optional[str] = Field(default=None, description="Copper API key fallback")
    copper_email: Optional[str] = Field(default=None, description="Copper user email fallback")
    copper_base_url: str = Field(default="https://api.copper.com/developer_api/v1", description="Copper API base URL")
    agilecrm_email: Optional[str] = Field(default=None, description="Agile CRM account email fallback")
    agilecrm_api_key: Optional[str] = Field(default=None, description="Agile CRM API key fallback")
    agilecrm_subdomain: Optional[str] = Field(default=None, description="Agile CRM account subdomain fallback")
    agilecrm_base_url: Optional[str] = Field(default=None, description="Agile CRM API base URL override")
    monica_access_token: Optional[str] = Field(default=None, description="Monica CRM API token fallback")
    monica_base_url: str = Field(default="https://app.monicahq.com/api", description="Monica CRM API base URL")
    affinity_api_key: Optional[str] = Field(default=None, description="Affinity API key fallback")
    affinity_base_url: str = Field(default="https://api.affinity.co", description="Affinity API base URL")
    keap_access_token: Optional[str] = Field(default=None, description="Keap OAuth access token fallback")
    keap_base_url: str = Field(default="https://api.infusionsoft.com/crm/rest/v1", description="Keap REST API base URL")
    twilio_account_sid: Optional[str] = Field(default=None, description="Twilio account SID fallback")
    twilio_auth_token: Optional[str] = Field(default=None, description="Twilio auth token or API key secret fallback")
    twilio_api_key_sid: Optional[str] = Field(default=None, description="Optional Twilio API key SID fallback")
    twilio_base_url: str = Field(default="https://api.twilio.com/2010-04-01", description="Twilio API base URL")
    sendgrid_api_key: Optional[str] = Field(default=None, description="SendGrid API key fallback")
    sendgrid_base_url: str = Field(default="https://api.sendgrid.com/v3", description="SendGrid API base URL")
    mailgun_api_key: Optional[str] = Field(default=None, description="Mailgun API key fallback")
    mailgun_domain: Optional[str] = Field(default=None, description="Mailgun sending domain fallback")
    mailgun_base_url: str = Field(default="https://api.mailgun.net/v3", description="Mailgun API base URL")
    brevo_api_key: Optional[str] = Field(default=None, description="Brevo API key fallback")
    brevo_base_url: str = Field(default="https://api.brevo.com/v3", description="Brevo API base URL")
    mailjet_api_key: Optional[str] = Field(default=None, description="Mailjet email API key fallback")
    mailjet_secret_key: Optional[str] = Field(default=None, description="Mailjet email secret key fallback")
    mailjet_sms_token: Optional[str] = Field(default=None, description="Mailjet SMS token fallback")
    mailjet_base_url: str = Field(default="https://api.mailjet.com", description="Mailjet API base URL")
    mandrill_api_key: Optional[str] = Field(default=None, description="Mandrill / Mailchimp Transactional API key fallback")
    mandrill_base_url: str = Field(default="https://mandrillapp.com/api/1.0", description="Mandrill API base URL")
    messagebird_access_key: Optional[str] = Field(default=None, description="MessageBird access key fallback")
    messagebird_base_url: str = Field(default="https://rest.messagebird.com", description="MessageBird API base URL")
    mocean_api_key: Optional[str] = Field(default=None, description="Mocean API key fallback")
    mocean_api_secret: Optional[str] = Field(default=None, description="Mocean API secret fallback")
    mocean_base_url: str = Field(default="https://rest.moceanapi.com", description="Mocean API base URL")
    msg91_auth_key: Optional[str] = Field(default=None, description="MSG91 authentication key fallback")
    msg91_base_url: str = Field(default="https://api.msg91.com/api", description="MSG91 API base URL")
    plivo_auth_id: Optional[str] = Field(default=None, description="Plivo auth ID fallback")
    plivo_auth_token: Optional[str] = Field(default=None, description="Plivo auth token fallback")
    plivo_base_url: str = Field(default="https://api.plivo.com/v1", description="Plivo API base URL")
    vonage_api_key: Optional[str] = Field(default=None, description="Vonage API key fallback")
    vonage_api_secret: Optional[str] = Field(default=None, description="Vonage API secret fallback")
    vonage_base_url: str = Field(default="https://rest.nexmo.com", description="Vonage REST API base URL")
    seven_api_key: Optional[str] = Field(default=None, description="seven.io API key fallback")
    seven_base_url: str = Field(default="https://gateway.seven.io/api", description="seven.io API base URL")
    stripe_secret_key: Optional[str] = Field(default=None, description="Stripe secret key fallback")
    stripe_base_url: str = Field(default="https://api.stripe.com/v1", description="Stripe API base URL")
    shopify_shop: Optional[str] = Field(default=None, description="Shopify shop subdomain or myshopify.com host fallback")
    shopify_access_token: Optional[str] = Field(default=None, description="Shopify Admin API access token fallback")
    shopify_api_key: Optional[str] = Field(default=None, description="Legacy Shopify API key fallback")
    shopify_password: Optional[str] = Field(default=None, description="Legacy Shopify Admin API password fallback")
    shopify_api_version: str = Field(default="2026-01", description="Shopify Admin REST API version")
    shopify_base_url: Optional[str] = Field(default=None, description="Shopify Admin REST API base URL override")
    woocommerce_url: Optional[str] = Field(default=None, description="WooCommerce site URL fallback")
    woocommerce_base_url: Optional[str] = Field(default=None, description="WooCommerce REST API base URL override")
    woocommerce_consumer_key: Optional[str] = Field(default=None, description="WooCommerce consumer key fallback")
    woocommerce_consumer_secret: Optional[str] = Field(default=None, description="WooCommerce consumer secret fallback")
    chargebee_api_key: Optional[str] = Field(default=None, description="Chargebee API key fallback")
    chargebee_site: Optional[str] = Field(default=None, description="Chargebee site subdomain fallback")
    chargebee_base_url: Optional[str] = Field(default=None, description="Chargebee API base URL override")
    paddle_vendor_id: Optional[str] = Field(default=None, description="Paddle vendor ID fallback")
    paddle_vendor_auth_code: Optional[str] = Field(default=None, description="Paddle vendor auth code fallback")
    paddle_sandbox: bool = Field(default=False, description="Use Paddle sandbox vendor API")
    paddle_base_url: Optional[str] = Field(default=None, description="Paddle vendor API base URL override")
    profitwell_api_token: Optional[str] = Field(default=None, description="ProfitWell API token fallback")
    profitwell_base_url: str = Field(default="https://api.profitwell.com/v2", description="ProfitWell API base URL")
    tapfiliate_api_key: Optional[str] = Field(default=None, description="Tapfiliate API key fallback")
    tapfiliate_base_url: str = Field(default="https://api.tapfiliate.com/1.6", description="Tapfiliate API base URL")
    magento_host: Optional[str] = Field(default=None, description="Magento site host fallback")
    magento_base_url: Optional[str] = Field(default=None, description="Magento REST API base URL override")
    magento_access_token: Optional[str] = Field(default=None, description="Magento access token fallback")
    unleashed_api_id: Optional[str] = Field(default=None, description="Unleashed API ID fallback")
    unleashed_api_key: Optional[str] = Field(default=None, description="Unleashed API key fallback")
    unleashed_base_url: str = Field(default="https://api.unleashedsoftware.com", description="Unleashed API base URL")
    quickbooks_access_token: Optional[str] = Field(default=None, description="QuickBooks Online OAuth access token fallback")
    quickbooks_realm_id: Optional[str] = Field(default=None, description="QuickBooks Online company/realm ID fallback")
    quickbooks_environment: str = Field(default="production", description="QuickBooks environment: production or sandbox")
    quickbooks_base_url: Optional[str] = Field(default=None, description="QuickBooks Online API base URL override")
    xero_access_token: Optional[str] = Field(default=None, description="Xero OAuth access token fallback")
    xero_tenant_id: Optional[str] = Field(default=None, description="Xero tenant/organization ID fallback")
    xero_base_url: str = Field(default="https://api.xero.com/api.xro/2.0", description="Xero Accounting API base URL")
    xero_connections_url: str = Field(default="https://api.xero.com/connections", description="Xero connections API URL")
    pushbullet_access_token: Optional[str] = Field(default=None, description="Pushbullet access token fallback")
    pushbullet_base_url: str = Field(default="https://api.pushbullet.com/v2", description="Pushbullet API base URL")
    pushcut_api_key: Optional[str] = Field(default=None, description="Pushcut API key fallback")
    pushcut_base_url: str = Field(default="https://api.pushcut.io/v1", description="Pushcut API base URL")
    gotify_base_url: Optional[str] = Field(default=None, description="Gotify server base URL fallback")
    gotify_app_token: Optional[str] = Field(default=None, description="Gotify application token fallback")
    gotify_client_token: Optional[str] = Field(default=None, description="Gotify client token fallback")
    pushover_api_token: Optional[str] = Field(default=None, description="Pushover application API token fallback")
    pushover_user_key: Optional[str] = Field(default=None, description="Pushover user or group key fallback")
    pushover_base_url: str = Field(default="https://api.pushover.net/1", description="Pushover API base URL")
    signl4_team_secret: Optional[str] = Field(default=None, description="SIGNL4 team secret fallback")
    signl4_webhook_url: Optional[str] = Field(default=None, description="SIGNL4 full webhook URL fallback")
    signl4_base_url: str = Field(default="https://connect.signl4.com/webhook", description="SIGNL4 webhook base URL")
    wordpress_url: Optional[str] = Field(default=None, description="WordPress site URL fallback")
    wordpress_username: Optional[str] = Field(default=None, description="WordPress username fallback")
    wordpress_password: Optional[str] = Field(default=None, description="WordPress application password fallback")
    strapi_url: Optional[str] = Field(default=None, description="Strapi API/site URL fallback")
    strapi_api_token: Optional[str] = Field(default=None, description="Strapi API token fallback")
    strapi_email: Optional[str] = Field(default=None, description="Strapi local auth email fallback")
    strapi_password: Optional[str] = Field(default=None, description="Strapi local auth password fallback")
    strapi_api_version: str = Field(default="v4", description="Strapi REST API version, v4 or v3")
    contentful_space_id: Optional[str] = Field(default=None, description="Contentful space ID fallback")
    contentful_delivery_token: Optional[str] = Field(default=None, description="Contentful delivery API token fallback")
    contentful_preview_token: Optional[str] = Field(default=None, description="Contentful preview API token fallback")
    contentful_base_url: str = Field(default="https://cdn.contentful.com", description="Contentful delivery API base URL")
    contentful_preview_base_url: str = Field(
        default="https://preview.contentful.com",
        description="Contentful preview API base URL",
    )
    ghost_url: Optional[str] = Field(default=None, description="Ghost site URL fallback")
    ghost_content_api_key: Optional[str] = Field(default=None, description="Ghost Content API key fallback")
    ghost_admin_api_key: Optional[str] = Field(default=None, description="Ghost Admin API key fallback")
    ghost_api_version: str = Field(default="v5.0", description="Ghost Admin API Accept-Version header")
    storyblok_content_token: Optional[str] = Field(default=None, description="Storyblok Content API token fallback")
    storyblok_management_token: Optional[str] = Field(default=None, description="Storyblok Management API token fallback")
    storyblok_space_id: Optional[str] = Field(default=None, description="Storyblok space ID fallback")
    storyblok_content_base_url: str = Field(default="https://api.storyblok.com/v2/cdn", description="Storyblok Content API base URL")
    storyblok_management_base_url: str = Field(default="https://mapi.storyblok.com/v1", description="Storyblok Management API base URL")
    webflow_access_token: Optional[str] = Field(default=None, description="Webflow access token fallback")
    webflow_base_url: str = Field(default="https://api.webflow.com/v2", description="Webflow API base URL")
    netlify_access_token: Optional[str] = Field(default=None, description="Netlify access token fallback")
    netlify_base_url: str = Field(default="https://api.netlify.com/api/v1", description="Netlify API base URL")
    rundeck_base_url: Optional[str] = Field(default=None, description="Rundeck instance base URL fallback")
    rundeck_token: Optional[str] = Field(default=None, description="Rundeck API token fallback")
    uptimerobot_api_key: Optional[str] = Field(default=None, description="UptimeRobot API key fallback")
    uptimerobot_base_url: str = Field(default="https://api.uptimerobot.com/v2", description="UptimeRobot API base URL")
    pagerduty_api_token: Optional[str] = Field(default=None, description="PagerDuty API token fallback")
    pagerduty_from_email: Optional[str] = Field(default=None, description="PagerDuty From email fallback")
    pagerduty_base_url: str = Field(default="https://api.pagerduty.com", description="PagerDuty API base URL")
    sentry_auth_token: Optional[str] = Field(default=None, description="Sentry auth token fallback")
    sentry_base_url: str = Field(default="https://sentry.io", description="Sentry API base URL")
    cloudflare_api_token: Optional[str] = Field(default=None, description="Cloudflare API token fallback")
    cloudflare_base_url: str = Field(default="https://api.cloudflare.com/client/v4", description="Cloudflare API base URL")
    grafana_api_token: Optional[str] = Field(default=None, description="Grafana API token fallback")
    grafana_base_url: Optional[str] = Field(default=None, description="Grafana base URL fallback")
    metabase_base_url: Optional[str] = Field(default=None, description="Metabase base URL fallback")
    metabase_session_token: Optional[str] = Field(default=None, description="Metabase session token fallback")
    metabase_api_key: Optional[str] = Field(default=None, description="Metabase API key fallback")
    metabase_username: Optional[str] = Field(default=None, description="Metabase username fallback")
    metabase_password: Optional[str] = Field(default=None, description="Metabase password fallback")
    elasticsearch_base_url: Optional[str] = Field(default=None, description="Elasticsearch base URL fallback")
    elasticsearch_api_key: Optional[str] = Field(default=None, description="Elasticsearch API key fallback")
    elasticsearch_bearer_token: Optional[str] = Field(default=None, description="Elasticsearch bearer token fallback")
    elasticsearch_username: Optional[str] = Field(default=None, description="Elasticsearch username fallback")
    elasticsearch_password: Optional[str] = Field(default=None, description="Elasticsearch password fallback")
    elasticsearch_ignore_ssl_issues: bool = Field(default=False, description="Ignore Elasticsearch SSL certificate verification")
    splunk_base_url: Optional[str] = Field(default=None, description="Splunk base URL fallback")
    splunk_auth_token: Optional[str] = Field(default=None, description="Splunk auth token fallback")
    splunk_allow_unauthorized_certs: bool = Field(default=False, description="Allow self-signed Splunk certificates")
    urlscan_api_key: Optional[str] = Field(default=None, description="urlscan.io API key fallback")
    urlscan_base_url: str = Field(default="https://urlscan.io/api/v1", description="urlscan.io API base URL")
    hunter_api_key: Optional[str] = Field(default=None, description="Hunter API key fallback")
    hunter_base_url: str = Field(default="https://api.hunter.io/v2", description="Hunter API base URL")
    mailcheck_api_key: Optional[str] = Field(default=None, description="Mailcheck API key fallback")
    mailcheck_base_url: str = Field(default="https://api.mailcheck.co/v1", description="Mailcheck API base URL")
    peekalink_api_key: Optional[str] = Field(default=None, description="Peekalink API key fallback")
    peekalink_base_url: str = Field(default="https://api.peekalink.io", description="Peekalink API base URL")
    jina_api_key: Optional[str] = Field(default=None, description="Jina AI API key fallback")
    jina_reader_base_url: str = Field(default="https://r.jina.ai", description="Jina Reader API base URL")
    jina_search_base_url: str = Field(default="https://s.jina.ai", description="Jina Search API base URL")
    jina_deepsearch_base_url: str = Field(default="https://deepsearch.jina.ai/v1", description="Jina DeepSearch API base URL")
    misp_base_url: Optional[str] = Field(default=None, description="MISP base URL fallback")
    misp_api_key: Optional[str] = Field(default=None, description="MISP API key fallback")
    misp_allow_unauthorized_certs: bool = Field(default=False, description="Allow self-signed MISP certificates")
    thehive_base_url: Optional[str] = Field(default=None, description="TheHive base URL fallback")
    thehive_api_key: Optional[str] = Field(default=None, description="TheHive API key fallback")
    thehive_api_version: str = Field(default="v1", description="TheHive API version hint")
    thehive_allow_unauthorized_certs: bool = Field(default=False, description="Allow self-signed TheHive certificates")
    securityscorecard_api_key: Optional[str] = Field(default=None, description="SecurityScorecard API key fallback")
    securityscorecard_base_url: str = Field(default="https://api.securityscorecard.io", description="SecurityScorecard API base URL")
    okta_access_token: Optional[str] = Field(default=None, description="Okta SSWS API token fallback")
    okta_domain: Optional[str] = Field(default=None, description="Okta org domain fallback")
    okta_base_url: Optional[str] = Field(default=None, description="Okta org base URL override")
    elastic_security_base_url: Optional[str] = Field(default=None, description="Elastic Security Kibana base URL fallback")
    elastic_security_api_key: Optional[str] = Field(default=None, description="Elastic Security API key fallback")
    elastic_security_username: Optional[str] = Field(default=None, description="Elastic Security basic-auth username fallback")
    elastic_security_password: Optional[str] = Field(default=None, description="Elastic Security basic-auth password fallback")
    baserow_api_token: Optional[str] = Field(default=None, description="Baserow API or database token fallback")
    baserow_base_url: str = Field(default="https://api.baserow.io", description="Baserow API base URL")
    supabase_url: Optional[str] = Field(default=None, description="Supabase project URL fallback")
    supabase_service_role_key: Optional[str] = Field(default=None, description="Supabase service role key fallback")
    supabase_api_key: Optional[str] = Field(default=None, description="Supabase anon/API key fallback")
    supabase_base_url: Optional[str] = Field(default=None, description="Supabase REST API base URL override")
    quickbase_hostname: Optional[str] = Field(default=None, description="Quickbase realm hostname fallback")
    quickbase_user_token: Optional[str] = Field(default=None, description="Quickbase user token fallback")
    quickbase_base_url: str = Field(default="https://api.quickbase.com/v1", description="Quickbase API base URL")
    seatable_api_token: Optional[str] = Field(default=None, description="SeaTable API token fallback")
    seatable_base_url: str = Field(default="https://cloud.seatable.io", description="SeaTable API base URL")
    stackby_api_key: Optional[str] = Field(default=None, description="Stackby API key fallback")
    stackby_base_url: str = Field(default="https://stackby.com/api/betav1", description="Stackby API base URL")
    nocodb_api_token: Optional[str] = Field(default=None, description="NocoDB API token fallback")
    nocodb_base_url: str = Field(default="https://app.nocodb.com", description="NocoDB API base URL")
    nocodb_auth_header: str = Field(default="xc-token", description="NocoDB auth header name: xc-token or xc-auth")
    coda_api_token: Optional[str] = Field(default=None, description="Coda API token fallback")
    coda_base_url: str = Field(default="https://coda.io/apis/v1", description="Coda API base URL")
    grist_api_key: Optional[str] = Field(default=None, description="Grist API key fallback")
    grist_base_url: str = Field(default="https://docs.getgrist.com/api", description="Grist API base URL")
    adalo_api_key: Optional[str] = Field(default=None, description="Adalo API key fallback")
    adalo_app_id: Optional[str] = Field(default=None, description="Adalo app ID fallback")
    adalo_base_url: Optional[str] = Field(default=None, description="Adalo app API base URL override")
    bubble_api_token: Optional[str] = Field(default=None, description="Bubble Data API token fallback")
    bubble_app_name: Optional[str] = Field(default=None, description="Bubble app name fallback")
    bubble_environment: str = Field(default="live", description="Bubble environment: live or development")
    bubble_domain: Optional[str] = Field(default=None, description="Bubble self-hosted/custom domain fallback")
    bubble_base_url: Optional[str] = Field(default=None, description="Bubble API base URL override")
    cockpit_base_url: Optional[str] = Field(default=None, description="Cockpit site or API base URL fallback")
    cockpit_access_token: Optional[str] = Field(default=None, description="Cockpit access token fallback")
    kobotoolbox_api_token: Optional[str] = Field(default=None, description="KoBoToolbox API token fallback")
    kobotoolbox_base_url: str = Field(default="https://kf.kobotoolbox.org", description="KoBoToolbox API root URL")
    telegram_api_base_url: str = Field(default="https://api.telegram.org", description="Telegram Bot API base URL")
    webex_access_token: Optional[str] = Field(default=None, description="Webex access token fallback")
    webex_base_url: str = Field(default="https://webexapis.com/v1", description="Webex API base URL")
    webex_webhook_secret: Optional[str] = Field(default=None, description="Webex webhook HMAC secret")
    webex_bot_person_id: Optional[str] = Field(default=None, description="Webex bot person ID override")
    webex_bot_email: Optional[str] = Field(default=None, description="Webex bot email override")
    webex_show_tool_events: bool = Field(default=False, description="Show compact tool-call events in Webex bot replies")
    whatsapp_access_token: Optional[str] = Field(default=None, description="WhatsApp Business Cloud access token fallback")
    whatsapp_business_account_id: Optional[str] = Field(default=None, description="WhatsApp Business account ID fallback")
    whatsapp_phone_number_id: Optional[str] = Field(default=None, description="WhatsApp sender phone number ID fallback")
    whatsapp_base_url: str = Field(default="https://graph.facebook.com/v19.0", description="WhatsApp Graph API base URL")
    whatsapp_webhook_verify_token: Optional[str] = Field(default=None, description="WhatsApp Cloud webhook verification token")
    whatsapp_app_secret: Optional[str] = Field(default=None, description="Meta app secret for WhatsApp webhook signature verification")
    whatsapp_show_tool_events: bool = Field(default=False, description="Show compact tool-call events in WhatsApp bot replies")
    messenger_page_access_token: Optional[str] = Field(default=None, description="Meta Messenger Page access token fallback")
    messenger_page_id: Optional[str] = Field(default=None, description="Meta Messenger Page ID fallback")
    messenger_webhook_verify_token: Optional[str] = Field(default=None, description="Meta Messenger webhook verification token")
    messenger_app_secret: Optional[str] = Field(default=None, description="Meta app secret for Messenger webhook signature verification")
    messenger_graph_api_base_url: str = Field(default="https://graph.facebook.com/v23.0", description="Meta Messenger Graph API base URL")
    messenger_show_tool_events: bool = Field(default=False, description="Show compact tool-call events in Messenger bot replies")
    instagram_access_token: Optional[str] = Field(default=None, description="Meta Instagram Messaging API access token fallback")
    instagram_ig_user_id: Optional[str] = Field(default=None, description="Instagram professional account ID used for Messaging API replies")
    instagram_webhook_verify_token: Optional[str] = Field(default=None, description="Meta Instagram webhook verification token")
    instagram_app_secret: Optional[str] = Field(default=None, description="Meta app secret for Instagram webhook signature verification")
    instagram_graph_api_base_url: str = Field(default="https://graph.instagram.com/v23.0", description="Meta Instagram Messaging API base URL")
    instagram_show_tool_events: bool = Field(default=False, description="Show compact tool-call events in Instagram bot replies")
    facebook_access_token: Optional[str] = Field(default=None, description="Facebook Graph API access token fallback")
    facebook_app_secret: Optional[str] = Field(default=None, description="Facebook app secret fallback for appsecret_proof")
    facebook_graph_base_url: str = Field(default="https://graph.facebook.com/v23.0", description="Facebook Graph API base URL")
    linkedin_access_token: Optional[str] = Field(default=None, description="LinkedIn OAuth access token fallback")
    linkedin_base_url: str = Field(default="https://api.linkedin.com", description="LinkedIn API base URL")
    linkedin_api_version: str = Field(default="202604", description="LinkedIn REST API version header")
    twitter_bearer_token: Optional[str] = Field(default=None, description="X/Twitter bearer token fallback")
    twitter_access_token: Optional[str] = Field(default=None, description="X/Twitter OAuth access token fallback")
    twitter_api_base_url: str = Field(default="https://api.twitter.com/2", description="X/Twitter API v2 base URL")
    discord_base_url: str = Field(default="https://discord.com/api/v10", description="Discord REST API base URL")
    mattermost_access_token: Optional[str] = Field(default=None, description="Mattermost access token fallback")
    mattermost_base_url: Optional[str] = Field(default=None, description="Mattermost server base URL")
    matrix_access_token: Optional[str] = Field(default=None, description="Matrix access token fallback")
    matrix_base_url: str = Field(default="https://matrix-client.matrix.org/_matrix/client/v3", description="Matrix Client-Server API base URL")
    rocketchat_auth_token: Optional[str] = Field(default=None, description="Rocket.Chat auth token fallback")
    rocketchat_user_id: Optional[str] = Field(default=None, description="Rocket.Chat user ID fallback")
    rocketchat_base_url: Optional[str] = Field(default=None, description="Rocket.Chat server base URL")
    zulip_api_key: Optional[str] = Field(default=None, description="Zulip API key fallback")
    zulip_email: Optional[str] = Field(default=None, description="Zulip email fallback")
    zulip_base_url: Optional[str] = Field(default=None, description="Zulip organization base URL")
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
    scheduler_missed_work_policy: Literal["run", "ask"] = Field(
        default="run",
        description=(
            "Startup policy for scheduled TODOs that became due while the "
            "scheduler was offline: run immediately, or ask by holding them "
            "until an admin releases missed work."
        ),
    )
    scheduler_active_execution_stale_minutes: int = Field(
        default=1440,
        ge=1,
        le=10080,
        description=(
            "Minutes before an active scheduled TODO execution marker is "
            "considered stale after an interrupted scheduler process."
        ),
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
        description="Trigger auto-compact at this percentage of context window (used when compact_threshold_mode='percentage')"
    )
    compact_threshold_mode: Literal["percentage", "tokens"] = Field(
        default="percentage",
        description="Whether auto-compact trigger uses 'percentage' of context window or an absolute 'tokens' count"
    )
    compact_threshold_tokens: int = Field(
        default=100_000,
        ge=1_000,
        le=2_000_000,
        description="Trigger auto-compact when input tokens reach this absolute count (used when compact_threshold_mode='tokens'); clamped to model context limit at runtime"
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
    memory_char_limit: int = Field(
        default=8000,
        ge=1,
        le=2000000,
        description="Maximum persisted characters for global memories and per-thread notepads"
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
    tts_provider: Literal["none", "openai", "qwen3", "gemini", "cartesia"] = Field(  # type: ignore[assignment]
        default="none",
        description="TTS provider: none, openai, qwen3, gemini, cartesia",
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
        """Get the path to the packaged soul.md system prompt (shipped default)."""
        return PACKAGE_ROOT / "config" / "soul.md"

    @property
    def system_prompt_override_path(self) -> Path:
        """Get the path to the user-editable system-prompt override.

        Lives in the (writable, gitignored) data dir so the packaged soul.md is
        never mutated. When this file exists and is non-empty, load_soul() uses
        it instead of soul.md; deleting it restores the shipped default.
        """
        return self.data_dir / "system_prompt.md"

    @property
    def dream_prompt_path(self) -> Path:
        """Get the path to the dream cycle system prompt."""
        return PACKAGE_ROOT / "config" / "dream_prompt.md"

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
        provider = normalize_llm_provider(self.llm_provider)
        if provider == "anthropic":
            if self.llm_base_url:
                return self.anthropic_api_key
            return self.anthropic_direct_api_key or self.anthropic_api_key

        return resolve_provider_api_key(provider, settings=self)

    def load_soul(self) -> str:
        """Load the base system prompt.

        Precedence: a non-empty user override (system_prompt_override_path) wins,
        otherwise the packaged soul.md, otherwise a minimal fallback.
        """
        override_path = self.system_prompt_override_path
        if override_path.exists():
            override_text = override_path.read_text(encoding="utf-8").strip()
            if override_text:
                return override_text
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8")
        return "You are Nymeria, a helpful AI assistant."

    def load_dream_prompt(self) -> str:
        """Load the dream-cycle system prompt. Returns a minimal fallback if absent."""
        if self.dream_prompt_path.exists():
            return self.dream_prompt_path.read_text(encoding="utf-8")
        return (
            "You are in a dream cycle. Read the parent thread's memory and "
            "instructions, prune what's stale, tweak instructions if patterns "
            "have emerged, and schedule TODOs only when justified."
        )

    def validate_runtime(self) -> Tuple[List[str], List[str]]:
        """
        Validate runtime configuration settings.

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
        provider = normalize_llm_provider(self.llm_provider)
        provider_spec = get_llm_provider_spec(provider)
        if not provider_key and provider_requires_api_key(provider):
            env_hint = ""
            if provider_spec and provider_spec.api_key_env_vars:
                env_hint = ", ".join(provider_spec.api_key_env_vars)
            else:
                env_hint = f"{provider.upper()}_API_KEY"
            warnings.append(
                f"No env API key for LLM provider '{self.llm_provider}'.\n"
                f"  Set one of: {env_hint}; or save an active credential-vault "
                f"record for provider '{provider}' with secret field 'api_key'."
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

        # Slack bot warning
        if (self.slack_bot_token or self.slack_app_token) and not provider_key:
            warnings.append(
                "SLACK_BOT_TOKEN or SLACK_APP_TOKEN is set but no LLM API key configured.\n"
                "  The Slack bot will not be able to process messages."
            )

        # Matrix bot warning
        if (self.matrix_access_token or self.matrix_password) and not provider_key:
            warnings.append(
                "MATRIX_ACCESS_TOKEN or MATRIX_PASSWORD is set but no LLM API key configured.\n"
                "  The Matrix bot will not be able to process messages."
            )

        if (self.signal_http_url or self.signal_account) and not provider_key:
            warnings.append(
                "SIGNAL_HTTP_URL or SIGNAL_ACCOUNT is set but no LLM API key configured.\n"
                "  The Signal bot will not be able to process messages."
            )

        if (self.instagram_access_token or self.instagram_ig_user_id) and not provider_key:
            warnings.append(
                "INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_IG_USER_ID is set but no LLM API key configured.\n"
                "  The Instagram bot will not be able to process messages."
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
