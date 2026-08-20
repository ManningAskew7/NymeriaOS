"""Settings and model-catalog API schemas."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# Top-level stdlib-only leaf: importing it does NOT pull `config.settings`, so this
# schema module stays import-light (see the ReasoningEffort re-declaration below).
from nymeria._env_overrides import FIELD_ENV_OVERRIDES


LLMProviderName = str
OpenAIApiMode = Literal["chat_completions", "responses"]
ProviderRoute = Literal["native", "openai_compat", "anthropic_messages"]
ProviderTier = Literal["native", "gateway", "unverified"]
# Mirrors nymeria.config.settings.ReasoningEffort (re-declared so schemas stay
# import-light). "off" explicitly disables thinking; None = unset/inherit.
ReasoningEffortName = Literal["off", "low", "medium", "high", "xhigh", "max"]


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
    tier: ProviderTier = "unverified"
    notes_for_user: str = ""
    supported_routes: list[ProviderRoute] = Field(default_factory=list)
    default_route: ProviderRoute = "native"
    openai_compat_base_url: Optional[str] = None
    verified: bool = False
    anthropic_native_for_claude: bool = False
    reasoning_passback_verified: bool = False
    # Curated "get a key" affordance (empty for most providers); the CLI
    # wizard's picker panel reads the same fields off the spec directly.
    signup_url: str = ""
    signup_guidance: str = ""


class RagEmbedderOptionResponse(BaseModel):
    """One embedder choice from the CLI wizard's RAG catalog (setup/rag_catalog.py)."""

    id: str
    tier: str
    label: str
    description: str
    provider: str
    model: str
    dimensions: int
    requires_key: bool
    key_vendor: Optional[str] = None
    base_url: Optional[str] = None
    input_type: Optional[str] = None
    pricing: str = ""
    key_label: str = "API key"
    eval_tag: str = ""


class RagRerankerOptionResponse(BaseModel):
    """One reranker choice from the CLI wizard's RAG catalog."""

    id: str
    tier: str
    label: str
    description: str
    provider: str
    model: Optional[str] = None
    requires_key: bool
    key_vendor: Optional[str] = None
    pricing: str = ""
    key_label: str = "API key"
    eval_tag: str = ""


class RagComboResponse(BaseModel):
    """A recommended embedder + reranker pairing from the internal eval."""

    label: str
    embedder_id: str
    reranker_id: str
    description: str


class RagCatalogResponse(BaseModel):
    """The RAG catalog the CLI wizard renders, served so GUIs share one source."""

    embedders: list[RagEmbedderOptionResponse]
    rerankers: list[RagRerankerOptionResponse]
    combos: list[RagComboResponse]
    quickstart_embedder: str
    quickstart_reranker: str


class SystemPromptResponse(BaseModel):
    """Current base system prompt plus the shipped default, for the editor."""

    content: str = Field(..., description="Effective base system prompt in use")
    default_content: str = Field(
        ..., description="Shipped soul.md default, shown for reset/compare"
    )
    is_override: bool = Field(
        ..., description="True when a user override file is active (not the default)"
    )


class SystemPromptUpdate(BaseModel):
    """Update the base system prompt override. Blank content resets to default."""

    content: str = Field(default="", max_length=100_000)


class DreamPromptInfo(BaseModel):
    """One dream prompt (system or kickoff) plus its shipped default and override flag."""

    content: str = Field(..., description="Effective global default in use")
    default_content: str = Field(
        ..., description="Shipped default, shown for reset/compare"
    )
    is_override: bool = Field(
        ..., description="True when a data-dir override is active (not the shipped file)"
    )


class DreamPromptsResponse(BaseModel):
    """Both global dream prompts for the editor: the system prompt and the kickoff."""

    system: DreamPromptInfo
    kickoff: DreamPromptInfo


class DreamPromptsUpdate(BaseModel):
    """Update the global dream-prompt overrides.

    Each field is optional; a provided blank string clears that override (resets to
    the shipped default), a provided non-blank string writes it, and an omitted
    field is left untouched.
    """

    system: Optional[str] = Field(default=None, max_length=50_000)
    kickoff: Optional[str] = Field(default=None, max_length=10_000)


