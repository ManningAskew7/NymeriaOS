"""Drift guards for the schema-derived settings env mapping.

The settings-field -> dotenv-var map (`server_settings_env_mapping`) is derived from
`ServerSettingsUpdate` plus a tiny `_ENV_VAR_OVERRIDES` table, and the `Settings` model
carries matching `validation_alias`es for the override fields. These tests pin the three
together so a future divergence (a new field whose write env name differs from the name
the model reads it back from, i.e. the S3 bug class) fails loudly here instead of
silently dropping or orphaning a setting at runtime.

Pure and fast: only class-level `model_fields` are read, so nothing loads the env.
"""

from __future__ import annotations

from pydantic import AliasChoices

from nymeria._env_overrides import FIELD_ENV_OVERRIDES
from nymeria.api.schemas.settings import (
    VIRTUAL_UPDATE_FIELDS,
    ServerSettingsUpdate,
    _ENV_VAR_OVERRIDES,
    server_settings_env_mapping,
)
from nymeria.config.settings import Settings


def _model_read_env_name(field_name: str) -> str:
    """The dotenv var pydantic-settings actually loads ``field_name`` from."""
    alias = Settings.model_fields[field_name].validation_alias
    if isinstance(alias, AliasChoices):
        # The first choice is the canonical name PATCH /settings writes to.
        return alias.choices[0]
    if isinstance(alias, str):
        return alias
    # No alias: pydantic-settings matches the (case-insensitive) field name.
    return field_name.upper()


def test_mapping_covers_every_update_field():
    # Virtual fields (llm_api_key) are routed dynamically by the applier, never
    # written to one fixed var, so they are the one sanctioned mapping gap.
    mapping = server_settings_env_mapping()
    missing = set(ServerSettingsUpdate.model_fields) - set(mapping) - VIRTUAL_UPDATE_FIELDS
    assert not missing, f"update fields with no env mapping: {sorted(missing)}"


def test_virtual_fields_are_real_update_fields_and_stay_out_of_the_mapping():
    # The virtual set must name real patchable fields (else it is dead config)
    # and the mapping must skip them (else field.upper() would write a bogus
    # LLM_API_KEY line nothing reads).
    mapping = server_settings_env_mapping()
    for name in VIRTUAL_UPDATE_FIELDS:
        assert name in ServerSettingsUpdate.model_fields, name
        assert name not in mapping, name


def test_every_update_field_exists_on_settings_model():
    # A patchable field with no Settings field would write a dotenv line nothing
    # reads. Virtual fields are exempt: they never write their own var at all.
    missing = (
        set(ServerSettingsUpdate.model_fields)
        - set(Settings.model_fields)
        - VIRTUAL_UPDATE_FIELDS
    )
    assert not missing, f"patchable fields absent from Settings model: {sorted(missing)}"


def test_non_override_fields_map_to_upper():
    mapping = server_settings_env_mapping()
    for name, env_var in mapping.items():
        if name not in _ENV_VAR_OVERRIDES:
            assert env_var == name.upper(), name


def test_overrides_are_real_update_fields():
    for name in _ENV_VAR_OVERRIDES:
        assert name in ServerSettingsUpdate.model_fields, name


def test_s3_overrides_are_the_only_divergence():
    mapping = server_settings_env_mapping()
    diverging = {name for name, env_var in mapping.items() if env_var != name.upper()}
    assert diverging == set(_ENV_VAR_OVERRIDES)


def test_write_env_name_matches_model_read_name():
    # The crown-jewel guard: the var the mapping WRITES for a field must equal the var
    # the Settings model READS that field from. This is exactly what the S3 fields
    # violated (wrote AWS_*, read S3_*) before their validation_aliases were added.
    mapping = server_settings_env_mapping()
    mismatches = {
        name: (env_var, _model_read_env_name(name))
        for name, env_var in mapping.items()
        if name in Settings.model_fields and env_var != _model_read_env_name(name)
    }
    assert not mismatches, f"write/read env name divergence: {mismatches}"


def test_schema_overrides_are_the_shared_constant():
    # The schema's override table is the one shared `FIELD_ENV_OVERRIDES` source, not a
    # private copy, so the write surface cannot drift from the model's read aliases.
    assert _ENV_VAR_OVERRIDES is FIELD_ENV_OVERRIDES


def test_settings_aliases_built_from_shared_constant():
    # The `Settings` model builds each diverging field's primary AliasChoices entry from
    # the same shared table, so a single edit to `FIELD_ENV_OVERRIDES` moves both the
    # model read name and the API write name together.
    for field_name, env_var in FIELD_ENV_OVERRIDES.items():
        alias = Settings.model_fields[field_name].validation_alias
        assert isinstance(alias, AliasChoices), field_name
        assert alias.choices[0] == env_var, (field_name, alias.choices)


