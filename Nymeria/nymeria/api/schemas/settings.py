"""Settings and model-catalog API schemas."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


LLMProviderName = str
OpenAIApiMode = Literal["chat_completions", "responses"]


class LLMProviderSpecResponse(BaseModel):
    """Public LLM provider metadata for settings UIs and docs consumers."""

    id: str
    label: str
    api_format: str
    default_base_url: Optional[str] = None
    api_key_env_vars: list[str] = Field(default_factory=list)
    base_url_env_vars: list[str] = Field(default_factory=list)
    default_model: Optional[str] = None
    default_api_mode: OpenAIApiMode | str = "chat_completions"
    supports_chat_completions: bool = True
    supports_responses: bool = False
    requires_api_key: bool = True
    requires_base_url: bool = False
    docs_url: Optional[str] = None
    notes: str = ""
    aliases: list[str] = Field(default_factory=list)


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
    bitly_token: Optional[str] = None
    bitly_base_url: Optional[str] = None
    brandfetch_api_key: Optional[str] = None
    brandfetch_base_url: Optional[str] = None
    marketstack_api_key: Optional[str] = None
    marketstack_base_url: Optional[str] = None
    deepl_api_key: Optional[str] = None
    deepl_api_plan: Optional[str] = None
    deepl_base_url: Optional[str] = None
    todoist_api_key: Optional[str] = None
    todoist_base_url: Optional[str] = None
    trello_api_key: Optional[str] = None
    trello_api_token: Optional[str] = None
    trello_base_url: Optional[str] = None
    asana_access_token: Optional[str] = None
    asana_base_url: Optional[str] = None
    linear_api_key: Optional[str] = None
    linear_api_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_access_token: Optional[str] = None
    jira_base_url: Optional[str] = None
    clickup_access_token: Optional[str] = None
    clickup_base_url: Optional[str] = None
    slack_bot_token: Optional[str] = None
    slack_access_token: Optional[str] = None
    slack_base_url: Optional[str] = None
    notion_api_key: Optional[str] = None
    notion_version: Optional[str] = None
    notion_base_url: Optional[str] = None
    airtable_access_token: Optional[str] = None
    airtable_api_key: Optional[str] = None
    airtable_base_url: Optional[str] = None
    hubspot_access_token: Optional[str] = None
    hubspot_base_url: Optional[str] = None
    zendesk_email: Optional[str] = None
    zendesk_api_token: Optional[str] = None
    zendesk_access_token: Optional[str] = None
    zendesk_subdomain: Optional[str] = None
    zendesk_base_url: Optional[str] = None
    mailchimp_api_key: Optional[str] = None
    mailchimp_access_token: Optional[str] = None
    mailchimp_server_prefix: Optional[str] = None
    mailchimp_base_url: Optional[str] = None
    freshdesk_api_key: Optional[str] = None
    freshdesk_domain: Optional[str] = None
    freshdesk_base_url: Optional[str] = None
    helpscout_access_token: Optional[str] = None
    helpscout_base_url: Optional[str] = None
    intercom_access_token: Optional[str] = None
    intercom_base_url: Optional[str] = None
    intercom_version: Optional[str] = None
    pipedrive_api_token: Optional[str] = None
    pipedrive_access_token: Optional[str] = None
    pipedrive_base_url: Optional[str] = None
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_api_key_sid: Optional[str] = None
    twilio_base_url: Optional[str] = None
    sendgrid_api_key: Optional[str] = None
    sendgrid_base_url: Optional[str] = None
    mailgun_api_key: Optional[str] = None
    mailgun_domain: Optional[str] = None
    mailgun_base_url: Optional[str] = None
    brevo_api_key: Optional[str] = None
    brevo_base_url: Optional[str] = None
    mailjet_api_key: Optional[str] = None
    mailjet_secret_key: Optional[str] = None
    mailjet_sms_token: Optional[str] = None
    mailjet_base_url: Optional[str] = None
    mandrill_api_key: Optional[str] = None
    mandrill_base_url: Optional[str] = None
    messagebird_access_key: Optional[str] = None
    messagebird_base_url: Optional[str] = None
    mocean_api_key: Optional[str] = None
    mocean_api_secret: Optional[str] = None
    mocean_base_url: Optional[str] = None
    msg91_auth_key: Optional[str] = None
    msg91_base_url: Optional[str] = None
    stripe_secret_key: Optional[str] = None
    stripe_base_url: Optional[str] = None
    shopify_shop: Optional[str] = None
    shopify_access_token: Optional[str] = None
    shopify_api_key: Optional[str] = None
    shopify_password: Optional[str] = None
    shopify_api_version: Optional[str] = None
    shopify_base_url: Optional[str] = None
    woocommerce_url: Optional[str] = None
    woocommerce_base_url: Optional[str] = None
    woocommerce_consumer_key: Optional[str] = None
    woocommerce_consumer_secret: Optional[str] = None
    chargebee_api_key: Optional[str] = None
    chargebee_site: Optional[str] = None
    chargebee_base_url: Optional[str] = None
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


class LLMProviderTestSuiteRequest(BaseModel):
    """Request model for the production-readiness provider test suite."""

    model_config = ConfigDict(extra="forbid")

    llm_provider: LLMProviderName
    llm_model: Optional[str] = None
    api_key: Optional[SecretStr] = None
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[OpenAIApiMode] = "chat_completions"
    run_model_list: bool = True
    run_chat_completion: bool = True
    run_tool_call: bool = True
    allow_billable: bool = False
    prefer_free_model: bool = True
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)

    @field_validator("llm_model")
    @classmethod
    def _strip_optional_model(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("llm_base_url")
    @classmethod
    def _strip_optional_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        return value or None


class LLMProviderTestSuiteStepResponse(BaseModel):
    """One step returned by the provider test suite."""

    name: str
    status: str
    ok: bool
    message: str
    url: Optional[str] = None
    status_code: Optional[int] = None
    latency_ms: Optional[int] = None
    error_type: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMProviderTestSuiteResponse(BaseModel):
    """Sanitized production-readiness report for a provider setup."""

    ok: bool
    provider: LLMProviderName
    requested_provider: LLMProviderName
    model: Optional[str] = None
    effective_base_url: Optional[str] = None
    effective_api_mode: Optional[OpenAIApiMode] = None
    credential_source: str
    models_count: Optional[int] = None
    message: str
    steps: list[LLMProviderTestSuiteStepResponse]


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
