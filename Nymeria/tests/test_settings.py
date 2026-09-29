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


# --- #101 entries 17 and 21 (2026-08-24): silent container misconfig ---------


def _fake_module_presence(monkeypatch, name, present):
    """Make ``name`` look installed (or not) to the find_spec probes."""
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(module, *args, **kwargs):
        if module == name:
            return object() if present else None
        return real_find_spec(module, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def _warnings_mentioning(settings, needle, **kwargs):
    _errors, warnings = settings.validate_runtime(**kwargs)
    return [w for w in warnings if needle in w]


def _real_secrets_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def test_missing_secrets_key_warns_at_boot_with_a_mint_command(monkeypatch):
    # A compose stood up by hand never runs the wizard that mints the key, and
    # the first sign used to be a failed credential save deep in some UI.
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    settings = Settings(_env_file=None)

    [warning] = _warnings_mentioning(settings, "NYMERIA_SECRETS_KEY")
    assert "not set" in warning
    assert "credential" in warning.lower()
    # The exact command: without .decode() it prints b'...', which is not a key.
    assert (
        'python3 -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    ) in warning
    assert "docker exec" not in warning  # shape-neutral: no container name
    # A Docker restart does not re-read the env file; the copy must say up -d.
    assert "up -d" in warning


def test_a_valid_secrets_key_is_silent(monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", _real_secrets_key())
    settings = Settings(_env_file=None)

    assert _warnings_mentioning(settings, "NYMERIA_SECRETS_KEY") == []


@pytest.mark.parametrize(
    "value",
    [
        # What a bare print(Fernet.generate_key()) writes: the bytes repr.
        "b'" + "A" * 43 + "='",
        "not-a-key",
        "A" * 40 + "=",  # decodes, but to the wrong length
    ],
)
def test_a_malformed_secrets_key_warns_at_boot(monkeypatch, value):
    # Set but unusable passes a presence check, then fails every save.
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", value)
    settings = Settings(_env_file=None)

    [warning] = _warnings_mentioning(settings, "NYMERIA_SECRETS_KEY")
    assert "not a valid key" in warning
    assert "print(Fernet.generate_key().decode())" in warning
    assert value not in warning  # never echo a secret-shaped value


@pytest.mark.parametrize(
    "overrides,env,loss",
    [
        # The embedder normalizes its provider, so " Local " still loads it.
        ({"embedding_provider": " Local "}, "EMBEDDING_PROVIDER", "without embeddings"),
        (
            {"rag_rerank_provider": "local", "rag_rerank_enabled": True},
            "RAG_RERANK_PROVIDER",
            "not reranked",
        ),
    ],
)
def test_local_rag_config_without_the_extra_warns_at_boot(monkeypatch, overrides, env, loss):
    # The config asks for an in-process model the image cannot load: chat works
    # and retrieval quietly degrades.
    _fake_module_presence(monkeypatch, "sentence_transformers", present=False)
    settings = Settings(_env_file=None, **overrides)

    [warning] = _warnings_mentioning(settings, "local-rag")
    assert f"{env}=local" in warning
    assert loss in warning
    assert "nymeriaos[local-rag]" in warning
    assert "NYMERIA_LOCAL_RAG=1" in warning
    assert "nymeria init" in warning


def test_local_rag_warning_names_both_keys_and_both_losses(monkeypatch):
    _fake_module_presence(monkeypatch, "sentence_transformers", present=False)
    settings = Settings(
        _env_file=None,
        embedding_provider="local",
        rag_rerank_provider="local",
        rag_rerank_enabled=True,
    )

    [warning] = _warnings_mentioning(settings, "local-rag")
    assert "EMBEDDING_PROVIDER=local and RAG_RERANK_PROVIDER=local" in warning
    assert "without embeddings" in warning and "not reranked" in warning


def test_local_rag_warning_is_silent_when_the_runtime_would_not_load_a_model(monkeypatch):
    _fake_module_presence(monkeypatch, "sentence_transformers", present=True)
    installed = Settings(
        _env_file=None,
        embedding_provider="local",
        rag_rerank_provider="local",
        rag_rerank_enabled=True,
    )
    assert _warnings_mentioning(installed, "local-rag") == []

    _fake_module_presence(monkeypatch, "sentence_transformers", present=False)
    for overrides in (
        {"embedding_provider": "openai", "rag_rerank_provider": "llm"},
        # Reranking off: the local reranker is never reached.
        {"rag_rerank_provider": "local", "rag_rerank_enabled": False},
        # The rerank path compares exactly, so this never selects local.
        {"rag_rerank_provider": "Local", "rag_rerank_enabled": True},
    ):
        settings = Settings(_env_file=None, **overrides)
        assert _warnings_mentioning(settings, "local-rag") == [], overrides


def test_thin_clients_skip_the_server_dependency_warnings(monkeypatch):
    # The MCP server and the bots relay to the API and run without the LLM
    # key, vault key, and embedder by design: warning there tells a correctly
    # configured stack to change something.
    _fake_module_presence(monkeypatch, "sentence_transformers", present=False)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_DIRECT_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        embedding_provider="local",
        nymeria_service_token=None,
        database_backend="postgres",
        postgres_uri=None,
    )

    server_errors, server_warnings = settings.validate_runtime()
    thin_errors, thin_warnings = settings.validate_runtime(server_process=False)

    server_text = "\n".join(server_warnings)
    for needle in ("LLM provider", "NYMERIA_SECRETS_KEY", "local-rag"):
        assert needle in server_text
        assert not any(needle in w for w in thin_warnings), needle
    # What a thin client does need is still checked.
    assert any("NYMERIA_SERVICE_TOKEN" in w for w in thin_warnings)
    assert thin_errors == server_errors and any("POSTGRES_URI" in e for e in thin_errors)
