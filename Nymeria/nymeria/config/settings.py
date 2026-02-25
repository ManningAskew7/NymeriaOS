"""Nymeria settings management using Pydantic."""

from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), str(PROJECT_ROOT / ".env.docker")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API Authentication (required for security)
    nymeria_api_key: Optional[str] = Field(default=None, description="API key for authentication")

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
        default="*",
        description="Comma-separated list of allowed CORS origins, or '*' for all"
    )

    # Worker mode flag
    worker_mode: bool = Field(
        default=False,
        description="Run in worker mode (ticker only, no API server)"
    )

    # Webhook security
    webhook_secret: Optional[str] = Field(
        default=None,
        description="Secret for validating incoming webhooks"
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

    # Messaging Platform Credentials - Slack
    slack_webhook_url: Optional[str] = Field(
        default=None,
        description="Slack webhook URL for notifications"
    )
    slack_bot_token: Optional[str] = Field(
        default=None,
        description="Slack bot token for two-way communication"
    )

    # LLM Configuration
    llm_provider: Literal["openrouter", "openai", "anthropic"] = Field(
        default="anthropic", description="LLM provider"
    )
    llm_model: str = Field(
        default="claude-sonnet-4-20250514", description="Model identifier"
    )
    llm_temperature: float = Field(default=1.0, ge=0.0, le=2.0)

    # Advanced LLM settings (optional - only sent if explicitly set)
    llm_max_tokens: Optional[int] = Field(
        default=None, ge=1, le=32000, description="Maximum output tokens"
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
    llm_reasoning_effort: Optional[str] = Field(
        default=None, description="Reasoning effort for compatible models: low, medium, high"
    )
    llm_extended_thinking: bool = Field(
        default=False, description="Enable extended thinking/reasoning for compatible models"
    )
    llm_use_model_defaults: bool = Field(
        default=False,
        description="Use model-specific defaults for temperature/top_p/frequency_penalty instead of global values"
    )

    # API Keys
    openai_api_key: Optional[str] = Field(default=None)
    anthropic_api_key: Optional[str] = Field(default=None)
    openrouter_api_key: Optional[str] = Field(default=None)
    perplexity_api_key: Optional[str] = Field(default=None)
    perplexity_search_model: str = Field(default="sonar-pro", description="Default Perplexity model for web search")

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
            return Path(self.sqlite_path)
        return self.data_dir / "nymeria.db"

    # User Timezone
    user_timezone: str = Field(
        default="Australia/Sydney",
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
    max_self_invokes_per_hour: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum self_invoke calls per user per hour"
    )
    # Context Management Settings
    context_management: Literal["auto_compact", "sliding_window", "none"] = Field(
        default="auto_compact",
        description="Strategy: auto_compact (default), sliding_window (legacy), none"
    )
    compact_threshold: float = Field(
        default=0.8,
        ge=0.5,
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

    @property
    def tasks_db_path(self) -> Path:
        """Get the tasks database path (separate from conversations)."""
        return self.data_dir / "tasks.db"

    # Server Configuration
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # Windows Service Configuration
    service_name: str = Field(
        default="NymeriaService",
        description="Windows service name"
    )
    service_display_name: str = Field(
        default="Nymeria AI Assistant",
        description="Display name in Windows Services manager"
    )
    service_description: str = Field(
        default="Personal AI Assistant service providing REST API access",
        description="Service description"
    )
    service_auto_start: bool = Field(
        default=True,
        description="Auto-start service on system boot"
    )
    service_log_file: str = Field(
        default="service.log",
        description="Log filename for service mode"
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

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    audit_log_enabled: bool = Field(default=True)

    # Paths
    @property
    def project_root(self) -> Path:
        """Get the project root directory."""
        return Path(__file__).parent.parent.parent

    @property
    def data_dir(self) -> Path:
        """
        Get the data directory.

        Uses nymeria_data_dir if set (for Docker volumes), otherwise falls back
        to project_root/data for local development.
        """
        if self.nymeria_data_dir:
            return Path(self.nymeria_data_dir)
        return self.project_root / "data"

    @property
    def soul_path(self) -> Path:
        """Get the path to the soul.md system prompt."""
        return Path(__file__).parent / "soul.md"

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
    def cors_origins_list(self) -> List[str]:
        """Get CORS origins as a list."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def get_api_key_for_provider(self) -> Optional[str]:
        """Get the API key for the configured LLM provider."""
        key_map = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
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

        # Check for NYMERIA_API_KEY (required for security)
        if not self.nymeria_api_key:
            errors.append(
                "NYMERIA_API_KEY not set. This is required for API authentication.\n"
                "  Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
                "  Then add it to your .env file."
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