class ServerSettingsResponse(BaseModel):
    """Response model for server settings."""

    llm_provider: str
    llm_model: str
    llm_fast_model: Optional[str] = None
    llm_smart_model: Optional[str] = None
    llm_background_model: Optional[str] = None
    llm_background_base_url: Optional[str] = None
    # Read-only effective "provider:model" the fast/smart/background aliases
    # resolve to (configured value or default), used by the per-thread tier
    # quick-pick.
    llm_fast_model_resolved: Optional[str] = None
    llm_smart_model_resolved: Optional[str] = None
    llm_background_model_resolved: Optional[str] = None
    llm_fallback_models: list[str] = Field(default_factory=list)
    llm_temperature: float
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[str] = None
    llm_extended_thinking: bool = False
    dynamic_tool_binding: bool = True
    sequential_tool_execution: bool = False
    hooks_enabled: bool = True
    llm_use_model_defaults: bool = False
    llm_base_url: Optional[str] = None
    llm_context_length: Optional[int] = None
    llm_ollama_num_ctx: Optional[int] = None
    llm_provider_route: Optional[ProviderRoute] = None
    openai_api_mode: Optional[OpenAIApiMode] = "responses"
    llm_stream_max_retries: int
    llm_stream_retry_initial_delay: float
    llm_stream_retry_max_delay: float
    llm_fallback_hold_seconds: int
    llm_fallback_switch_mode: str = "auto"
    llm_fallback_prompt_timeout_seconds: int = 180
    llm_refusal_swap_mode: str = "ask"
    context_management: str
    compact_threshold: float
    compact_threshold_mode: str = "tokens"
    compact_threshold_tokens: int = 200_000
    compact_keep_messages: int
    compact_model: Optional[str] = None
    compact_proactive_enabled: bool = False
    compact_proactive_idle_seconds: int = 210
    compact_proactive_min_pct: int = 85
    sliding_window_cycles: int
    tool_output_max_chars: int
    tool_timeout: int = 300
    tool_timing_in_results: bool = False
    memory_char_limit: int = 8000
    memory_max_entries: int = 100
    memory_value_max_chars: int = 1000
    agent_max_iterations: int = 500
    user_timezone: str = "UTC"
    log_level: str
    watchdog_enabled: bool
    watchdog_interval_minutes: int
    todo_staleness_minutes: int
    activity_retention_hours: int
    dream_default_min_interval_hours: int = 6
    dream_default_min_idle_minutes: int = 30
    dream_default_min_turns_since_last: int = 10
    dream_default_model: Optional[str] = None
    tts_provider: str = "none"
    tts_base_url: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_output_format: str = "mp3"
    tts_speed: float = 1.0
    stt_provider: str = "none"
    stt_base_url: Optional[str] = None
    stt_model: Optional[str] = None
    stt_language: Optional[str] = None
    voice_default_thread_id: Optional[str] = None
    # RAG / semantic memory (see config/settings.py and the desktop RAG tab).
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: Optional[int] = None
    embedding_base_url: Optional[str] = None
    embedding_input_type: Optional[str] = None
    rag_retrieval_mode: str = "hybrid"
    rag_embed_tool_results: bool = True
    rag_rerank_enabled: bool = False
    rag_rerank_provider: str = "llm"
    rag_rerank_model: Optional[str] = None