# ── Visibility/patchability coherence (2026-08-10 outage pass) ──────────────


def _visible_env_fields() -> set[str]:
    from nymeria.api.routers.settings import _ENV_CATEGORIES

    return {name for fields in _ENV_CATEGORIES.values() for name in fields}


def test_every_advertised_env_key_is_patchable():
    """VISIBLE implies SETTABLE: /env show must never advertise a key the
    update model cannot write. Thirty advertised keys (redis_url,
    discord_bot_token, the twitch family, ...) were missing from
    ServerSettingsUpdate, so /env set on them reported success and wrote
    nothing."""
    unsettable = _visible_env_fields() - set(ServerSettingsUpdate.model_fields)
    assert not unsettable, f"/env show advertises unsettable keys: {sorted(unsettable)}"


def test_restart_required_and_clearable_registries_are_patchable():
    """The applier's side tables only fire for patchable fields; an entry for
    an unpatchable field is dead configuration."""
    from nymeria.api.routers.settings import (
        _CLEARABLE_NULL_SETTINGS,
        _RESTART_REQUIRED_KEYS,
    )

    patchable = set(ServerSettingsUpdate.model_fields)
    assert _RESTART_REQUIRED_KEYS <= patchable, sorted(
        _RESTART_REQUIRED_KEYS - patchable
    )
    assert set(_CLEARABLE_NULL_SETTINGS) <= patchable, sorted(
        set(_CLEARABLE_NULL_SETTINGS) - patchable
    )


def test_watchdog_keys_are_restart_required():
    """The watchdog sweep captures all three at construction (its own
    __init__ for the staleness window, the Ticker for the interval and the
    enable flag), and the applier's hot reload never rebinds them. Without
    the flag a PATCH answers "applied, restart_required: false" while the
    running sweep keeps the old value; in Docker the PATCH lands in the API
    container and the sweep runs in the worker, so only a restart applies
    it."""
    from nymeria.api.routers.settings import _RESTART_REQUIRED_KEYS

    watchdog_keys = {
        "todo_staleness_minutes",
        "watchdog_interval_minutes",
        "watchdog_enabled",
    }
    assert watchdog_keys <= _RESTART_REQUIRED_KEYS, sorted(
        watchdog_keys - _RESTART_REQUIRED_KEYS
    )


