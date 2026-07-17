"""Registry-backed provider catalog and live model listing.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _FakeModelsClient,
    _install_fake_models_client,
)


# --- registry-backed provider catalog ---------------------------------------


def test_grouped_provider_specs_tiers_and_membership():
    from nymeria.config.llm_providers import ALL_LLM_PROVIDERS
    from nymeria.setup.providers import grouped_provider_specs

    groups = grouped_provider_specs()
    assert [label for label, _ in groups][:3] == [
        "Native reasoning",
        "Gateway",
        "Unverified",
    ]

    tier_of = {spec.id: label for label, specs in groups for spec in specs}
    assert tier_of["anthropic"] == "Native reasoning"
    assert tier_of["openai"] == "Native reasoning"
    assert tier_of["openrouter"] == "Gateway"
    assert tier_of["groq"] == "Unverified"

    all_ids = [spec.id for _label, specs in groups for spec in specs]
    assert len(all_ids) == len(set(all_ids))  # no duplicates
    assert set(all_ids) == set(ALL_LLM_PROVIDERS)  # every provider present

    for _label, specs in groups:  # each group stays label-sorted
        labels = [spec.label.lower() for spec in specs]
        assert labels == sorted(labels)


def test_filter_items_substring_and_headers():
    from nymeria.setup.widgets import ListItem, filter_items

    items = [
        ListItem(value="", primary="Native", is_header=True),
        ListItem(value="anthropic", primary="Anthropic"),
        ListItem(value="openai", primary="OpenAI"),
        ListItem(value="", primary="Gateway", is_header=True),
        ListItem(value="openrouter", primary="OpenRouter"),
    ]
    assert filter_items(items, "") == items  # empty query returns everything

    result = filter_items(items, "OPEN")  # case-insensitive substring
    assert [i.value for i in result if not i.is_header] == ["openai", "openrouter"]
    # a header survives only when its group still has a visible row
    assert [i.primary for i in result if i.is_header] == ["Native", "Gateway"]

    assert filter_items(items, "zzzzz") == []  # no matches -> nothing (no headers)


# --- live model listing -----------------------------------------------------


def test_fetch_models_for_spec_anthropic(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch, body={"data": [{"id": "claude-3"}, {"id": "claude-2"}]}
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("anthropic"), api_key="sk-ant-x")  # type: ignore[bad-argument-type]
    )

    assert [m.id for m in models] == ["claude-2", "claude-3"]  # sorted by id
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/models"
    assert call["headers"]["x-api-key"] == "sk-ant-x"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_fetch_models_for_spec_openai_compatible(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch, body={"data": [{"id": "deepseek-chat", "name": "DeepSeek Chat"}]}
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("deepseek"), api_key="sk-deepseek")  # type: ignore[bad-argument-type]
    )

    assert [(m.id, m.name) for m in models] == [("deepseek-chat", "DeepSeek Chat")]
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "https://api.deepseek.com/models"
    assert call["headers"]["Authorization"] == "Bearer sk-deepseek"


def test_fetch_models_for_spec_local_substitutes_not_needed(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(monkeypatch, body={"data": [{"id": "local-model"}]})
    models = asyncio.run(
        fetch_models_for_spec(
            get_llm_provider_spec("lmstudio"),  # type: ignore[bad-argument-type]
            api_key="",
            base_url="http://localhost:1234/v1",
        )
    )

    assert [m.id for m in models] == ["local-model"]
    call = _FakeModelsClient.calls[0]
    assert call["url"] == "http://localhost:1234/v1/models"
    assert call["headers"]["Authorization"] == "Bearer not-needed"


def test_fetch_models_for_spec_returns_empty_on_http_error(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(monkeypatch, status=401, body={"error": "nope"})
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("openai"), api_key="sk-bad")  # type: ignore[bad-argument-type]
    )
    assert models == []

# --- honest validation probes (beta-readiness 03) ----------------------------
#
# Gemini validates (and lists models) through its documented OpenAI-compat
# shim; Ollama through the native /api/tags listing. Both used to fall through
# to `tested=False` silently.


def test_models_request_google_uses_openai_compat_shim():
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import _models_request

    spec = get_llm_provider_spec("google")
    assert _models_request(spec, "AIza-key", None) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        {"Authorization": "Bearer AIza-key"},
    )
    # A custom base URL wins (the user points at their own compat endpoint).
    url, _headers = _models_request(spec, "AIza-key", "https://proxy.example/v1/")
    assert url == "https://proxy.example/v1/models"
    # Keyless Gemini has nothing to probe with.
    assert _models_request(spec, "", None) is None


def test_models_request_ollama_probes_api_tags():
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import _models_request

    spec = get_llm_provider_spec("ollama")
    # Default local endpoint, no auth.
    assert _models_request(spec, "", None) == (
        "http://localhost:11434/api/tags",
        {},
    )
    # Custom base + optional key (an auth proxy in front of Ollama).
    assert _models_request(spec, "tok", "http://box:11434/") == (
        "http://box:11434/api/tags",
        {"Authorization": "Bearer tok"},
    )


def test_fetch_models_for_spec_ollama_parses_tags_shape(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch,
        body={"models": [{"name": "qwen3:8b"}, {"name": "gemma3:4b"}]},
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("ollama"), api_key="")  # type: ignore[bad-argument-type]
    )

    assert [m.id for m in models] == ["gemma3:4b", "qwen3:8b"]  # sorted by id
    assert _FakeModelsClient.calls[0]["url"] == "http://localhost:11434/api/tags"


def test_fetch_models_for_spec_google_lists_via_shim(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import fetch_models_for_spec

    _install_fake_models_client(
        monkeypatch,
        body={"data": [{"id": "gemini-3.5-flash"}, {"id": "gemini-3.1-pro-preview"}]},
    )
    models = asyncio.run(
        fetch_models_for_spec(get_llm_provider_spec("google"), api_key="AIza-key")  # type: ignore[bad-argument-type]
    )

    assert [m.id for m in models] == ["gemini-3.1-pro-preview", "gemini-3.5-flash"]
    call = _FakeModelsClient.calls[0]
    assert call["url"].endswith("/v1beta/openai/models")
    assert call["headers"]["Authorization"] == "Bearer AIza-key"


class _FakeSyncClient:
    """Stand-in for httpx.Client covering the GET-probe connection checks."""

    response_status = 200
    raise_transport: Exception | None = None
    calls: list[dict] = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def get(self, url, *, headers):
        import httpx

        type(self).calls.append({"url": url, "headers": headers})
        if type(self).raise_transport is not None:
            raise type(self).raise_transport
        return httpx.Response(
            self.response_status,
            json={"data": []},
            request=httpx.Request("GET", url),
        )


def _install_fake_sync_client(monkeypatch, *, status=200, raise_transport=None):
    import httpx

    _FakeSyncClient.calls = []
    _FakeSyncClient.response_status = status
    _FakeSyncClient.raise_transport = raise_transport
    monkeypatch.setattr(httpx, "Client", _FakeSyncClient)


def test_check_llm_connection_probes_google_ollama_and_nvidia(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import check_llm_connection_for_spec

    _install_fake_sync_client(monkeypatch)
    for provider_id, model in (
        ("google", "gemini-3.5-flash"),
        ("ollama", "qwen3:8b"),
        ("nvidia", "nvidia/nemotron-3-super-120b-a12b"),
    ):
        result = check_llm_connection_for_spec(
            get_llm_provider_spec(provider_id), model, "some-key"  # type: ignore[bad-argument-type]
        )
        assert result.tested is True, provider_id
        assert result.model == model

    urls = [call["url"] for call in _FakeSyncClient.calls]
    assert urls == [
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "http://localhost:11434/api/tags",
        "https://integrate.api.nvidia.com/v1/models",
    ]


def test_check_llm_connection_ollama_unreachable_is_actionable(monkeypatch):
    import httpx
    import pytest

    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import LLMConnectionError, check_llm_connection_for_spec

    _install_fake_sync_client(
        monkeypatch, raise_transport=httpx.ConnectError("connection refused")
    )
    with pytest.raises(LLMConnectionError) as excinfo:
        check_llm_connection_for_spec(get_llm_provider_spec("ollama"), "qwen3:8b", "")  # type: ignore[bad-argument-type]

    message = str(excinfo.value)
    assert "ollama.com" in message
    assert "http://localhost:11434" in message
    assert "--skip-llm-test" in message


def test_check_llm_connection_bedrock_still_reports_untested(monkeypatch):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.providers import check_llm_connection_for_spec

    # No httpx fake installed: bedrock must short-circuit before any request.
    result = check_llm_connection_for_spec(
        get_llm_provider_spec("bedrock"), "some-model", "some-key"  # type: ignore[bad-argument-type]
    )
    assert result.tested is False