class ServerSettingsUpdate(BaseModel):
    """Request model for updating server settings."""

    # extra="allow" is deliberate and load-bearing: unknown keys must reach
    # `apply_server_settings_update`, which rejects them with a 400 naming the
    # key and suggesting near-matches. Pydantic's extra="ignore" default
    # silently dropped unknown keys, so every caller (the GUI, /env set,
    # /settings set) reported success for writes that never happened.
    model_config = ConfigDict(extra="allow")

    # Platform/infra fields advertised by /env show (_ENV_CATEGORIES) but
    # formerly missing here, so PATCHing them false-succeeded; visibility
    # implies patchability now (guarded by tests/test_settings_env_mapping.py).
    # Range constraints mirror the Settings fields so bad values 422 at the
    # request; the applier's Settings-field validation is the backstop.
    nymeria_public_url: Optional[str] = None
    nymeria_data_dir: Optional[str] = None
    redis_url: Optional[str] = None
    redis_enabled: Optional[bool] = None
    postgres_uri: Optional[str] = None
    lock_timeout: Optional[int] = Field(default=None, ge=30, le=600)
    max_concurrent_autonomous: Optional[int] = Field(default=None, ge=0)
    ticker_poll_interval: Optional[int] = Field(default=None, ge=1, le=60)
    todo_auto_archive_days: Optional[int] = Field(default=None, ge=1, le=30)

    # Chat-platform credentials and knobs, same provenance as the block above
    # (advertised, previously unpatchable). Bot processes read most of these
    # at their own boot; the token fields are in the restart-required table.
    discord_bot_token: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_default_chat_id: Optional[str] = None
    twitch_bot_access_token: Optional[str] = None
    twitch_bot_refresh_token: Optional[str] = None
    twitch_bot_user_id: Optional[str] = None
    twitch_broadcaster_refresh_token: Optional[str] = None
    twitch_broadcaster_token: Optional[str] = None
    twitch_buffer_size: Optional[int] = Field(default=None, ge=50, le=5000)
    twitch_channel: Optional[str] = None
    twitch_client_id: Optional[str] = None
    twitch_client_secret: Optional[str] = None
    twitch_pulse_enabled: Optional[bool] = None
    twitch_pulse_interval: Optional[int] = Field(default=None, ge=60, le=3600)
    twitch_respond_mode: Optional[str] = None
    twitch_system_prompt: Optional[str] = Field(default=None, max_length=50000)

    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_fast_model: Optional[str] = None
    llm_smart_model: Optional[str] = None
    llm_background_model: Optional[str] = None
    llm_background_base_url: Optional[str] = None
    llm_fallback_models: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[ReasoningEffortName] = None
    llm_extended_thinking: Optional[bool] = None
    dynamic_tool_binding: Optional[bool] = None
    sequential_tool_execution: Optional[bool] = None
    hooks_enabled: Optional[bool] = None
    llm_use_model_defaults: Optional[bool] = None
    llm_base_url: Optional[str] = None
    llm_context_length: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)
    llm_ollama_num_ctx: Optional[int] = Field(default=None, ge=1_000, le=2_000_000)
    llm_provider_route: Optional[ProviderRoute] = None
    openai_api_mode: Optional[OpenAIApiMode] = None
    cliproxy_management_url: Optional[str] = None
    # Accepted by PATCH /settings only. Secret values are intentionally absent
    # from ServerSettingsResponse.
    cliproxy_management_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    anthropic_direct_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    # Generic write-only key slot: routed by the applier to the FIRST declared
    # api_key_env_var of the provider being set (updates.llm_provider, falling
    # back to the current settings.llm_provider), mirroring how the CLI wizard
    # finalize writes `spec.api_key_env_vars[0]`. This is what lets the GUI
    # configure the long-tail registry providers (groq, xai, deepseek, ...)
    # whose keys have no dedicated field here. Declared in
    # VIRTUAL_UPDATE_FIELDS because it has no fixed env mapping of its own.
    llm_api_key: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimensions: Optional[int] = None
    embedding_base_url: Optional[str] = None
    embedding_input_type: Optional[str] = None
    rag_retrieval_mode: Optional[str] = None
    rag_embed_tool_results: Optional[bool] = None
    rag_rerank_enabled: Optional[bool] = None
    rag_rerank_provider: Optional[str] = None
    rag_rerank_model: Optional[str] = None
    rag_rerank_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_extraction_model: Optional[str] = None
    perplexity_api_key: Optional[str] = None
    perplexity_search_model: Optional[str] = None
    groq_api_key: Optional[str] = None
    # Capability-backend keys the CLI wizard's backend_keys step writes
    # (web search / fetch / image generation), exposed here so GUI onboarding
    # can store them through the same PATCH path. Write-only like every key.
    tavily_api_key: Optional[str] = None
    exa_api_key: Optional[str] = None
    firecrawl_api_key: Optional[str] = None
    brave_api_key: Optional[str] = None
    replicate_api_key: Optional[str] = None
    fal_api_key: Optional[str] = None
    bfl_api_key: Optional[str] = None
    wolfram_alpha_app_id: Optional[str] = None
    searxng_base_url: Optional[str] = None
    nasa_api_key: Optional[str] = None
    openweathermap_api_key: Optional[str] = None
    npm_registry_url: Optional[str] = None
    graphql_endpoint: Optional[str] = None
    graphql_bearer_token: Optional[str] = None
    graphql_api_key: Optional[str] = None
    graphql_api_key_header: Optional[str] = None
    graphql_headers_json: Optional[str] = None
    totp_secret: Optional[str] = None
    crypto_hmac_secret: Optional[str] = None
    crypto_sign_private_key: Optional[str] = None
    crypto_sign_private_key_passphrase: Optional[str] = None
    jwt_secret: Optional[str] = None
    jwt_private_key: Optional[str] = None
    jwt_public_key: Optional[str] = None
    jwt_algorithm: Optional[str] = None
    github_token: Optional[str] = None
    github_api_base_url: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_base_url: Optional[str] = None
    circleci_api_token: Optional[str] = None
    circleci_base_url: Optional[str] = None
    travisci_api_token: Optional[str] = None
    travisci_base_url: Optional[str] = None
    jenkins_base_url: Optional[str] = None
    jenkins_username: Optional[str] = None
    jenkins_api_token: Optional[str] = None
    dropbox_access_token: Optional[str] = None
    dropbox_api_base_url: Optional[str] = None
    dropbox_content_base_url: Optional[str] = None
    nextcloud_webdav_url: Optional[str] = None
    nextcloud_username: Optional[str] = None
    nextcloud_password: Optional[str] = None
    nextcloud_access_token: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_session_token: Optional[str] = None
    s3_region: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_force_path_style: Optional[bool] = None
    clearbit_api_key: Optional[str] = None
    clearbit_company_base_url: Optional[str] = None
    clearbit_person_base_url: Optional[str] = None
    clearbit_autocomplete_base_url: Optional[str] = None
    uplead_api_key: Optional[str] = None
    uplead_base_url: Optional[str] = None
    dropcontact_api_key: Optional[str] = None
    dropcontact_base_url: Optional[str] = None
    humantic_api_key: Optional[str] = None
    humantic_base_url: Optional[str] = None
    lonescale_api_key: Optional[str] = None
    lonescale_base_url: Optional[str] = None
    uproc_email: Optional[str] = None
    uproc_api_key: Optional[str] = None
    uproc_base_url: Optional[str] = None
    bitly_token: Optional[str] = None
    bitly_base_url: Optional[str] = None
    brandfetch_api_key: Optional[str] = None
    brandfetch_base_url: Optional[str] = None
    marketstack_api_key: Optional[str] = None
    marketstack_base_url: Optional[str] = None
    deepl_api_key: Optional[str] = None
    deepl_api_plan: Optional[str] = None
    deepl_base_url: Optional[str] = None
    lingvanex_api_key: Optional[str] = None
    lingvanex_base_url: Optional[str] = None
    apitemplate_api_key: Optional[str] = None
    apitemplate_base_url: Optional[str] = None
    onesimple_api_token: Optional[str] = None
    onesimple_base_url: Optional[str] = None
    dhl_api_key: Optional[str] = None
    dhl_base_url: Optional[str] = None
    onfleet_api_key: Optional[str] = None
    onfleet_base_url: Optional[str] = None
    phantombuster_api_key: Optional[str] = None
    phantombuster_base_url: Optional[str] = None
    erpnext_api_key: Optional[str] = None
    erpnext_api_secret: Optional[str] = None
    erpnext_base_url: Optional[str] = None
    erpnext_subdomain: Optional[str] = None
    erpnext_cloud_domain: Optional[str] = None
    odoo_url: Optional[str] = None
    odoo_username: Optional[str] = None
    odoo_password: Optional[str] = None
    odoo_database: Optional[str] = None
    invoiceninja_api_token: Optional[str] = None
    invoiceninja_secret: Optional[str] = None
    invoiceninja_base_url: Optional[str] = None
    invoiceninja_api_version: Optional[str] = None
    demio_api_key: Optional[str] = None
    demio_api_secret: Optional[str] = None
    demio_base_url: Optional[str] = None
    zoom_access_token: Optional[str] = None
    zoom_base_url: Optional[str] = None
    gotowebinar_access_token: Optional[str] = None
    gotowebinar_account_key: Optional[str] = None
    gotowebinar_organizer_key: Optional[str] = None
    gotowebinar_base_url: Optional[str] = None
    todoist_api_key: Optional[str] = None
    todoist_base_url: Optional[str] = None
    trello_api_key: Optional[str] = None
    trello_api_token: Optional[str] = None
    trello_base_url: Optional[str] = None
    raindrop_access_token: Optional[str] = None
    raindrop_base_url: Optional[str] = None
    yourls_url: Optional[str] = None
    yourls_signature: Optional[str] = None
    yourls_username: Optional[str] = None
    yourls_password: Optional[str] = None
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
    monday_api_token: Optional[str] = None
    monday_api_url: Optional[str] = None
    taiga_auth_token: Optional[str] = None
    taiga_username: Optional[str] = None
    taiga_password: Optional[str] = None
    taiga_base_url: Optional[str] = None
    wekan_base_url: Optional[str] = None
    wekan_token: Optional[str] = None
    wekan_username: Optional[str] = None
    wekan_password: Optional[str] = None
    slack_bot_token: Optional[str] = None
    slack_access_token: Optional[str] = None
    slack_base_url: Optional[str] = None
    microsoft_graph_access_token: Optional[str] = None
    microsoft_graph_base_url: Optional[str] = None
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
    mautic_base_url: Optional[str] = None
    mautic_access_token: Optional[str] = None
    mautic_username: Optional[str] = None
    mautic_password: Optional[str] = None
    freshdesk_api_key: Optional[str] = None
    freshdesk_domain: Optional[str] = None
    freshdesk_base_url: Optional[str] = None
    helpscout_access_token: Optional[str] = None
    helpscout_base_url: Optional[str] = None
    intercom_access_token: Optional[str] = None
    intercom_base_url: Optional[str] = None
    intercom_version: Optional[str] = None
    drift_access_token: Optional[str] = None
    drift_base_url: Optional[str] = None
    salesforce_instance_url: Optional[str] = None
    salesforce_access_token: Optional[str] = None
    salesforce_base_url: Optional[str] = None
    salesforce_api_version: Optional[str] = None
    zoho_crm_access_token: Optional[str] = None
    zoho_crm_api_domain: Optional[str] = None
    zoho_crm_base_url: Optional[str] = None
    freshworks_crm_api_key: Optional[str] = None
    freshworks_crm_domain: Optional[str] = None
    freshworks_crm_base_url: Optional[str] = None
    salesmate_session_token: Optional[str] = None
    salesmate_link_name: Optional[str] = None
    salesmate_base_url: Optional[str] = None
    pipedrive_api_token: Optional[str] = None
    pipedrive_access_token: Optional[str] = None
    pipedrive_base_url: Optional[str] = None
    copper_api_key: Optional[str] = None
    copper_email: Optional[str] = None
    copper_base_url: Optional[str] = None
    agilecrm_email: Optional[str] = None
    agilecrm_api_key: Optional[str] = None
    agilecrm_subdomain: Optional[str] = None
    agilecrm_base_url: Optional[str] = None
    monica_access_token: Optional[str] = None
    monica_base_url: Optional[str] = None
    affinity_api_key: Optional[str] = None
    affinity_base_url: Optional[str] = None
    keap_access_token: Optional[str] = None
    keap_base_url: Optional[str] = None
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
    plivo_auth_id: Optional[str] = None
    plivo_auth_token: Optional[str] = None
    plivo_base_url: Optional[str] = None
    vonage_api_key: Optional[str] = None
    vonage_api_secret: Optional[str] = None
    vonage_base_url: Optional[str] = None
    seven_api_key: Optional[str] = None
    seven_base_url: Optional[str] = None
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
    paddle_vendor_id: Optional[str] = None
    paddle_vendor_auth_code: Optional[str] = None
    paddle_sandbox: Optional[bool] = None
    paddle_base_url: Optional[str] = None
    profitwell_api_token: Optional[str] = None
    profitwell_base_url: Optional[str] = None
    tapfiliate_api_key: Optional[str] = None
    tapfiliate_base_url: Optional[str] = None
    magento_host: Optional[str] = None
    magento_base_url: Optional[str] = None
    magento_access_token: Optional[str] = None
    unleashed_api_id: Optional[str] = None
    unleashed_api_key: Optional[str] = None
    unleashed_base_url: Optional[str] = None
    quickbooks_access_token: Optional[str] = None
    quickbooks_realm_id: Optional[str] = None
    quickbooks_environment: Optional[str] = None
    quickbooks_base_url: Optional[str] = None
    xero_access_token: Optional[str] = None
    xero_tenant_id: Optional[str] = None
    xero_base_url: Optional[str] = None
    xero_connections_url: Optional[str] = None
    pushbullet_access_token: Optional[str] = None
    pushbullet_base_url: Optional[str] = None
    pushcut_api_key: Optional[str] = None
    pushcut_base_url: Optional[str] = None
    gotify_base_url: Optional[str] = None
    gotify_app_token: Optional[str] = None
    gotify_client_token: Optional[str] = None
    pushover_api_token: Optional[str] = None
    pushover_user_key: Optional[str] = None
    pushover_base_url: Optional[str] = None
    signl4_team_secret: Optional[str] = None
    signl4_webhook_url: Optional[str] = None
    signl4_base_url: Optional[str] = None
    wordpress_url: Optional[str] = None
    wordpress_username: Optional[str] = None
    wordpress_password: Optional[str] = None
    strapi_url: Optional[str] = None
    strapi_api_token: Optional[str] = None
    strapi_email: Optional[str] = None
    strapi_password: Optional[str] = None
    strapi_api_version: Optional[str] = None
    contentful_space_id: Optional[str] = None
    contentful_delivery_token: Optional[str] = None
    contentful_preview_token: Optional[str] = None
    contentful_base_url: Optional[str] = None
    contentful_preview_base_url: Optional[str] = None
    ghost_url: Optional[str] = None
    ghost_content_api_key: Optional[str] = None
    ghost_admin_api_key: Optional[str] = None
    ghost_api_version: Optional[str] = None
    storyblok_content_token: Optional[str] = None
    storyblok_management_token: Optional[str] = None
    storyblok_space_id: Optional[str] = None
    storyblok_content_base_url: Optional[str] = None
    storyblok_management_base_url: Optional[str] = None
    webflow_access_token: Optional[str] = None
    webflow_base_url: Optional[str] = None
    netlify_access_token: Optional[str] = None
    netlify_base_url: Optional[str] = None
    rundeck_base_url: Optional[str] = None
    rundeck_token: Optional[str] = None
    uptimerobot_api_key: Optional[str] = None
    uptimerobot_base_url: Optional[str] = None
    pagerduty_api_token: Optional[str] = None
    pagerduty_from_email: Optional[str] = None
    pagerduty_base_url: Optional[str] = None
    sentry_auth_token: Optional[str] = None
    sentry_base_url: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    cloudflare_base_url: Optional[str] = None
    grafana_api_token: Optional[str] = None
    grafana_base_url: Optional[str] = None
    metabase_base_url: Optional[str] = None
    metabase_session_token: Optional[str] = None
    metabase_api_key: Optional[str] = None
    metabase_username: Optional[str] = None
    metabase_password: Optional[str] = None
    elasticsearch_base_url: Optional[str] = None
    elasticsearch_api_key: Optional[str] = None
    elasticsearch_bearer_token: Optional[str] = None
    elasticsearch_username: Optional[str] = None
    elasticsearch_password: Optional[str] = None
    elasticsearch_ignore_ssl_issues: Optional[bool] = None
    splunk_base_url: Optional[str] = None
    splunk_auth_token: Optional[str] = None
    splunk_allow_unauthorized_certs: Optional[bool] = None
    urlscan_api_key: Optional[str] = None
    urlscan_base_url: Optional[str] = None
    hunter_api_key: Optional[str] = None
    hunter_base_url: Optional[str] = None
    mailcheck_api_key: Optional[str] = None
    mailcheck_base_url: Optional[str] = None
    peekalink_api_key: Optional[str] = None
    peekalink_base_url: Optional[str] = None
    jina_api_key: Optional[str] = None
    jina_reader_base_url: Optional[str] = None
    jina_search_base_url: Optional[str] = None
    jina_deepsearch_base_url: Optional[str] = None
    misp_base_url: Optional[str] = None
    misp_api_key: Optional[str] = None
    misp_allow_unauthorized_certs: Optional[bool] = None
    thehive_base_url: Optional[str] = None
    thehive_api_key: Optional[str] = None
    thehive_api_version: Optional[str] = None
    thehive_allow_unauthorized_certs: Optional[bool] = None
    securityscorecard_api_key: Optional[str] = None
    securityscorecard_base_url: Optional[str] = None
    okta_access_token: Optional[str] = None
    okta_domain: Optional[str] = None
    okta_base_url: Optional[str] = None
    elastic_security_base_url: Optional[str] = None
    elastic_security_api_key: Optional[str] = None
    elastic_security_username: Optional[str] = None
    elastic_security_password: Optional[str] = None
    baserow_api_token: Optional[str] = None
    baserow_base_url: Optional[str] = None
    nocodb_api_token: Optional[str] = None
    nocodb_base_url: Optional[str] = None
    nocodb_auth_header: Optional[str] = None
    coda_api_token: Optional[str] = None
    coda_base_url: Optional[str] = None
    grist_api_key: Optional[str] = None
    grist_base_url: Optional[str] = None
    adalo_api_key: Optional[str] = None
    adalo_app_id: Optional[str] = None
    adalo_base_url: Optional[str] = None
    bubble_api_token: Optional[str] = None
    bubble_app_name: Optional[str] = None
    bubble_environment: Optional[str] = None
    bubble_domain: Optional[str] = None
    bubble_base_url: Optional[str] = None
    cockpit_base_url: Optional[str] = None
    cockpit_access_token: Optional[str] = None
    kobotoolbox_api_token: Optional[str] = None
    kobotoolbox_base_url: Optional[str] = None
    telegram_api_base_url: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_base_url: Optional[str] = None
    whatsapp_webhook_verify_token: Optional[str] = None
    whatsapp_app_secret: Optional[str] = None
    whatsapp_show_tool_events: Optional[bool] = None
    discord_base_url: Optional[str] = None
    teams_bot_app_id: Optional[str] = None
    teams_bot_app_password: Optional[str] = None
    teams_bot_tenant_id: Optional[str] = None
    teams_bot_respond_mode: Optional[str] = None
    teams_bot_validate_auth: Optional[bool] = None
    teams_bot_show_tool_events: Optional[bool] = None
    teams_bot_token_url: Optional[str] = None
    teams_bot_openid_config_url: Optional[str] = None
    google_books_api_key: Optional[str] = None
    google_books_base_url: Optional[str] = None
    youtube_api_key: Optional[str] = None
    youtube_base_url: Optional[str] = None
    spotify_access_token: Optional[str] = None
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    spotify_base_url: Optional[str] = None
    spotify_accounts_base_url: Optional[str] = None
    reddit_access_token: Optional[str] = None
    reddit_refresh_token: Optional[str] = None
    reddit_client_id: Optional[str] = None
    reddit_client_secret: Optional[str] = None
    reddit_base_url: Optional[str] = None
    reddit_public_base_url: Optional[str] = None
    reddit_token_url: Optional[str] = None
    discourse_api_key: Optional[str] = None
    discourse_api_username: Optional[str] = None
    discourse_base_url: Optional[str] = None
    medium_access_token: Optional[str] = None
    medium_base_url: Optional[str] = None
    bamboohr_api_key: Optional[str] = None
    bamboohr_subdomain: Optional[str] = None
    bamboohr_base_url: Optional[str] = None
    beeminder_access_token: Optional[str] = None
    beeminder_base_url: Optional[str] = None
    clockify_api_key: Optional[str] = None
    clockify_base_url: Optional[str] = None
    harvest_access_token: Optional[str] = None
    harvest_account_id: Optional[str] = None
    harvest_base_url: Optional[str] = None
    oura_access_token: Optional[str] = None
    oura_base_url: Optional[str] = None
    strava_access_token: Optional[str] = None
    strava_base_url: Optional[str] = None
    homeassistant_access_token: Optional[str] = None
    homeassistant_base_url: Optional[str] = None
    philips_hue_access_token: Optional[str] = None
    philips_hue_username: Optional[str] = None
    philips_hue_base_url: Optional[str] = None
    activecampaign_api_key: Optional[str] = None
    activecampaign_base_url: Optional[str] = None
    convertkit_api_secret: Optional[str] = None
    convertkit_base_url: Optional[str] = None
    getresponse_api_key: Optional[str] = None
    getresponse_base_url: Optional[str] = None
    mailerlite_api_key: Optional[str] = None
    mailerlite_base_url: Optional[str] = None
    mailerlite_classic_api: Optional[bool] = None
    customerio_tracking_site_id: Optional[str] = None
    customerio_tracking_api_key: Optional[str] = None
    customerio_app_api_key: Optional[str] = None
    customerio_region: Optional[str] = None
    customerio_tracking_base_url: Optional[str] = None
    customerio_app_base_url: Optional[str] = None
    iterable_api_key: Optional[str] = None
    iterable_base_url: Optional[str] = None
    posthog_api_key: Optional[str] = None
    posthog_base_url: Optional[str] = None
    segment_write_key: Optional[str] = None
    segment_base_url: Optional[str] = None
    actionnetwork_api_key: Optional[str] = None
    actionnetwork_base_url: Optional[str] = None
    autopilot_api_key: Optional[str] = None
    autopilot_base_url: Optional[str] = None
    egoi_api_key: Optional[str] = None
    egoi_base_url: Optional[str] = None
    vero_auth_token: Optional[str] = None
    vero_base_url: Optional[str] = None
    lemlist_api_key: Optional[str] = None
    lemlist_base_url: Optional[str] = None
    sendy_url: Optional[str] = None
    sendy_base_url: Optional[str] = None
    sendy_api_key: Optional[str] = None
    emelia_api_key: Optional[str] = None
    emelia_graphql_url: Optional[str] = None
    llm_stream_max_retries: Optional[int] = None
    llm_stream_retry_initial_delay: Optional[float] = None
    llm_stream_retry_max_delay: Optional[float] = None
    llm_fallback_hold_seconds: Optional[int] = Field(default=None, ge=0, le=604800)
    llm_fallback_switch_mode: Optional[Literal["auto", "ask"]] = None
    llm_fallback_prompt_timeout_seconds: Optional[int] = Field(
        default=None, ge=10, le=600
    )
    llm_refusal_swap_mode: Optional[Literal["off", "ask", "auto"]] = None
    context_management: Optional[str] = None
    compact_threshold: Optional[float] = Field(default=None, ge=0.05, le=0.95)
    compact_threshold_mode: Optional[Literal["percentage", "tokens"]] = None
    compact_threshold_tokens: Optional[int] = Field(
        default=None,
        ge=1_000,
        le=2_000_000,
    )
    compact_keep_messages: Optional[int] = None
    compact_model: Optional[str] = None
    compact_proactive_enabled: Optional[bool] = None
    compact_proactive_idle_seconds: Optional[int] = Field(
        default=None, ge=30, le=3600
    )
    compact_proactive_min_pct: Optional[int] = Field(default=None, ge=10, le=100)
    sliding_window_cycles: Optional[int] = None
    tool_output_max_chars: Optional[int] = None
    # Bounds mirror the CLI wizard's tuning catalog (setup/tuning_catalog.py).
    tool_timeout: Optional[int] = Field(default=None, ge=30, le=900)
    nymeria_mcp_chat_wait_seconds: Optional[int] = Field(
        default=None, ge=1, le=3600
    )
    tool_timing_in_results: Optional[bool] = None
    memory_char_limit: Optional[int] = Field(default=None, ge=1, le=2_000_000)
    memory_max_entries: Optional[int] = Field(default=None, ge=1, le=10_000)
    memory_value_max_chars: Optional[int] = Field(default=None, ge=50, le=100_000)
    agent_max_iterations: Optional[int] = Field(default=None, ge=10, le=10_000)
    user_timezone: Optional[str] = None
    log_level: Optional[str] = None
    watchdog_enabled: Optional[bool] = None
    watchdog_interval_minutes: Optional[int] = None
    todo_staleness_minutes: Optional[int] = None
    activity_retention_hours: Optional[int] = None
    max_concurrent_interactive: Optional[int] = Field(default=None, ge=0)
    interactive_admission_wait_seconds: Optional[float] = Field(
        default=None, ge=0.0, le=120.0
    )
    dream_default_min_interval_hours: Optional[int] = Field(default=None, ge=1, le=168)
    dream_default_min_idle_minutes: Optional[int] = Field(default=None, ge=5, le=10080)
    dream_default_min_turns_since_last: Optional[int] = Field(default=None, ge=1, le=10000)
    dream_default_model: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_base_url: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_output_format: Optional[str] = None
    tts_speed: Optional[float] = None
    tts_api_key: Optional[str] = None
    stt_provider: Optional[str] = None
    stt_base_url: Optional[str] = None
    stt_model: Optional[str] = None
    stt_language: Optional[str] = None
    stt_api_key: Optional[str] = None
    voice_default_thread_id: Optional[str] = None

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value):
        """Treat empty/blank strings as None (unset) and normalize case."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized or None
        return value

    @field_validator("user_timezone")
    @classmethod
    def _validate_user_timezone(cls, value):
        """Reject unknown IANA names; blank means unset (same check the CLI uses)."""
        if value is None:
            return value
        text = value.strip()
        if not text:
            return None
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(text)
        except Exception as exc:  # noqa: BLE001 (ZoneInfoNotFoundError, ValueError, ...)
            raise ValueError(f"unknown IANA timezone: {text!r}") from exc
        return text


# The settings-field -> dotenv-var override table (only the S3 credentials diverge from
# `field.upper()`; see `FIELD_ENV_OVERRIDES`'s docstring for why). Aliased to the shared
# constant that `Settings` also builds its aliases from, so the write surface here cannot
# drift from the model's read names (guarded by `tests/test_settings_env_mapping.py`).
# Imported from the top-level `_env_overrides` leaf, not `config.settings`, to keep this
# schema module import-light.
_ENV_VAR_OVERRIDES = FIELD_ENV_OVERRIDES

# Patchable fields with NO fixed env var of their own: the applier routes each
# one dynamically instead of writing `field.upper()`. Currently only the generic
# `llm_api_key` slot, which lands in the selected provider's declared key var
# (`spec.api_key_env_vars[0]`). Excluded from `server_settings_env_mapping()`
# and carved out of the drift guards in `tests/test_settings_env_mapping.py`.
VIRTUAL_UPDATE_FIELDS = frozenset({"llm_api_key"})


def server_settings_env_mapping() -> dict[str, str]:
    """Return the settings-field -> dotenv-var map for every patchable setting.

    Derived from `ServerSettingsUpdate` (the patchable surface) so it cannot drift:
    a new update field automatically gets a mapping entry. Each field maps to
    `field.upper()` unless listed in `_ENV_VAR_OVERRIDES`. Fields in
    `VIRTUAL_UPDATE_FIELDS` are skipped: they are routed dynamically by the
    applier rather than written to one fixed var. The `Settings` model
    carries matching `validation_alias`es for the override fields, so the env var
    written here is the same one the field reads back (see the round-trip guard in
    `tests/test_settings_env_mapping.py`).
    """
    return {
        name: _ENV_VAR_OVERRIDES.get(name, name.upper())
        for name in ServerSettingsUpdate.model_fields
        if name not in VIRTUAL_UPDATE_FIELDS
    }


# Agent-actor write policy for settings (#157, vault posture). Consumed by the
# command layer, the in-turn agent path to settings writes (the MCP and REST
# settings surfaces are human-client trust by scope, not mechanism: they carry
# no actor concept, and SECURITY.md 2.2 owns everything past the process
# boundary). Both lists are ratcheted as subsets of the patchable fields in
# tests/test_settings_env_mapping.py.
#
# ALERT: agent-issued writes to these platform-level, URL/egress-shaped keys
# succeed but fire a send_owner_alert (loud + reversible, never blocking).
# Integration-level base_urls are deliberately excluded: their egress is
# policy-controlled at consumption (the service-integration egress gates).
AGENT_WRITE_ALERT_SETTINGS = frozenset({
    "nymeria_public_url",
    "llm_base_url",
    "llm_background_base_url",
    "embedding_base_url",
    "cliproxy_management_url",
    "cliproxy_management_key",
})

# BLOCKED: the gating machinery must not be removable by the thing it gates
# (the analogue of system credentials staying off the agent's auth_write
# surface). Human admins are unaffected on every surface.
AGENT_WRITE_BLOCKED_SETTINGS = frozenset({
    "hooks_enabled",
})


def resolve_settings_field_name(key: str) -> str:
    """Resolve a user-typed settings key to its field name, best effort.

    The ONE spelling rule for the whole config surface (`/env set`,
    `/settings set`, `GET /settings/env/{key}` all use it): an exact env-var
    name resolves through the reverse of `server_settings_env_mapping()` so
    the divergent spellings (the S3 family: AWS_ACCESS_KEY_ID ->
    s3_access_key_id) land on the right field, everything else case-folds.
    Unknown keys pass through folded; the callers' own unknown-key handling
    (the applier's 400, the env reader's 404) stays authoritative.
    """
    reverse = {v: k for k, v in server_settings_env_mapping().items()}
    return reverse.get(key.upper(), key.lower())


class LLMProviderTestRequest(BaseModel):
    """Request model for testing an arbitrary provider configuration."""

    model_config = ConfigDict(extra="forbid")

    llm_provider: LLMProviderName
    llm_model: str = Field(min_length=1)
    api_key: Optional[SecretStr] = None
    llm_base_url: Optional[str] = None
    provider_route: Optional[ProviderRoute] = None
    openai_api_mode: Optional[OpenAIApiMode] = "chat_completions"

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


class AvailableModelsRequest(BaseModel):
    """Request body for listing models with an ephemeral credential.

    The POST variant of /models/available: the pasted key rides the request
    body (never a query string, so it stays out of URLs and access logs) and
    is used for that one listing only, nothing is persisted.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[SecretStr] = None

    @field_validator("base_url")
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
    provider_route: Optional[ProviderRoute] = None
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
    # Resolved adapter route at the time of the diagnostic. Populated when the
    # active LLMConfig has provider_route set; left None for legacy callers
    # that do not carry one.
    provider_route: Optional[ProviderRoute] = None
    llm_max_tokens: Optional[int] = None
    effective_max_tokens: Optional[int] = None
    source_env_files: list[str] = []
    openrouter: Optional[OpenRouterKeyDiagnostics] = None