# Settings fields deliberately UNREACHABLE through the whole config surface:
# not in an /env show category, not in ServerSettingsUpdate, not hidden. Each
# is a boot-time/infra value, an internal knob, or an integration credential
# that never joined the update model. Shrink-only: make a field reachable
# (add it to _ENV_CATEGORIES and/or ServerSettingsUpdate) and DELETE its line;
# never add a line without deciding the field should stay unreachable.
_UNREACHABLE_SETTINGS_EXEMPT = frozenset({
    "account_bootstrap_token_ttl_hours", "account_max_active_tokens_per_user",
    "account_token_ttl_days", "allow_unbound_tool_calls",
    "api_host", "api_port", "audit_log_enabled", "bash_env_passthrough",
    "checkpoint_executor_max_workers", "cors_origins", "database_backend",
    "default_executor_max_workers",
    "discord_default_account", "discord_default_account_guilds",
    "discord_mode", "discord_reaction_trigger_enabled", "discord_respond_mode",
    "exec_sandbox_enabled",
    "facebook_access_token", "facebook_app_secret", "facebook_graph_base_url",
    "fcm_credentials_json",
    "fcm_enabled", "freshservice_api_key", "freshservice_base_url",
    "freshservice_domain",
    # harness_report_*: deployment wiring (a mounted intake dir, an email
    # destination, an instance label), env-only like nymeria_error_report_email;
    # a runtime PATCH cannot conjure the mount the dir setting names.
    "harness_report_dir", "harness_report_email",
    "harness_report_instance_label",
    "hooks_run_command_enabled", "http_allow_https_to_http_redirect",
    "http_domain_allowlist", "http_domain_blocklist",
    "http_internal_allowlist", "http_max_redirects", "linkedin_access_token",
    "linkedin_api_version",
    "linkedin_base_url", "mcp_registry_url", "nymeria_allow_self_edit",
    "nymeria_allow_unsandboxed_mcp_install",
    "nymeria_api_docs", "nymeria_claude_code_allowed_models",
    "nymeria_claude_code_bare", "nymeria_claude_code_block_seconds",
    "nymeria_claude_code_default_mode", "nymeria_claude_code_disallowed_tools",
    "nymeria_claude_code_fallback_model", "nymeria_claude_code_max_budget_usd",
    "nymeria_claude_code_max_concurrency", "nymeria_claude_code_max_turns",
    "nymeria_claude_code_model", "nymeria_claude_code_roots",
    "nymeria_claude_code_token", "nymeria_claude_code_url",
    "nymeria_confine_file_to_workspace", "nymeria_debug",
    "nymeria_enforce_mcp_stdio_allowlist", "nymeria_error_report_email",
    "nymeria_mcp_extra_stdio_commands", "nymeria_service_token",
    "nymeria_snapshots_dir", "outlook_default_account_id",
    "postgres_pool_max_size", "postgres_pool_min_size",
    "quickbase_base_url", "quickbase_hostname", "quickbase_user_token",
    "rag_anchor_enabled",
    "rag_anchor_floor", "rag_anchor_weight", "rag_contextual_enabled",
    "rag_dedup_enabled",
    "rag_dedup_threshold", "rag_fusion_method", "rag_ingest_dedup_enabled",
    "rag_ingest_dedup_threshold",
    "rag_prose_priority_enabled", "rag_prose_priority_weight",
    "rag_recency_enabled", "rag_rerank_local_onnx_file",
    "rag_rerank_top_n", "rag_result_max_chars", "rag_tool_result_max_chars",
    "scheduler_active_execution_stale_minutes",
    "scheduler_failure_alert_after", "scheduler_failure_pause_after",
    "scheduler_missed_work_policy", "seatable_api_token",
    "seatable_base_url", "service_log_backup_count", "service_log_file",
    "service_log_max_bytes",
    "service_token_warn_days", "servicenow_access_token",
    "servicenow_base_url", "servicenow_instance",
    "servicenow_password", "servicenow_username", "slack_app_token",
    "slack_respond_mode",
    "slack_show_tool_events", "slack_webhook_url", "smithery_api_key",
    "sqlite_path",
    "stackby_api_key", "stackby_base_url", "supabase_api_key",
    "supabase_base_url",
    "supabase_service_role_key", "supabase_url", "teams_account_id",
    "teams_channel_id",
    "teams_team_id", "telegram_bot_username",
    "telegram_reaction_trigger_enabled",
    "twitch_pulse_message_count",
    "twitter_access_token", "twitter_api_base_url",
    "twitter_bearer_token", "worker_mode", "zammad_base_url",
    "zammad_password",
    "zammad_token", "zammad_username",
})


def test_new_settings_fields_must_be_reachable_or_exempted():
    """Shrink-only exemption ratchet for the unreachable-setting class.

    NYMERIA_PUBLIC_URL was a real Settings field invisible to the whole /env
    surface AND absent from the update model, which turned one unset variable
    into a 74-day OAuth outage. A new Settings field must now be reachable
    through the config surface (an /env show category or ServerSettingsUpdate)
    or consciously exempted above. Patchable-but-uncategorized fields (the
    ~480 integration credentials) are reachable by definition and exempt by
    predicate, so a new integration field never trips this test.
    """
    from nymeria.api.schemas.settings import HIDDEN_CONFIG_SETTINGS

    unreachable = (
        set(Settings.model_fields)
        - _visible_env_fields()
        - set(ServerSettingsUpdate.model_fields)
        - HIDDEN_CONFIG_SETTINGS
    )
    unexempted = unreachable - _UNREACHABLE_SETTINGS_EXEMPT
    assert not unexempted, (
        f"new unreachable settings fields (make settable/visible or exempt): "
        f"{sorted(unexempted)}"
    )
    stale = _UNREACHABLE_SETTINGS_EXEMPT - unreachable
    assert not stale, f"stale exemption entries (delete them): {sorted(stale)}"


def test_agent_write_policy_lists_are_patchable_fields():
    """#157 list ratchet: a rename cannot silently orphan a policy entry."""
    from nymeria.api.schemas.settings import (
        AGENT_WRITE_ALERT_SETTINGS,
        AGENT_WRITE_BLOCKED_SETTINGS,
    )

    patchable = set(ServerSettingsUpdate.model_fields)
    assert AGENT_WRITE_ALERT_SETTINGS <= patchable, sorted(
        AGENT_WRITE_ALERT_SETTINGS - patchable
    )
    assert AGENT_WRITE_BLOCKED_SETTINGS <= patchable, sorted(
        AGENT_WRITE_BLOCKED_SETTINGS - patchable
    )
