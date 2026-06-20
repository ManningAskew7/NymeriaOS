"""Registry-backed provider catalog and live model listing.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio

from _setup_wizard_helpers import (
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
        fetch_models_for_spec(get_llm_provider_spec("anthropic"), api_key="sk-ant-x")
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
        fetch_models_for_spec(get_llm_provider_spec("deepseek"), api_key="sk-deepseek")
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
            get_llm_provider_spec("lmstudio"),
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
        fetch_models_for_spec(get_llm_provider_spec("openai"), api_key="sk-bad")
    )
    assert models == []
