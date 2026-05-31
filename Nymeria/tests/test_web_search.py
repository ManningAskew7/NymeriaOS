"""Tests for the opt-in web search tools (web_search_perplexity, web_search_tavily, web_search_exa, web_search_firecrawl, web_search_brave).

Covers credential resolution precedence (vault -> settings -> env), the
missing-credential hint, source formatting, and registration in the opt-in
web search tool groups.
"""

import httpx
import pytest


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_key_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert web._get_perplexity_api_key(config=None) == "vault-key"


def test_key_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        perplexity_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert web._get_perplexity_api_key(config=None) == "settings-key"


def test_missing_credential_returns_setup_hint(monkeypatch):
    from nymeria.tools import web

    monkeypatch.setattr(web, "_get_perplexity_api_key", lambda config=None: None)

    result = web.web_search_perplexity.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_perplexity" in result


def test_requires_a_query():
    from nymeria.tools import web

    assert web.web_search_perplexity.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_single_query_appends_sources(monkeypatch):
    from nymeria.tools import web

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "Answer text"}}],
                "citations": ["https://a.example", "https://b.example"],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = web._search_single("q", "sonar", 5, 60.0, 2000, "key")

    assert "Answer text" in out
    assert "**Sources:**" in out
    assert "1. https://a.example" in out


def test_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web

    class FakeResponse:
        status_code = 401

    def raise_status(self):
        raise httpx.HTTPStatusError("nope", request=None, response=FakeResponse())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            resp = FakeResponse()
            resp.raise_for_status = raise_status.__get__(resp)
            return resp

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = web._search_single("q", "sonar", 5, 60.0, 2000, "key")

    assert out.startswith("[Error]: Perplexity API error: 401")


def test_registered_under_new_name_in_optional_group():
    from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
    from nymeria.tools.web import WEB_SEARCH_SERVICE_TOOLS

    # New name is an opt-in optional tool, not core.
    assert "web_search_perplexity" in OPTIONAL_TOOLS
    assert [t.name for t in WEB_SEARCH_SERVICE_TOOLS] == ["web_search_perplexity"]

    # The old name is gone everywhere.
    assert "web_search" not in OPTIONAL_TOOLS
    assert all(t.name not in ("web_search",) for t in ALL_TOOLS)


# --- Tavily (web_search_tavily) ------------------------------------------------


def _fake_httpx_client(payload):
    """Return a FakeClient class whose .post() yields ``payload`` as JSON."""

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    return FakeClient


def test_tavily_key_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert wsi._get_tavily_api_key(config=None) == "vault-key"


def test_tavily_key_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        tavily_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_tavily_api_key(config=None) == "settings-key"


def test_tavily_missing_credential_returns_setup_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_tavily_api_key", lambda config=None: None)

    result = wsi.web_search_tavily.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_tavily" in result


def test_tavily_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_tavily.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_tavily_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "results": [
            {"title": "First", "url": "https://a.example", "score": 0.834, "content": "snippet a"},
            {"title": "Second", "url": "https://b.example", "score": 0.5, "content": "snippet b"},
        ]
    }

    out = wsi._format_tavily_results(data, max_results=5)

    assert "1. First" in out
    assert "https://a.example  (score: 0.83)" in out
    assert "snippet a" in out
    assert "2. Second" in out
    assert "**Answer:**" not in out


def test_tavily_include_answer_prepends_answer_block():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "answer": "A short synthesized answer.",
        "results": [{"title": "T", "url": "https://x.example", "score": 0.9, "content": "c"}],
    }

    out = wsi._format_tavily_results(data, max_results=5)

    assert out.startswith("**Answer:** A short synthesized answer.")
    assert "**Sources:**" in out
    assert "1. T" in out


def test_tavily_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_tavily_api_key", lambda config=None: "key")
    monkeypatch.setattr(
        httpx,
        "Client",
        _fake_httpx_client(
            {"results": [{"title": "Doc", "url": "https://d.example", "score": 0.7, "content": "body"}]}
        ),
    )

    out = wsi.web_search_tavily.func(query="python asyncio", max_results=3)

    assert "1. Doc" in out
    assert "https://d.example" in out


