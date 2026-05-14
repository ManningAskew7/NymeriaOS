"""Settings and model-catalog API schemas."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


LLMProviderName = Literal["openrouter", "openai", "anthropic"]
OpenAIApiMode = Literal["chat_completions", "responses"]


class ServerSettingsResponse(BaseModel):
    """Response model for server settings."""

    llm_provider: str
    llm_model: str
    llm_fast_model: Optional[str] = None
    llm_fallback_models: list[str] = Field(default_factory=list)
    llm_temperature: float
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[str] = None
    llm_extended_thinking: bool = False
    llm_use_model_defaults: bool = False
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[OpenAIApiMode] = "responses"
    llm_stream_max_retries: int
    llm_stream_retry_initial_delay: float
    llm_stream_retry_max_delay: float
    context_management: str
    compact_threshold: float
    compact_keep_messages: int
    compact_model: Optional[str] = None
    sliding_window_cycles: int
    tool_output_max_chars: int
    log_level: str
    watchdog_enabled: bool
    watchdog_interval_minutes: int
    todo_staleness_minutes: int
    activity_retention_hours: int
    tts_provider: str = "none"
    tts_base_url: Optional[str] = None
    tts_model: str = "tts-1-hd"
    tts_voice: str = "nova"
    tts_output_format: str = "mp3"
    tts_speed: float = 1.0
    stt_provider: str = "none"
    stt_base_url: Optional[str] = None
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: Optional[str] = None
    voice_default_thread_id: Optional[str] = None


class ServerSettingsUpdate(BaseModel):
    """Request model for updating server settings."""

    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_fast_model: Optional[str] = None
    llm_fallback_models: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[str] = None
    llm_extended_thinking: Optional[bool] = None
    llm_use_model_defaults: Optional[bool] = None
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[OpenAIApiMode] = None
    # Accepted by PATCH /settings only. Secret values are intentionally absent
    # from ServerSettingsResponse.
    anthropic_api_key: Optional[str] = None
    anthropic_direct_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    embedding_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    perplexity_api_key: Optional[str] = None
    wolfram_alpha_app_id: Optional[str] = None
    searxng_base_url: Optional[str] = None
    nasa_api_key: Optional[str] = None
    openweathermap_api_key: Optional[str] = None
    npm_registry_url: Optional[str] = None
    github_token: Optional[str] = None
    github_api_base_url: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_base_url: Optional[str] = None
    llm_stream_max_retries: Optional[int] = None
    llm_stream_retry_initial_delay: Optional[float] = None
    llm_stream_retry_max_delay: Optional[float] = None
    context_management: Optional[str] = None
    compact_threshold: Optional[float] = None
    compact_keep_messages: Optional[int] = None
    compact_model: Optional[str] = None
    sliding_window_cycles: Optional[int] = None
    tool_output_max_chars: Optional[int] = None
    log_level: Optional[str] = None
    watchdog_enabled: Optional[bool] = None
    watchdog_interval_minutes: Optional[int] = None
    todo_staleness_minutes: Optional[int] = None
    activity_retention_hours: Optional[int] = None
    tts_provider: Optional[str] = None
    tts_base_url: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_output_format: Optional[str] = None
    tts_speed: Optional[float] = None
    stt_provider: Optional[str] = None
    stt_base_url: Optional[str] = None
    stt_model: Optional[str] = None
    stt_language: Optional[str] = None
    voice_default_thread_id: Optional[str] = None


class LLMProviderTestRequest(BaseModel):
    """Request model for testing an arbitrary provider configuration."""

    model_config = ConfigDict(extra="forbid")

    llm_provider: LLMProviderName
    llm_model: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[OpenAIApiMode] = "responses"

    @field_validator("llm_model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("llm_model cannot be blank")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def _strip_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        return value or None


class LLMProviderTestResponse(BaseModel):
    """Sanitized response for a provider test attempt."""

    ok: bool
    provider: LLMProviderName
    model: str
    message: str
    openai_api_mode: Optional[OpenAIApiMode] = None
    status_code: Optional[int] = None
    error_type: Optional[str] = None


HIDDEN_CONFIG_SETTINGS = {
    # Retained on Settings for legacy .env compatibility, but no longer part of
    # the public/admin configuration API now that account tokens are authoritative.
    "nymeria_api_key",
}


class OpenRouterKeyDiagnostics(BaseModel):
    """Runtime details for the currently active OpenRouter API key."""

    label: Optional[str] = None
    limit: Optional[float] = None
    limit_remaining: Optional[float] = None
    usage: Optional[float] = None
    limit_reset: Optional[str] = None
    include_byok_in_limit: Optional[bool] = None
    is_management_key: Optional[bool] = None
    fetch_error: Optional[str] = None


class LLMRuntimeDiagnosticsResponse(BaseModel):
    """Runtime diagnostics for currently active LLM configuration."""

    provider: str
    model: str
    llm_max_tokens: Optional[int] = None
    effective_max_tokens: Optional[int] = None
    source_env_files: list[str] = []
    openrouter: Optional[OpenRouterKeyDiagnostics] = None
