import pytest
from pydantic import ValidationError

from nymeria.config.settings import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_USER_TIMEZONE,
    MAX_LLM_OUTPUT_TOKENS,
    Settings,
)


def test_cors_default_is_restricted_to_local_desktop_origins():
    assert Settings.model_fields["cors_origins"].default == DEFAULT_CORS_ORIGINS
    assert DEFAULT_CORS_ORIGINS != "*"

    settings = Settings(_env_file=None, cors_origins=DEFAULT_CORS_ORIGINS)

    assert settings.cors_origins_list == [
        "http://localhost:1420",
        "tauri://localhost",
    ]


def test_cors_wildcard_requires_explicit_override():
    settings = Settings(_env_file=None, cors_origins="*")

    assert settings.cors_origins_list == ["*"]


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


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
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