def test_tavily_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        status_code = 401

    def raise_status(self):
        raise httpx.HTTPStatusError("nope", request=None, response=FakeResponse())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            resp = FakeResponse()
            resp.raise_for_status = raise_status.__get__(resp)
            return resp

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = wsi._tavily_search_single({"query": "q"}, "key", 30.0, 5)

    assert out.startswith("[Error]: Tavily API error: 401")


def test_tavily_registered_in_optional_group():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_tavily" in OPTIONAL_TOOLS
    assert "web_search_tavily" in [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS]


# --- Exa (web_search_exa) ------------------------------------------------------


def _capturing_httpx_client(response_payload, captured):
    """FakeClient that records the POST url/headers/json into ``captured``."""

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return response_payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    return FakeClient


def test_exa_key_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert wsi._get_exa_api_key(config=None) == "vault-key"


def test_exa_key_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        exa_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_exa_api_key(config=None) == "settings-key"


def test_exa_missing_credential_returns_setup_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: None)

    result = wsi.web_search_exa.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_exa" in result


def test_exa_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_exa.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_exa_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "results": [
            {
                "title": "First",
                "url": "https://a.example",
                "score": 0.834,
                "publishedDate": "2025-01-02",
                "highlights": ["snippet a", "more a"],
            },
            {"title": "Second", "url": "https://b.example", "score": 0.5, "highlights": ["snippet b"]},
        ]
    }

    out = wsi._format_exa_results(data, max_results=5)

    assert "1. First" in out
    assert "https://a.example  (score: 0.83)" in out
    assert "· 2025-01-02" in out
    assert "snippet a more a" in out
    assert "2. Second" in out


def test_exa_formats_no_results():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_exa_results({"results": []}, max_results=5) == "[No results]"


def test_exa_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    monkeypatch.setattr(
        httpx,
        "Client",
        _fake_httpx_client(
            {"results": [{"title": "Doc", "url": "https://d.example", "score": 0.7, "highlights": ["body"]}]}
        ),
    )

    out = wsi.web_search_exa.func(query="neural retrieval", num_results=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert "body" in out


def test_exa_builds_payload_with_filters(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"results": []}, captured))

    wsi.web_search_exa.func(
        query="llms",
        search_type="fast",
        num_results=3,
        category="research paper",
        start_published_date="2025-01-01",
        end_published_date="2025-06-01",
        include_domains="arxiv.org, nature.com",
        exclude_domains="spam.example",
    )

    payload = captured["json"]
    assert payload["type"] == "fast"
    assert payload["numResults"] == 3
    assert payload["category"] == "research paper"
    assert payload["startPublishedDate"] == "2025-01-01"
    assert payload["endPublishedDate"] == "2025-06-01"
    assert payload["includeDomains"] == ["arxiv.org", "nature.com"]
    assert payload["excludeDomains"] == ["spam.example"]
    assert payload["contents"] == {"highlights": {"maxCharacters": 1000}}
    # Auth uses the x-api-key header plus the integration tag.
    assert captured["headers"]["x-api-key"] == "key"
    assert captured["headers"]["x-exa-integration"] == "nymeria"


def test_exa_invalid_search_type_is_ignored(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"results": []}, captured))

    wsi.web_search_exa.func(query="q", search_type="turbo")

    assert "type" not in captured["json"]


def test_exa_company_category_drops_date_and_exclude(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"results": []}, captured))

    wsi.web_search_exa.func(
        query="acme corp",
        category="company",
        start_published_date="2024-01-01",
        end_published_date="2024-12-31",
        exclude_domains="spam.example",
        include_domains="acme.com",
    )

    payload = captured["json"]
    assert payload["category"] == "company"
    # company/people reject these, so they must be dropped (not sent).
    assert "startPublishedDate" not in payload
    assert "endPublishedDate" not in payload
    assert "excludeDomains" not in payload
    # include_domains is still allowed.
    assert payload["includeDomains"] == ["acme.com"]


def test_exa_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        status_code = 401

    def raise_status(self):
        raise httpx.HTTPStatusError("nope", request=None, response=FakeResponse())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            resp = FakeResponse()
            resp.raise_for_status = raise_status.__get__(resp)
            return resp

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = wsi._exa_search_single({"query": "q"}, "key", 30.0, 5)

    assert out.startswith("[Error]: Exa API error: 401")


def test_exa_registered_in_optional_group():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_exa" in OPTIONAL_TOOLS
    assert "web_search_exa" in [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS]


