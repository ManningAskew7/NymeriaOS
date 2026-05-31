"""Tests for the web_search_perplexity tool (renamed from web_search).

Covers credential resolution precedence (vault -> settings -> env), the
missing-credential hint, source formatting, and registration under the new name
in the opt-in WEB_SEARCH_SERVICE_TOOLS group.
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
