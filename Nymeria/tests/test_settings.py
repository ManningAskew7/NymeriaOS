from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nymeria.config.settings import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_USER_TIMEZONE,
    MAX_LLM_OUTPUT_TOKENS,
    Settings,
)
from nymeria.triggers.api import _validate_cors_settings


def test_cors_default_is_restricted_to_local_ui_origins():
    assert Settings.model_fields["cors_origins"].default == DEFAULT_CORS_ORIGINS
    assert DEFAULT_CORS_ORIGINS != "*"

    settings = Settings(_env_file=None, cors_origins=DEFAULT_CORS_ORIGINS)

    assert settings.cors_origins_list == [
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost:8000",
    ]


@pytest.mark.parametrize("cors_origins", ["*", " http://localhost:1420 , * "])
def test_cors_wildcard_is_rejected_with_credentials(cors_origins):
    with pytest.raises(ValidationError, match="CORS_ORIGINS cannot include '\\*'"):
        Settings(_env_file=None, cors_origins=cors_origins)


def test_api_startup_rejects_wildcard_cors_from_injected_settings():
    settings = SimpleNamespace(cors_origins_list=["*"])

    with pytest.raises(RuntimeError, match="CORS_ORIGINS cannot include '\\*'"):
        _validate_cors_settings(settings)


def test_user_timezone_defaults_to_utc(monkeypatch):
    monkeypatch.delenv("USER_TIMEZONE", raising=False)

    settings = Settings(_env_file=None)

    assert Settings.model_fields["user_timezone"].default == DEFAULT_USER_TIMEZONE
    assert settings.user_timezone == "UTC"


def test_api_docs_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("NYMERIA_DEBUG", raising=False)
    monkeypatch.delenv("NYMERIA_API_DOCS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.api_docs_enabled is False


def test_api_docs_are_enabled_by_explicit_flag(monkeypatch):
    monkeypatch.delenv("NYMERIA_DEBUG", raising=False)
    monkeypatch.setenv("NYMERIA_API_DOCS", "true")

    settings = Settings(_env_file=None)

    assert settings.api_docs_enabled is True


def test_api_docs_are_enabled_by_debug_flag(monkeypatch):
    monkeypatch.setenv("NYMERIA_DEBUG", "true")
    monkeypatch.delenv("NYMERIA_API_DOCS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.api_docs_enabled is True


@pytest.mark.parametrize("max_tokens", [64000, 128000, MAX_LLM_OUTPUT_TOKENS])
def test_llm_max_tokens_accepts_large_modern_output_limits(max_tokens):
    settings = Settings(_env_file=None, llm_max_tokens=max_tokens)

    assert settings.llm_max_tokens == max_tokens


def test_llm_max_tokens_accepts_large_env_value(monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOKENS", "128000")

    settings = Settings(_env_file=None)

    assert settings.llm_max_tokens == 128000


def test_llm_max_tokens_rejects_values_above_config_guardrail():
    with pytest.raises(ValidationError, match="llm_max_tokens"):
        Settings(_env_file=None, llm_max_tokens=MAX_LLM_OUTPUT_TOKENS + 1)


@pytest.mark.parametrize("effort", ["off", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_accepts_documented_values(effort):
    settings = Settings(_env_file=None, llm_reasoning_effort=effort)

    assert settings.llm_reasoning_effort == effort


def test_reasoning_effort_rejects_invalid_constructor_value():
    with pytest.raises(ValidationError, match="llm_reasoning_effort"):
        Settings(_env_file=None, llm_reasoning_effort="extreme")


def test_reasoning_effort_rejects_invalid_env_value(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")

    with pytest.raises(ValidationError, match="llm_reasoning_effort"):
        Settings(_env_file=None)


def test_anthropic_direct_key_is_used_for_direct_provider_config():
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        llm_base_url="",
        anthropic_api_key=None,
        anthropic_direct_api_key="sk-ant-direct",
    )

    assert settings.get_api_key_for_provider() == "sk-ant-direct"


def test_anthropic_proxy_config_uses_gatekeeper_key():
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        llm_base_url="http://cli-proxy-api:8317",
        anthropic_api_key="cpx-gatekeeper",
        anthropic_direct_api_key="sk-ant-direct",
    )

    assert settings.get_api_key_for_provider() == "cpx-gatekeeper"


def test_load_soul_prefers_override_then_packaged_default(tmp_path):
    settings = Settings(_env_file=None, nymeria_data_dir=str(tmp_path))

    override = settings.system_prompt_override_path
    assert override == tmp_path / "system_prompt.md"

    # No override file: falls back to the packaged soul.md (non-empty default).
    packaged = settings.load_soul()
    assert packaged.strip()

    # A non-empty override wins over the packaged default.
    override.write_text("CUSTOM PERSONA", encoding="utf-8")
    assert settings.load_soul() == "CUSTOM PERSONA"

    # A blank/whitespace override is ignored; falls back to the packaged default.
    override.write_text("   \n\t ", encoding="utf-8")
    assert settings.load_soul() == packaged


# ---------------------------------------------------------------------------
# validate_runtime bot-key warnings (F9: data-table refactor)
# ---------------------------------------------------------------------------

# Bot-token env vars are commonly set on deployment hosts (e.g. the Docker
# convention DISCORD_BOT_TOKEN=disabled), so these tests must clear them to stay
# hermetic and not pick up an ambient platform's token.
_BOT_TOKEN_ENV_VARS = (
    "TWITCH_BOT_ACCESS_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_PASSWORD",
    "SIGNAL_HTTP_URL",
    "SIGNAL_ACCOUNT",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_IG_USER_ID",
)


def _clear_bot_token_env(monkeypatch) -> None:
    for var in _BOT_TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_validate_runtime_warns_when_bot_token_set_without_provider_key(monkeypatch):
    _clear_bot_token_env(monkeypatch)
    settings = Settings(
        _env_file=None,
        slack_bot_token="xoxb-test",
        instagram_access_token="ig-test",
    )
    # Force "no LLM key" regardless of ambient env so the bot-warning branch fires.
    monkeypatch.setattr(Settings, "get_api_key_for_provider", lambda self: "")

    _errors, warnings = settings.validate_runtime()

    assert any(
        "SLACK_BOT_TOKEN or SLACK_APP_TOKEN is set but no LLM API key configured.\n"
        "  The Slack bot will not be able to process messages." == w
        for w in warnings
    )
    assert any(
        "INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_IG_USER_ID is set but no LLM API key configured.\n"
        "  The Instagram bot will not be able to process messages." == w
        for w in warnings
    )
    # A platform whose token is unset must not warn.
    assert not any("Discord bot will not be able" in w for w in warnings)


def test_validate_runtime_suppresses_bot_warning_when_provider_key_present(monkeypatch):
    _clear_bot_token_env(monkeypatch)
    settings = Settings(_env_file=None, twitch_bot_access_token="oauth:test")
    monkeypatch.setattr(Settings, "get_api_key_for_provider", lambda self: "sk-present")

    _errors, warnings = settings.validate_runtime()

    assert not any("Twitch bot will not be able" in w for w in warnings)