# --- Firecrawl (web_search_firecrawl) ------------------------------------------


def test_firecrawl_key_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert wsi._get_firecrawl_api_key(config=None) == "vault-key"


def test_firecrawl_key_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        firecrawl_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_firecrawl_api_key(config=None) == "settings-key"


def test_firecrawl_missing_credential_returns_setup_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_firecrawl_api_key", lambda config=None: None)

    result = wsi.web_search_firecrawl.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_firecrawl" in result


def test_firecrawl_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_firecrawl.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_firecrawl_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "data": {
            "web": [
                {"title": "First", "url": "https://a.example", "description": "snippet a"},
                {"title": "Second", "url": "https://b.example", "description": "snippet b"},
            ],
            "news": [
                {"title": "Newsy", "url": "https://n.example", "snippet": "news body", "date": "2 days ago"},
            ],
        }
    }

    out = wsi._format_firecrawl_results(data, max_results=5)

    assert "1. First" in out
    assert "https://a.example" in out
    assert "snippet a" in out
    assert "2. Second" in out
    # News items are tagged and use snippet + free-form date.
    assert "3. Newsy [news]" in out
    assert "· 2 days ago" in out
    assert "news body" in out


def test_firecrawl_formats_no_results():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_firecrawl_results({}, max_results=5) == "[No results]"


def test_firecrawl_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_firecrawl_api_key", lambda config=None: "key")
    monkeypatch.setattr(
        httpx,
        "Client",
        _fake_httpx_client(
            {"data": {"web": [{"title": "Doc", "url": "https://d.example", "description": "body"}]}}
        ),
    )

    out = wsi.web_search_firecrawl.func(query="firecrawl scraping", limit=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert "body" in out


def test_firecrawl_builds_payload_with_filters(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_firecrawl_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"data": {"web": []}}, captured))

    wsi.web_search_firecrawl.func(
        query="anthropic claude",
        limit=5,
        time_range="week",
        sources="web, news",
        categories="github, research",
        include_domains="anthropic.com",
    )

    payload = captured["json"]
    assert payload["limit"] == 5
    assert payload["tbs"] == "qdr:w"
    assert payload["sources"] == ["web", "news"]
    assert payload["categories"] == ["github", "research"]
    assert payload["includeDomains"] == ["anthropic.com"]
    # Snippet-only: result pages are never scraped.
    assert "scrapeOptions" not in payload
    # Bearer auth.
    assert captured["headers"]["Authorization"] == "Bearer key"


def test_firecrawl_invalid_category_is_ignored(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_firecrawl_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"data": {"web": []}}, captured))

    # "news" is a valid source but NOT a valid category; only github survives.
    wsi.web_search_firecrawl.func(query="q", categories="news, github")

    assert captured["json"]["categories"] == ["github"]


def test_firecrawl_mutually_exclusive_domains_prefers_include(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_firecrawl_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"data": {"web": []}}, captured))

    wsi.web_search_firecrawl.func(
        query="q",
        include_domains="https://github.com/foo",
        exclude_domains="spam.example",
    )

    payload = captured["json"]
    # Mutually exclusive (Firecrawl 400s on both): include wins, exclude dropped.
    # The host is normalized (scheme/path stripped) so Firecrawl does not 400.
    assert payload["includeDomains"] == ["github.com"]
    assert "excludeDomains" not in payload


def test_firecrawl_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        status_code = 401

    def raise_status(self):
        raise httpx.HTTPStatusError("nope", request=None, response=FakeResponse())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            resp = FakeResponse()
            resp.raise_for_status = raise_status.__get__(resp)
            return resp

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = wsi._firecrawl_search_single({"query": "q"}, "key", 30.0, 5)

    assert out.startswith("[Error]: Firecrawl API error: 401")


def test_firecrawl_registered_in_optional_group():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_firecrawl" in OPTIONAL_TOOLS
    assert "web_search_firecrawl" in [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS]


# --- Brave (web_search_brave) --------------------------------------------------


def _fake_httpx_get_client(payload):
    """Return a FakeClient class whose .get() yields ``payload`` as JSON."""

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    return FakeClient


def _capturing_httpx_get_client(response_payload, captured):
    """FakeClient that records the GET url/headers/params into ``captured``."""

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return response_payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return FakeResponse()

    return FakeClient


