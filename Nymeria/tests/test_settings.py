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


def test_suite_hermeticity_pins_root_and_disables_dotenv_chain():
    # Pins the conftest hermeticity mechanism (backlog #101 entry 20): a
    # refactor or conftest reshuffle that silently re-opens the ambient-instance
    # leak must fail here, not resurface as RAG-default failures on
    # multi-instance hosts.
    #
    # The MECHANISM moved with #302. `Settings` no longer declares an
    # `env_file` at all (the files are materialized into `os.environ` once, at
    # boot), so blanking that key is no longer what stops the checkout's real
    # `.env.docker` reaching the suite. `suppress_env_file_loading()` is, and it
    # covers the default chain however it is spelled.
    import os
    from pathlib import Path

    from nymeria.config import settings as settings_mod

    assert not Settings.model_config.get("env_file")
    assert settings_mod.load_env_files_into_environ() == []
    assert settings_mod.load_env_files_into_environ(settings_mod.PROJECT_ROOT) == []

    pinned_root = Path(os.environ["NYMERIA_PROJECT_ROOT"]).resolve()
    assert pinned_root == Path(__file__).resolve().parents[1]


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




# --- #101 entry 12: the startup web-search warning ---------------------------

_SEARCH_WARNING_MARK = "web search"


def _hide_ddgs(monkeypatch):
    """Make ``ddgs`` look uninstalled to has_web_search_backend's find_spec."""
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "ddgs":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def _clear_search_env(monkeypatch):
    from nymeria.config.settings import WEB_SEARCH_BACKEND_ENV_VARS

    for env in WEB_SEARCH_BACKEND_ENV_VARS:
        monkeypatch.delenv(env, raising=False)


def _search_warnings(settings):
    _errors, warnings = settings.validate_runtime()
    return [w for w in warnings if _SEARCH_WARNING_MARK in w.lower()]


def test_no_search_warning_when_only_keyless_ddgs_is_available(monkeypatch):
    # The shipped bug: every install without Perplexity was told "Web search
    # will be unavailable" although web_search_ddgs needs no key and ddgs is a
    # core dependency.
    _clear_search_env(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.has_web_search_backend() is True
    assert _search_warnings(settings) == []


def test_search_warning_when_no_backend_at_all_names_every_option(monkeypatch):
    from nymeria.config.settings import WEB_SEARCH_BACKEND_ENV_VARS

    _clear_search_env(monkeypatch)
    _hide_ddgs(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.has_web_search_backend() is False
    [warning] = _search_warnings(settings)
    for env in WEB_SEARCH_BACKEND_ENV_VARS:
        assert env in warning
    assert "ddgs" in warning
    assert "Web search will be unavailable" not in warning


@pytest.mark.parametrize(
    "env,value",
    [
        ("PERPLEXITY_API_KEY", "pplx-test"),
        ("TAVILY_API_KEY", "tvly-test"),
        ("EXA_API_KEY", "exa-test"),
        ("FIRECRAWL_API_KEY", "fc-test"),
        ("BRAVE_API_KEY", "brave-test"),
        ("SEARXNG_BASE_URL", "http://searxng:8080"),
    ],
)
def test_any_keyed_backend_satisfies_search_without_ddgs(monkeypatch, env, value):
    # Also pins the env-var -> Settings attribute map: a wrong attribute name
    # for any backend would leave this one red.
    _clear_search_env(monkeypatch)
    _hide_ddgs(monkeypatch)
    monkeypatch.setenv(env, value)
    settings = Settings(_env_file=None)

    assert settings.has_web_search_backend() is True
    assert _search_warnings(settings) == []


def test_keyed_backend_list_matches_the_wizard_key_specs_and_the_catalog():
    # Three places name the web_search family's credentials: this list (the
    # startup warning and the wizard's capability summary), the wizard's
    # per-tool key specs, and the tool catalog. Compared by env-var NAME, so a
    # misspelled var fails here, not only a missing one. Scope is the
    # web_search_* family the wizard offers; jina_search_web belongs to the Jina
    # service integration, not this family.
    from nymeria.config.settings import WEB_SEARCH_BACKEND_ENV_VARS
    from nymeria.setup.tool_keys import BACKEND_KEY_SPECS
    from nymeria.tools import CATALOG_TOOLS

    keyed_tools = {
        name for name in CATALOG_TOOLS
        if name.startswith("web_search_") and name != "web_search_ddgs"
    }
    spec_env = {
        spec.env_var for tool, spec in BACKEND_KEY_SPECS.items()
        if tool.startswith("web_search_")
    }
    spec_tools = {tool for tool in BACKEND_KEY_SPECS if tool.startswith("web_search_")}

    assert spec_tools == keyed_tools
    assert set(WEB_SEARCH_BACKEND_ENV_VARS) == spec_env
