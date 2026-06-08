import sys
import types

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _module(monkeypatch, name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _install_langchain_community_parents(monkeypatch):
    _module(monkeypatch, "langchain_community")
    _module(monkeypatch, "langchain_community.tools")
    _module(monkeypatch, "langchain_community.utilities")


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_calculator_evaluates_safe_math():
    from nymeria.tools.utility_integrations import calculator

    assert calculator.func("2 + 3 * 4") == "14"
    assert calculator.func("sqrt(81) + round(pi, 2)") == "12.14"
    assert calculator.func("__import__('os').system('id')").startswith("[Error]:")


def test_wikipedia_search_uses_langchain_wrapper(monkeypatch):
    from nymeria.tools.utility_integrations import wikipedia_search

    captured = {}

    class FakeWikipediaAPIWrapper:
        def __init__(self, **kwargs):
            captured["wrapper"] = kwargs

    class FakeWikipediaQueryRun:
        def __init__(self, *, api_wrapper):
            captured["tool_wrapper"] = api_wrapper

        def invoke(self, query):
            captured["query"] = query
            return "wiki result"

    _install_langchain_community_parents(monkeypatch)
    _module(monkeypatch, "langchain_community.tools.wikipedia")
    _module(
        monkeypatch,
        "langchain_community.tools.wikipedia.tool",
        WikipediaQueryRun=FakeWikipediaQueryRun,
    )
    _module(
        monkeypatch,
        "langchain_community.utilities.wikipedia",
        WikipediaAPIWrapper=FakeWikipediaAPIWrapper,
    )

    result = wikipedia_search.func(
        query="Ada Lovelace",
        top_k_results=5,
        language="en",
        max_chars=2500,
    )

    assert result == "wiki result"
    assert captured["wrapper"] == {
        "top_k_results": 5,
        "lang": "en",
        "doc_content_chars_max": 2500,
    }
    assert captured["query"] == "Ada Lovelace"


def test_wolfram_alpha_query_uses_configured_app_id(monkeypatch):
    from nymeria.tools.utility_integrations import wolfram_alpha_query

    captured = {}

    class FakeWolframAlphaAPIWrapper:
        def __init__(self, **kwargs):
            captured["wrapper"] = kwargs

    class FakeWolframAlphaQueryRun:
        def __init__(self, *, api_wrapper):
            captured["tool_wrapper"] = api_wrapper

        def invoke(self, query):
            captured["query"] = query
            return "wolfram result"

    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-123")
    _install_langchain_community_parents(monkeypatch)
    _module(monkeypatch, "langchain_community.tools.wolfram_alpha")
    _module(
        monkeypatch,
        "langchain_community.tools.wolfram_alpha.tool",
        WolframAlphaQueryRun=FakeWolframAlphaQueryRun,
    )
    _module(
        monkeypatch,
        "langchain_community.utilities.wolfram_alpha",
        WolframAlphaAPIWrapper=FakeWolframAlphaAPIWrapper,
    )

    result = wolfram_alpha_query.func("population of Sydney")

    assert result == "wolfram result"
    assert captured["wrapper"] == {"wolfram_alpha_appid": "app-123"}
    assert captured["query"] == "population of Sydney"


def test_wolfram_alpha_query_uses_vault_app_id(tmp_path, monkeypatch):
    from nymeria.tools.utility_integrations import wolfram_alpha_query

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Wolfram Alpha",
        provider="wolfram_alpha",
        kind="api_key",
        allowed_targets=["native_tool:wolfram_alpha_query"],
        secret_fields={"app_id": "vault-app-id"},
        created_by_user_id="alice",
    )
    captured = {}

    class FakeWolframAlphaAPIWrapper:
        def __init__(self, **kwargs):
            captured["wrapper"] = kwargs

    class FakeWolframAlphaQueryRun:
        def __init__(self, *, api_wrapper):
            captured["tool_wrapper"] = api_wrapper

        def invoke(self, query):
            captured["query"] = query
            return "vault wolfram result"

    _install_langchain_community_parents(monkeypatch)
    _module(monkeypatch, "langchain_community.tools.wolfram_alpha")
    _module(
        monkeypatch,
        "langchain_community.tools.wolfram_alpha.tool",
        WolframAlphaQueryRun=FakeWolframAlphaQueryRun,
    )
    _module(
        monkeypatch,
        "langchain_community.utilities.wolfram_alpha",
        WolframAlphaAPIWrapper=FakeWolframAlphaAPIWrapper,
    )

    result = wolfram_alpha_query.func(
        "population of Sydney",
        config={"configurable": {"user_id": "alice"}},
    )

    assert result == "vault wolfram result"
    assert captured["wrapper"] == {"wolfram_alpha_appid": "vault-app-id"}


def test_utility_integrations_tools_are_optional_integrations():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    for name in {
        "calculator",
        "wikipedia_search",
        "wolfram_alpha_query",
    }:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE
        assert metadata.default_enabled is False