def test_brave_key_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert wsi._get_brave_api_key(config=None) == "vault-key"


def test_brave_key_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        brave_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_brave_api_key(config=None) == "settings-key"


def test_brave_missing_credential_returns_setup_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: None)

    result = wsi.web_search_brave.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_brave" in result


def test_brave_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_brave.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_brave_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "web": {
            "results": [
                {"title": "First", "url": "https://a.example", "description": "snippet a", "page_age": "2025-01-02T10:00:00Z"},
                {"title": "Second", "url": "https://b.example", "description": "snippet b"},
            ]
        },
        "news": {
            "results": [
                {"title": "Newsy", "url": "https://n.example", "description": "news body", "age": "2 days ago"},
            ]
        },
        "discussions": {
            "results": [
                {"title": "Thread", "url": "https://d.example", "description": "forum body"},
            ]
        },
    }

    out = wsi._format_brave_results(data, count=5)

    assert "1. First" in out
    # Web items: description snippet + page_age (ISO) date.
    assert "https://a.example · 2025-01-02T10:00:00Z" in out
    assert "snippet a" in out
    assert "2. Second" in out
    # News and discussions are tagged; news falls back to the relative "age".
    assert "3. Newsy [news]" in out
    assert "· 2 days ago" in out
    assert "news body" in out
    assert "4. Thread [discussion]" in out
    assert "forum body" in out


def test_brave_formats_no_results():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_brave_results({}, count=5) == "[No results]"


def test_brave_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: "key")
    monkeypatch.setattr(
        httpx,
        "Client",
        _fake_httpx_get_client(
            {"web": {"results": [{"title": "Doc", "url": "https://d.example", "description": "body"}]}}
        ),
    )

    out = wsi.web_search_brave.func(query="brave search", count=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert "body" in out


def test_brave_builds_params_with_filters(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"web": {"results": []}}, captured))

    wsi.web_search_brave.func(
        query="anthropic claude",
        count=5,
        time_range="week",
        sources="web, news",
        include_domains="github.com",
    )

    params = captured["params"]
    assert params["count"] == 5
    assert params["freshness"] == "pw"
    assert params["result_filter"] == "web,news"
    # text_decorations off keeps <strong> tags out of snippets.
    assert params["text_decorations"] == "false"
    # include_domains becomes a site: operator appended to the query (Brave has
    # no native domain filter).
    assert params["q"] == "anthropic claude site:github.com"
    # Auth uses the X-Subscription-Token header, not a Bearer token.
    assert captured["headers"]["X-Subscription-Token"] == "key"


def test_brave_site_operators_include_and_exclude(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"web": {"results": []}}, captured))

    wsi.web_search_brave.func(
        query="rust",
        include_domains="github.com, https://docs.rs/foo",
        exclude_domains="pinterest.com",
    )

    # Multiple includes group with OR; each exclude uses NOT site:. Hosts are
    # reduced to bare hostnames first (https://docs.rs/foo -> docs.rs).
    assert captured["params"]["q"] == "rust (site:github.com OR site:docs.rs) NOT site:pinterest.com"


def test_brave_invalid_source_is_ignored(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"web": {"results": []}}, captured))

    # "videos" is not a text-useful bucket and is dropped; only news survives.
    wsi.web_search_brave.func(query="q", sources="videos, news")

    assert captured["params"]["result_filter"] == "news"


def test_brave_date_range_passthrough(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_brave_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"web": {"results": []}}, captured))

    wsi.web_search_brave.func(query="q", time_range="2024-01-01to2024-12-31")

    assert captured["params"]["freshness"] == "2024-01-01to2024-12-31"


def test_brave_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        status_code = 401

    def raise_status(self):
        raise httpx.HTTPStatusError("nope", request=None, response=FakeResponse())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            resp = FakeResponse()
            resp.raise_for_status = raise_status.__get__(resp)
            return resp

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = wsi._brave_search_single({"q": "q"}, "key", 15.0, 5)

    assert out.startswith("[Error]: Brave API error: 401")


def test_brave_registered_in_optional_group():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_brave" in OPTIONAL_TOOLS
    assert [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS] == [
        "web_search_tavily",
        "web_search_exa",
        "web_search_firecrawl",
        "web_search_brave",
    ]
