import json
import sys
import types

import pytest


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


def test_calculator_evaluates_safe_math():
    from nymeria.tools.n8n_langchain import calculator

    assert calculator.func("2 + 3 * 4") == "14"
    assert calculator.func("sqrt(81) + round(pi, 2)") == "12.14"
    assert calculator.func("__import__('os').system('id')").startswith("[Error]:")


def test_wikipedia_search_uses_langchain_wrapper(monkeypatch):
    from nymeria.tools.n8n_langchain import wikipedia_search

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
    from nymeria.tools.n8n_langchain import wolfram_alpha_query

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


def test_searxng_search_uses_configured_base_url(monkeypatch):
    from nymeria.tools.n8n_langchain import searxng_search

    captured = {}

    class FakeSearxSearchWrapper:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def results(self, query, num_results, categories, engines, **kwargs):
            captured["call"] = {
                "query": query,
                "num_results": num_results,
                "categories": categories,
                "engines": engines,
                **kwargs,
            }
            return [{"title": "Result", "link": "https://example.com"}]

    monkeypatch.setenv("SEARXNG_BASE_URL", "https://searx.example/search")
    _install_langchain_community_parents(monkeypatch)
    _module(
        monkeypatch,
        "langchain_community.utilities.searx_search",
        SearxSearchWrapper=FakeSearxSearchWrapper,
    )

    result = searxng_search.func(
        query="nymeria",
        num_results=3,
        page_number=2,
        language="en",
        safesearch=1,
        categories="general, news",
        engines="duckduckgo, brave",
    )

    assert json.loads(result) == [{"title": "Result", "link": "https://example.com"}]
    assert captured["init"] == {
        "searx_host": "https://searx.example/search",
        "headers": {"Accept": "application/json"},
        "params": {"language": "en", "safesearch": 1},
    }
    assert captured["call"] == {
        "query": "nymeria",
        "num_results": 3,
        "categories": ["general", "news"],
        "engines": ["duckduckgo", "brave"],
        "pageno": 2,
    }


def test_n8n_langchain_tools_are_optional_integrations():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    for name in {
        "calculator",
        "wikipedia_search",
        "wolfram_alpha_query",
        "searxng_search",
    }:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE
        assert metadata.default_enabled is False
