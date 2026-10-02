"""Tests for the opt-in web search tools (web_search_perplexity, web_search_tavily, web_search_exa_ai, web_search_firecrawl, web_search_brave).

Covers credential resolution precedence (vault -> settings -> env), the
missing-credential hint, source formatting, and registration in the opt-in
web search tool groups.
"""

import httpx
import pytest


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


class _FakeAgentResponse:
    """A canned Agent API body, with the HTTP error hook the tool relies on."""

    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("nope", request=None, response=self)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _install_fake_client(monkeypatch, body, status_code=200):
    """Route the tool's httpx.Client at a canned body; return the capture dict."""
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeAgentResponse(body, status_code)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def _agent_body(text, results_by_item, status="completed", error=None):
    output = [
        {"type": "search_results", "queries": ["q"], "results": results}
        for results in results_by_item
    ]
    output.append(
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    return {
        "object": "response",
        "status": status,
        "error": error,
        "model": "openai/gpt-5.6-luna",
        "output": output,
        "usage": {"cost": {"total_cost": 0.004, "currency": "USD"}},
    }


def test_preset_request_targets_the_agent_api(monkeypatch):
    """A preset rides the Agent API's own fields and NOTHING that would override
    the preset's tuning: no `instructions` (it replaces the preset's system
    prompt and cost the [N] citations live), no `tools` (it replaces the
    preset's tool set), and none of the retired Sonar chat-completions shape,
    since the Agent API 400s on any unknown field."""
    from nymeria.tools import web

    captured = _install_fake_client(monkeypatch, _agent_body("Answer.", [[]]))

    web._search_single("what is new", "medium", 4, 60.0, 2000, "key")

    assert captured["url"] == "https://api.perplexity.ai/v1/agent"
    assert captured["headers"]["Authorization"] == "Bearer key"
    assert captured["client_kwargs"]["timeout"] == 60.0
    body = captured["json"]
    assert body == {"preset": "medium", "input": "what is new", "max_output_tokens": 2000, "store": False}


def test_provider_model_id_gets_instructions_and_an_explicit_search_tool(monkeypatch):
    """A bare provider/model id has no preset prompt and does not search on its
    own, so it is the one branch that sends `instructions` and `tools`."""
    from nymeria.tools import web

    captured = _install_fake_client(monkeypatch, _agent_body("Answer.", [[]]))

    web._search_single("q", "openai/gpt-5.6-luna", 5, 60.0, 2000, "key")

    body = captured["json"]
    assert body["model"] == "openai/gpt-5.6-luna"
    assert "preset" not in body
    assert body["instructions"] == web._SEARCH_INSTRUCTIONS
    assert body["tools"] == [{"type": "web_search", "max_results": 5}]


def test_batch_kill_timeout_grows_with_query_count_and_depth():
    """Queries run one after another, so the tool node's kill timeout must cover
    the whole batch: two `high` queries need 2 x 180s plus the margin; a single
    fast query returns less than the node default, which the node then keeps."""
    from nymeria.tools import web

    derive = web.web_search_perplexity.metadata["inline_wait_timeout"]

    assert derive({"queries": "a | b", "search_depth": "high"}) == 2 * 180.0 + 30.0
    assert derive({"query": "a", "search_depth": "xhigh"}) == 300.0 + 30.0
    assert derive({"query": "a", "search_depth": "fast"}) == 60.0 + 30.0
    assert derive({"query": "a", "search_depth": "bogus"}) is None
    assert derive({}) is None


def test_fetched_pages_continue_the_citation_ids(monkeypatch):
    """Shape of a real `high` run: two search_results items (ids 1-3, 4-5), then
    a fetch_url_results item whose contents carry no id, and an answer citing
    the fetched page as [web:6]. The fetched page must be listed as 6."""
    from nymeria.tools import web

    body = _agent_body(
        "Release 1.6.3 shipped.[web:1][web:6]",
        [
            [{"id": i, "url": f"https://s{i}.example", "title": f"S{i}"} for i in (1, 2, 3)],
            [{"id": i, "url": f"https://s{i}.example", "title": f"S{i}"} for i in (4, 5)],
        ],
    )
    body["output"].insert(
        2,
        {
            "type": "fetch_url_results",
            "contents": [{"snippet": "...", "title": "Release notes", "url": "https://github.example/release"}],
        },
    )
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "high", 10, 180.0, 4000, "key")

    assert "Release 1.6.3 shipped.[1][6]" in out
    assert "6. Release notes: https://github.example/release" in out


def test_idless_results_take_the_next_id_without_colliding(monkeypatch):
    from nymeria.tools import web

    body = _agent_body(
        "Text.",
        [[{"id": 1, "url": "https://a.example", "title": "A"}, {"url": "https://b.example", "title": "B"}, {"id": 7, "url": "https://c.example", "title": "C"}, {"url": "https://d.example", "title": "D"}]],
    )
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "fast", 10, 60.0, 2000, "key")

    sources = out.partition("**Sources** (numbered as cited):")[2].strip().splitlines()
    assert sources == [
        "1. A: https://a.example",
        "2. B: https://b.example",
        "7. C: https://c.example",
        "8. D: https://d.example",
    ]


def test_max_sources_trims_the_list_but_never_a_cited_source(monkeypatch):
    from nymeria.tools import web

    body = _agent_body(
        "Cites the ninth.[9]",
        [[{"id": i, "url": f"https://s{i}.example", "title": f"S{i}"} for i in range(1, 11)]],
    )
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "fast", 3, 60.0, 2000, "key")

    sources = out.partition("**Sources** (numbered as cited):")[2].strip().splitlines()
    assert sources == ["1. S1: https://s1.example", "2. S2: https://s2.example", "9. S9: https://s9.example"]


def test_message_parts_are_joined_and_empty_output_is_named(monkeypatch):
    from nymeria.tools import web

    body = _agent_body("first", [[]])
    body["output"][-1]["content"].append({"type": "refusal", "refusal": "no"})
    body["output"][-1]["content"].append({"type": "output_text", "text": "second", "annotations": []})
    body["output"].insert(0, "not-a-dict")
    _install_fake_client(monkeypatch, body)
    assert web._search_single("q", "fast", 5, 60.0, 2000, "key") == "first\n\nsecond"

    _install_fake_client(monkeypatch, {"status": "completed", "output": []})
    assert web._search_single("q", "fast", 5, 60.0, 2000, "key") == "[No answer text returned]"


def test_sources_keep_the_api_ids_across_search_items(monkeypatch):
    """A multi-step preset emits several search_results items whose ids
    increase globally and the answer cites those ids ([5], or [web:5]); the
    Sources list keeps the ids and de-duplicates so the citations line up."""
    from nymeria.tools import web

    body = _agent_body(
        "Canberra is the capital.[web:5] Population 28M.[1]",
        [
            [
                {"id": 1, "url": "https://a.example", "title": "A"},
                {"id": 2, "url": "https://b.example", "title": "B"},
                {"id": 3, "url": "https://c.example", "title": ""},
            ],
            [
                {"id": 5, "url": "https://e.example", "title": "E"},
                {"id": 6, "url": "https://f.example", "title": "F"},
                {"id": 5, "url": "https://dup.example", "title": "dup"},
            ],
        ],
    )
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "high", 5, 180.0, 4000, "key")

    text, _, sources = out.partition("**Sources** (numbered as cited):")
    assert "Canberra is the capital.[5] Population 28M.[1]" in text
    assert "[web:" not in out
    assert sources.strip().splitlines() == [
        "1. A: https://a.example",
        "2. B: https://b.example",
        "3. https://c.example",
        "5. E: https://e.example",
        "6. F: https://f.example",
    ]


def test_failed_status_is_an_error_even_on_http_200(monkeypatch):
    from nymeria.tools import web

    body = _agent_body("", [[]], status="failed", error={"message": "model overloaded"})
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "fast", 5, 60.0, 2000, "key")

    assert out == "[Error]: Perplexity search failed: model overloaded"


def test_incomplete_status_returns_text_with_a_truncation_note(monkeypatch):
    from nymeria.tools import web

    body = _agent_body("Partial answer", [[{"id": 1, "url": "https://a.example", "title": "A"}]], status="incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    _install_fake_client(monkeypatch, body)

    out = web._search_single("q", "fast", 5, 60.0, 2000, "key")

    assert out.startswith("Partial answer")
    assert "1. A: https://a.example" in out
    assert out.endswith("[Note]: response truncated (max_output_tokens)")


def test_network_failure_and_non_json_success_are_error_strings(monkeypatch):
    from nymeria.tools import web

    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("dns failed")

    monkeypatch.setattr(httpx, "Client", BoomClient)
    assert web._search_single("q", "fast", 5, 60.0, 2000, "key") == "[Error]: Web search failed: dns failed"

    _install_fake_client(monkeypatch, None, status_code=200)
    assert web._search_single("q", "fast", 5, 60.0, 2000, "key").startswith("[Error]: Web search failed: not json")

    _install_fake_client(monkeypatch, ["not", "a", "dict"], status_code=200)
    assert web._search_single("q", "fast", 5, 60.0, 2000, "key") == "[Error]: Perplexity search failed: unexpected response shape"


def test_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web

    _install_fake_client(monkeypatch, None, status_code=401)

    out = web._search_single("q", "fast", 5, 60.0, 2000, "key")

    assert out == "[Error]: Perplexity API error: 401"


def test_api_error_carries_the_json_message(monkeypatch):
    from nymeria.tools import web

    body = {"error": {"message": 'validation failed: model "sonar-pro" is not supported', "type": "invalid_request"}}
    _install_fake_client(monkeypatch, body, status_code=400)

    out = web._search_single("q", "fast", 5, 60.0, 2000, "key")

    assert out == '[Error]: Perplexity API error: 400: validation failed: model "sonar-pro" is not supported'


def _capture_search_single(monkeypatch):
    """Stub the wire call so tool-level tests can read what depth resolved to."""
    from nymeria.tools import web

    calls = []

    def fake_single(query, model, max_sources, timeout, max_tokens, api_key):
        calls.append(
            {"query": query, "model": model, "max_sources": max_sources, "timeout": timeout, "max_tokens": max_tokens}
        )
        return f"result for {query}"

    monkeypatch.setattr(web, "_get_perplexity_api_key", lambda config=None: "key")
    monkeypatch.setattr(web, "_search_single", fake_single)
    return calls


def _fake_settings(monkeypatch, value):
    import nymeria.config as config

    class FakeSettings:
        perplexity_search_model = value

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())


@pytest.mark.parametrize(
    "depth, preset, timeout, max_tokens",
    [
        ("fast", "fast", 60.0, 2000),
        ("Medium", "medium", 60.0, 2000),
        ("xhigh", "xhigh", 300.0, 4000),
        ("quick", "fast", 60.0, 2000),
        ("standard", "low", 60.0, 2000),
        ("DEEP", "high", 180.0, 4000),
    ],
)
def test_search_depth_names_and_legacy_aliases_resolve_to_presets(monkeypatch, depth, preset, timeout, max_tokens):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)

    assert web.web_search_perplexity.func(query="q", search_depth=depth) == "result for q"
    assert calls[0]["model"] == preset
    assert calls[0]["timeout"] == timeout
    assert calls[0]["max_tokens"] == max_tokens


def test_unknown_search_depth_is_refused_without_a_request(monkeypatch):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)

    out = web.web_search_perplexity.func(query="q", search_depth="thorough")

    assert out.startswith("[Error]: Unknown search_depth 'thorough'")
    assert "fast, low, medium, high, xhigh" in out
    assert calls == []


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("low", "low"),
        ("sonar-pro", "low"),
        ("sonar", "fast"),
        ("sonar-reasoning", "medium"),
        ("sonar-reasoning-pro", "medium"),
        ("sonar-deep-research", "high"),
        ("openai/gpt-5.6-luna", "openai/gpt-5.6-luna"),
        ("", "low"),
        ("   ", "low"),
        (None, "low"),
    ],
)
def test_settings_default_accepts_presets_legacy_ids_and_model_ids(monkeypatch, configured, expected):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)
    _fake_settings(monkeypatch, configured)

    web.web_search_perplexity.func(query="q")

    assert calls[0]["model"] == expected


def test_settings_default_that_is_neither_preset_nor_model_id_is_refused(monkeypatch):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)
    _fake_settings(monkeypatch, "sonar-medium-online")

    out = web.web_search_perplexity.func(query="q")

    assert out.startswith("[Error]: PERPLEXITY_SEARCH_MODEL='sonar-medium-online'")
    assert calls == []


def test_max_sources_is_clamped_and_forwarded(monkeypatch):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)

    web.web_search_perplexity.func(query="q", search_depth="fast", max_sources=40)
    web.web_search_perplexity.func(query="q", search_depth="fast")

    assert [c["max_sources"] for c in calls] == [10, 5]


def test_batch_mode_runs_one_request_per_query(monkeypatch):
    from nymeria.tools import web

    calls = _capture_search_single(monkeypatch)

    out = web.web_search_perplexity.func(queries="alpha | beta", search_depth="fast")

    assert [c["query"] for c in calls] == ["alpha", "beta"]
    assert "=== Query 1/2: alpha ===" in out
    assert "=== Query 2/2: beta ===" in out


def test_settings_default_preset_is_low():
    from nymeria.config.settings import Settings

    assert Settings.model_fields["perplexity_search_model"].default == "low"


def test_registered_under_new_name_in_optional_group():
    from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
    from nymeria.tools.web import WEB_SEARCH_SERVICE_TOOLS

    # New name is an opt-in optional tool, not core.
    assert "web_search_perplexity" in CATALOG_TOOLS
    assert [t.name for t in WEB_SEARCH_SERVICE_TOOLS] == ["web_search_perplexity"]

    # The old name is gone everywhere.
    assert "web_search" not in CATALOG_TOOLS
    assert all(t.name not in ("web_search",) for t in SEED_TOOLS)


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
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_tavily" in CATALOG_TOOLS
    assert "web_search_tavily" in [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS]


# --- Exa (web_search_exa_ai) ------------------------------------------------------


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

    result = wsi.web_search_exa_ai.func(query="hello")

    assert result.startswith("[Error]")
    assert "request_credential" in result
    assert "native_tool:web_search_exa_ai" in result


def test_exa_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_exa_ai.func() == (
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

    out = wsi.web_search_exa_ai.func(query="neural retrieval", num_results=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert "body" in out


def test_exa_builds_payload_with_filters(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"results": []}, captured))

    wsi.web_search_exa_ai.func(
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

    wsi.web_search_exa_ai.func(query="q", search_type="turbo")

    assert "type" not in captured["json"]


def test_exa_company_category_drops_date_and_exclude(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_exa_api_key", lambda config=None: "key")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_client({"results": []}, captured))

    wsi.web_search_exa_ai.func(
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
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_exa_ai" in CATALOG_TOOLS
    assert "web_search_exa_ai" in [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS]


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
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_firecrawl" in CATALOG_TOOLS
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
    from nymeria.tools import CATALOG_TOOLS

    assert "web_search_brave" in CATALOG_TOOLS


# --- SearXNG ---------------------------------------------------------------


def _allowlist_policy(monkeypatch, *hosts):
    """Point the egress policy at an explicit allowlist, with DNS off.

    Hermetic on purpose: an allowlist match short-circuits before resolution
    (``http_policy.evaluate_http_url``), so nothing here touches the network.
    """
    from nymeria.core import http_policy

    monkeypatch.setattr(
        http_policy,
        "load_http_policy_config",
        lambda: http_policy.HTTPPolicyConfig(internal_allowlist=hosts, resolve_dns=False),
    )


def test_searxng_base_url_prefers_vault_over_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "http://vault-searx:8080"
        credential_id = "c1"
        field_name = "base_url"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())
    # A vault-supplied address is screened against the egress policy, so a
    # self-hosted instance now needs an HTTP_INTERNAL_ALLOWLIST entry to win.
    # That is the documented capability change, not an accident of the fixture.
    _allowlist_policy(monkeypatch, "vault-searx:8080")

    assert wsi._get_searxng_base_url(config=None) == "http://vault-searx:8080"


def test_searxng_refused_vault_address_falls_through_to_settings(monkeypatch):
    """A planted vault address must be inert, never a denial of service.

    Any identified caller can POST a ``searxng`` credential. If the screen
    raised instead of falling through, one such record would break the tool for
    every user of the deployment even though the operator's own sidecar is
    configured and reachable.
    """
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    class PlantedCred:
        value = "http://127.0.0.1:8000"
        credential_id = "c1"
        field_name = "base_url"

    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: PlantedCred())
    _allowlist_policy(monkeypatch)

    class FakeSettings:
        searxng_base_url = "http://settings-searx:8080"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_searxng_base_url(config=None) == "http://settings-searx:8080"


def test_searxng_base_url_falls_back_to_settings(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        searxng_base_url = "http://settings-searx:8080"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert wsi._get_searxng_base_url(config=None) == "http://settings-searx:8080"


def test_searxng_missing_config_returns_setup_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: None)

    result = wsi.web_search_searxng.func(query="hello")

    assert result.startswith("[Error]")
    assert "SEARXNG_BASE_URL" in result
    assert "native_tool:web_search_searxng" in result
    assert "search.formats" in result


def test_searxng_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_searxng.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_searxng_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    # Deliberately out of score order: the formatter re-sorts by score desc.
    data = {
        "results": [
            {"title": "Low", "url": "https://low.example", "content": "low body", "category": "general", "score": 0.2},
            {"title": "High", "url": "https://high.example", "content": "high body", "category": "general", "score": 0.9, "publishedDate": "2025-03-01T00:00:00"},
            {"title": "Newsy", "url": "https://news.example", "content": "news body", "category": "news", "score": 0.5},
            {"title": "Sci", "url": "https://sci.example", "content": "sci body", "category": "science", "score": 0.4},
        ]
    }

    out = wsi._format_searxng_results(data, count=5)

    # Sorted by score desc: High (0.9), Newsy (0.5), Sci (0.4), Low (0.2).
    assert "1. High" in out
    assert "https://high.example · 2025-03-01T00:00:00" in out
    assert "high body" in out
    # Non-general categories are tagged; general is left untagged.
    assert "2. Newsy [news]" in out
    assert "3. Sci [science]" in out
    assert "4. Low" in out
    assert "[general]" not in out


def test_searxng_formats_no_results():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_searxng_results({}, count=5) == "[No results]"
    assert wsi._format_searxng_results({"results": []}, count=5) == "[No results]"


def test_searxng_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: "http://searx:8080")
    monkeypatch.setattr(
        httpx,
        "Client",
        _fake_httpx_get_client(
            {"results": [{"title": "Doc", "url": "https://d.example", "content": "body"}]}
        ),
    )

    out = wsi.web_search_searxng.func(query="searxng search", count=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert "body" in out


def test_searxng_builds_params_with_filters(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: "http://searx:8080")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"results": []}, captured))

    wsi.web_search_searxng.func(
        query="anthropic claude",
        count=5,
        time_range="week",
        sources="general, news",
        include_domains="github.com",
    )

    # The search endpoint is base_url + /search.
    assert captured["url"] == "http://searx:8080/search"
    params = captured["params"]
    # format=json, safesearch=1 (moderate), pageno=1 are hard defaults.
    assert params["format"] == "json"
    assert params["safesearch"] == 1
    assert params["pageno"] == 1
    # time_range maps 1:1 to SearXNG's own value; categories from sources.
    assert params["time_range"] == "week"
    assert params["categories"] == "general,news"
    # include_domains becomes a site: operator appended to the query (SearXNG has
    # no native domain filter).
    assert params["q"] == "anthropic claude site:github.com"
    assert captured["headers"]["Accept"] == "application/json"


def test_searxng_site_operators_include_and_exclude(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: "http://searx:8080")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"results": []}, captured))

    wsi.web_search_searxng.func(
        query="rust",
        include_domains="github.com, https://docs.rs/foo",
        exclude_domains="pinterest.com",
    )

    # Multiple includes group with OR; each exclude uses -site: (SearXNG/Google
    # syntax). Hosts are reduced to bare hostnames (https://docs.rs/foo -> docs.rs).
    assert captured["params"]["q"] == "rust (site:github.com OR site:docs.rs) -site:pinterest.com"


def test_searxng_invalid_source_is_ignored(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: "http://searx:8080")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"results": []}, captured))

    # "videos" is not an exposed category and is dropped; only news survives.
    wsi.web_search_searxng.func(query="q", sources="videos, news")

    assert captured["params"]["categories"] == "news"


def test_searxng_defaults_to_general_category(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: "http://searx:8080")
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _capturing_httpx_get_client({"results": []}, captured))

    wsi.web_search_searxng.func(query="q")

    assert captured["params"]["categories"] == "general"


def test_searxng_json_disabled_returns_hint(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    out = wsi._searxng_search_single("http://searx:8080", {"q": "x"}, 20.0, 5)

    assert out.startswith("[Error]")
    assert "JSON" in out
    assert "search.formats" in out


def test_searxng_api_error_is_returned_as_error_string(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    import httpx

    class FakeResponse:
        status_code = 503

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

    out = wsi._searxng_search_single("http://searx:8080", {"q": "q"}, 20.0, 5)

    assert out.startswith("[Error]: SearXNG API error: 503")


# --- SearXNG failure honesty (#296) -------------------------------------------
#
# A SearXNG page whose every engine failed is still HTTP 200 with an empty
# results list; the failures ride in "unresponsive_engines" as [name, reason]
# pairs (measured against the pinned sidecar image, 2026-10-02). The tool used
# to call that "[No results]", which every [Error]-prefix consumer (ordered
# batches, /prune, workflow verbs, trigger notify) reads as success. Skipped on
# purpose: the [Error] consumers themselves (their own tests pin the prefix).

# Carries credentials and a distinctive host so a leak of either is visible.
_SECRET_BASE = "http://searx-user:s3cret-pw@searx-internal.example:8080"
_TAIL_PIECES = ("another web_search_* tool", "tell the user", "nymeria doctor")


def _searxng_http_client(respond, captured=None):
    """FakeClient whose GET answers with ``respond(url, params)``.

    ``respond`` returns a REAL ``httpx.Response`` (so ``raise_for_status`` and
    ``json`` behave exactly as on the wire, including an HTTPStatusError whose
    text names the URL) or an exception to raise from the network boundary.
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            if captured is not None:
                captured.append((url, dict(params or {})))
            outcome = respond(url, params)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    return FakeClient


def _page(payload=None, *, status=200, text=None):
    def respond(url, params):
        request = httpx.Request("GET", url, params=params)
        if text is not None:
            return httpx.Response(status, text=text, request=request)
        return httpx.Response(status, json=payload, request=request)

    return respond


def _raise(exc):
    return lambda url, params: exc


def _assert_backend_failure(out: str) -> None:
    assert out.startswith("[Error]: SearXNG")
    for piece in _TAIL_PIECES:
        assert piece in out
    assert "s3cret" not in out
    assert "searx-internal" not in out
    assert "searx-user" not in out


_ENGINES_FAILED = {
    "query": "wikipedia",
    "results": [],
    "answers": [],
    "unresponsive_engines": [["brave", "too many requests"], ["google", "CAPTCHA"]],
}


def test_searxng_all_engines_failed_page_is_an_error_naming_them(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_page(_ENGINES_FAILED)))

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "wikipedia"}, 20.0, 5)

    assert out.startswith("[Error]: SearXNG returned no results")
    assert "brave (too many requests), google (CAPTCHA)" in out
    # Says the empty page is a backend failure, not an answer about the query.
    assert "not that nothing matches the query" in out
    assert "[No results]" not in out
    _assert_backend_failure(out)


def test_searxng_all_engines_failed_end_to_end_through_the_tool(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: _SECRET_BASE)
    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_page(_ENGINES_FAILED)))

    out = wsi.web_search_searxng.func(query="wikipedia")

    assert out.startswith("[Error]: SearXNG returned no results")
    assert "brave (too many requests), google (CAPTCHA)" in out
    _assert_backend_failure(out)


@pytest.mark.parametrize(
    "payload",
    [{}, {"results": []}, {"results": [], "unresponsive_engines": []}],
    ids=["no-fields", "empty-results", "empty-engines"],
)
def test_searxng_genuinely_empty_page_stays_no_results(monkeypatch, payload):
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_searxng_results(payload, count=5) == "[No results]"
    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: _SECRET_BASE)
    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_page(payload)))
    assert wsi.web_search_searxng.func(query="q") == "[No results]"


def test_searxng_partial_engine_failure_with_results_adds_no_note():
    from nymeria.tools import web_search_integrations as wsi

    results = [
        {"title": "Doc", "url": "https://d.example", "content": "body", "score": 1.0},
        {"title": "Other", "url": "https://o.example", "content": "more", "score": 0.5},
    ]
    with_failures = {
        "results": results,
        "unresponsive_engines": [["google", "CAPTCHA"], ["brave", "timeout"]],
    }

    out = wsi._format_searxng_results(with_failures, count=5)

    assert out == wsi._format_searxng_results({"results": results}, count=5)
    assert "1. Doc" in out and "2. Other" in out
    assert "google" not in out and "[Error]" not in out


def test_searxng_suspended_engines_keep_their_reasons():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "results": [],
        "unresponsive_engines": [
            ["duckduckgo", "Suspended: HTTP connection error"],
            ["startpage", "Suspended: access denied"],
        ],
    }

    out = wsi._format_searxng_results(data, count=5)

    assert out.startswith("[Error]: SearXNG returned no results")
    assert (
        "duckduckgo (Suspended: HTTP connection error), "
        "startpage (Suspended: access denied)" in out
    )


@pytest.mark.parametrize(
    "field",
    [
        "brave",
        {"brave": "timeout"},
        [["brave"]],
        [["brave", "timeout", "extra"]],
        [[1, "timeout"]],
        [["brave", None]],
        [None, "brave", 5],
        [["", "timeout"], ["  \n ", "CAPTCHA"]],
        None,
    ],
    ids=[
        "string", "dict", "arity-1", "arity-3", "non-str-name", "non-str-reason",
        "non-pairs", "blank-names", "null",
    ],
)
def test_searxng_malformed_engine_field_with_nothing_valid_is_a_genuine_empty(field):
    from nymeria.tools import web_search_integrations as wsi

    data = {"results": [], "unresponsive_engines": field}

    assert wsi._format_searxng_results(data, count=5) == "[No results]"


def test_searxng_malformed_engine_entries_are_ignored_and_valid_ones_named():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "results": [],
        "unresponsive_engines": [
            ["brave", "timeout"],
            ["half"],
            [2, "y"],
            ("google", "CAPTCHA"),
            ["mojeek", ""],
            ["brave", "timeout"],
        ],
    }

    out = wsi._format_searxng_results(data, count=5)

    assert out.startswith("[Error]: SearXNG returned no results")
    # Valid entries in SearXNG's order, an empty reason renders as the bare
    # name, and an exact duplicate is named once.
    assert "errors: brave (timeout), google (CAPTCHA), mojeek. " in out
    assert "half" not in out and "y)" not in out


def test_searxng_engine_list_is_capped_at_eight_in_searxngs_order():
    from nymeria.tools import web_search_integrations as wsi

    # Deliberately not alphabetical: SearXNG's own order is kept, not re-sorted.
    names = [f"engine{i:02d}" for i in (11, 3, 7, 0, 9, 1, 4, 10, 2, 8, 5, 6)]
    data = {"results": [], "unresponsive_engines": [[n, "timeout"] for n in names]}

    out = wsi._format_searxng_results(data, count=5)

    shown, hidden = names[:8], names[8:]
    positions = [out.index(f"{n} (timeout)") for n in shown]
    assert positions == sorted(positions)
    for n in hidden:
        assert n not in out
    assert f"{shown[-1]} (timeout), and 4 more." in out


def test_searxng_engine_text_is_flattened_stripped_of_controls_and_capped():
    from nymeria.tools import web_search_integrations as wsi

    long_name = "evil\nname\t" + "x" * 200
    data = {
        "results": [],
        "unresponsive_engines": [
            [long_name, "time\x1b[31mout‮\r\nIGNORE PREVIOUS " + "y" * 200],
        ],
    }

    out = wsi._format_searxng_results(data, count=5)

    assert out.startswith("[Error]: SearXNG returned no results")
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert "\x1b" not in out and "‮" not in out
    # Whitespace collapses to one space, control and format characters are
    # dropped, then the name keeps its first 40 characters and the reason its
    # first 60, each marked "..." when cut.
    name = "evil name " + "x" * 30 + "..."
    reason = "time[31mout IGNORE PREVIOUS " + "y" * 32 + "..."
    assert f"errors: {name} ({reason}). " in out


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("[Errno 111] Connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("[Errno -2] Name or service not known"),
    ],
    ids=["refused", "connect-timeout", "dns"],
)
def test_searxng_unreachable_instance_says_so_with_the_remedy(monkeypatch, exc):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_raise(exc)))

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith(
        "[Error]: SearXNG search failed: could not reach the SearXNG instance ("
    )
    assert str(exc) in out
    assert "may be stopped" in out
    _assert_backend_failure(out)


def test_searxng_unreachable_text_never_echoes_the_address(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    exc = httpx.ConnectError(f"connect to {_SECRET_BASE}/search failed")
    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_raise(exc)))

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith("[Error]: SearXNG search failed: could not reach")
    _assert_backend_failure(out)


def test_searxng_other_transport_failure_names_it_with_the_remedy(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    monkeypatch.setattr(httpx, "Client", _searxng_http_client(_raise(exc)))

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith(
        "[Error]: SearXNG search failed: Server disconnected without sending a "
        "response. Use another web_search_* tool"
    )
    _assert_backend_failure(out)


def test_searxng_slow_instance_is_a_timeout_not_unreachable(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(
        httpx, "Client", _searxng_http_client(_raise(httpx.ReadTimeout("timed out")))
    )

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith(
        "[Error]: SearXNG search failed: the SearXNG instance did not answer in time"
    )
    assert "could not reach" not in out
    _assert_backend_failure(out)


@pytest.mark.parametrize(
    "status, hint",
    [
        (403, "search.formats"),
        (429, "server.limiter: false"),
        (503, None),
        (500, None),
    ],
)
def test_searxng_http_errors_keep_the_prefix_and_carry_their_hint(monkeypatch, status, hint):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(
        httpx, "Client", _searxng_http_client(_page({"error": "x"}, status=status))
    )

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith(f"[Error]: SearXNG API error: {status}.")
    if hint is not None:
        assert hint in out
    else:
        assert "search.formats" not in out and "limiter" not in out
    _assert_backend_failure(out)


@pytest.mark.parametrize(
    "respond",
    [_page(text="<html><body>login</body></html>"), _page(["not", "an", "object"])],
    ids=["html", "json-list"],
)
def test_searxng_non_searxng_answer_is_a_json_error_not_a_crash(monkeypatch, respond):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(httpx, "Client", _searxng_http_client(respond))

    out = wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    assert out.startswith("[Error]: SearXNG did not return a JSON search response.")
    assert "search.formats" in out
    assert "object has no attribute" not in out
    _assert_backend_failure(out)


def test_searxng_non_object_result_items_are_skipped():
    from nymeria.tools import web_search_integrations as wsi

    data = {
        "results": [
            "junk",
            {"title": "Doc", "url": "https://d.example", "content": "body"},
            None,
        ]
    }

    out = wsi._format_searxng_results(data, count=5)

    assert out == "1. Doc\n   https://d.example\n   body"
    assert wsi._format_searxng_results({"results": "junk"}, count=5) == "[No results]"


def test_searxng_batch_keeps_a_failed_query_in_its_own_section(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    good = {"results": [{"title": "Doc", "url": "https://d.example", "content": "body"}]}

    def respond(url, params):
        payload = _ENGINES_FAILED if params["q"] == "a" else good
        return _page(payload)(url, params)

    monkeypatch.setattr(wsi, "_get_searxng_base_url", lambda config=None: _SECRET_BASE)
    monkeypatch.setattr(httpx, "Client", _searxng_http_client(respond))

    out = wsi.web_search_searxng.func(queries="a | b")

    first, second = out.split("\n\n=== Query 2/2: b ===\n")
    assert first.startswith("=== Query 1/2: a ===\n[Error]: SearXNG returned no results")
    assert "brave (too many requests)" in first
    assert second == "1. Doc\n   https://d.example\n   body"


def test_searxng_failures_are_logged_for_the_operator(monkeypatch, caplog):
    import logging

    from nymeria.tools import web_search_integrations as wsi

    logger_name = wsi.logger.name
    caplog.set_level(logging.INFO, logger=logger_name)

    # Every engine failed: a WARNING naming them (the ops visibility the mates
    # incident lacked).
    wsi._format_searxng_results(_ENGINES_FAILED, count=5)
    # A partial failure that still has results: one INFO line, no warning.
    wsi._format_searxng_results(
        {
            "results": [{"title": "Doc", "url": "https://d.example"}],
            "unresponsive_engines": [["wikidata", "timeout"]],
        },
        count=5,
    )
    # A refused connection: a WARNING without a traceback.
    monkeypatch.setattr(
        httpx,
        "Client",
        _searxng_http_client(_raise(httpx.ConnectError("[Errno 111] Connection refused"))),
    )
    wsi._searxng_search_single(_SECRET_BASE, {"q": "q"}, 20.0, 5)

    records = [r for r in caplog.records if r.name == logger_name]
    engines = [r for r in records if "brave (too many requests)" in r.getMessage()]
    assert [r.levelno for r in engines] == [logging.WARNING]
    partial = [r for r in records if "wikidata (timeout)" in r.getMessage()]
    assert [r.levelno for r in partial] == [logging.INFO]
    refused = [r for r in records if "Connection refused" in r.getMessage()]
    assert [r.levelno for r in refused] == [logging.WARNING]
    assert refused[0].exc_info is None
    assert all("s3cret" not in r.getMessage() for r in records)


def test_searxng_registered_in_optional_group():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.web_search_integrations import WEB_SEARCH_INTEGRATION_TOOLS

    assert "web_search_searxng" in CATALOG_TOOLS
    # The old utility-group searxng_search was replaced by this family member.
    assert "searxng_search" not in CATALOG_TOOLS
    assert [t.name for t in WEB_SEARCH_INTEGRATION_TOOLS] == [
        "web_search_tavily",
        "web_search_exa_ai",
        "web_search_firecrawl",
        "web_search_brave",
        "web_search_searxng",
        "web_search_ddgs",
    ]


# ---------------------------------------------------------------------------
# web_search_ddgs (keyless in-process metasearch)
# ---------------------------------------------------------------------------


def _fake_ddgs(results=None, raises=None, captured=None):
    """Build a stand-in for ddgs.DDGS.

    Records constructor and search kwargs into ``captured``, raises the queued
    exceptions one per call (None = succeed), then returns ``results``.
    """
    captured = captured if captured is not None else {}
    raise_queue = list(raises or [])

    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            captured["init"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, **kwargs):
            return self._run("text", query, kwargs)

        def news(self, query, **kwargs):
            return self._run("news", query, kwargs)

        def _run(self, category, query, kwargs):
            captured["category"] = category
            captured["query"] = query
            captured["kwargs"] = kwargs
            captured["calls"] = captured.get("calls", 0) + 1
            if raise_queue:
                exc = raise_queue.pop(0)
                if exc is not None:
                    raise exc
            return list(results or [])

    return FakeDDGS


def test_ddgs_requires_a_query():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi.web_search_ddgs.func() == (
        "[Error]: Provide a query or pipe-separated queries."
    )


def test_ddgs_formats_ranked_sources():
    from nymeria.tools import web_search_integrations as wsi

    items = [
        {"title": "Doc one", "href": "https://a.example", "body": "first snippet"},
        {"title": "", "href": "https://b.example", "body": ""},
        {"title": "Doc three", "href": "https://c.example", "body": "third"},
    ]

    out = wsi._format_ddgs_results(items, count=2)

    # count caps the output; the third item is dropped.
    assert "1. Doc one" in out
    assert "https://a.example" in out
    assert "first snippet" in out
    assert "2. (untitled)" in out
    assert "Doc three" not in out


def test_ddgs_formats_news_fields():
    from nymeria.tools import web_search_integrations as wsi

    items = [
        {
            "title": "Headline",
            "url": "https://news.example/story",
            "body": "summary",
            "source": "Example Wire",
            "date": "2026-06-12",
        }
    ]

    out = wsi._format_ddgs_results(items, count=5)

    # News items use "url" instead of "href" and add source/date metadata.
    assert "1. Headline" in out
    assert "https://news.example/story · Example Wire · 2026-06-12" in out
    assert "summary" in out


def test_ddgs_formats_no_results():
    from nymeria.tools import web_search_integrations as wsi

    assert wsi._format_ddgs_results([], count=5) == "[No results]"


def test_ddgs_single_query_end_to_end(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    captured: dict = {}
    fake = _fake_ddgs(
        results=[{"title": "Doc", "href": "https://d.example", "body": "body"}],
        captured=captured,
    )
    monkeypatch.setattr("ddgs.DDGS", fake)

    out = wsi.web_search_ddgs.func(query="ddgs search", count=3)

    assert "1. Doc" in out
    assert "https://d.example" in out
    assert captured["category"] == "text"
    assert captured["query"] == "ddgs search"
    # safesearch is a hard default; count flows through as max_results.
    assert captured["kwargs"]["safesearch"] == "moderate"
    assert captured["kwargs"]["max_results"] == 3
    assert captured["kwargs"]["timelimit"] is None


def test_ddgs_time_range_maps_to_timelimit(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    captured: dict = {}
    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(captured=captured))

    wsi.web_search_ddgs.func(query="q", time_range="week")

    # The family's day/week/month/year maps to ddgs single-letter timelimit.
    assert captured["kwargs"]["timelimit"] == "w"


def test_ddgs_news_category_routes_to_news(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    captured: dict = {}
    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(captured=captured))

    # "videos" is not an exposed category and is skipped; news wins.
    wsi.web_search_ddgs.func(query="q", sources="videos, news")

    assert captured["category"] == "news"


def test_ddgs_defaults_to_general_text(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    captured: dict = {}
    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(captured=captured))

    wsi.web_search_ddgs.func(query="q")

    assert captured["category"] == "text"


def test_ddgs_site_operators_include_and_exclude(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    captured: dict = {}
    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(captured=captured))

    wsi.web_search_ddgs.func(
        query="rust",
        include_domains="github.com, https://docs.rs/foo",
        exclude_domains="pinterest.com",
    )

    # Same Google-style operator synthesis as SearXNG (the upstream engines
    # understand site:); hosts reduce to bare hostnames.
    assert captured["query"] == "rust (site:github.com OR site:docs.rs) -site:pinterest.com"


def test_ddgs_rate_limit_returns_soft_error_without_retry(monkeypatch):
    # ddgs 9.14.x defines RatelimitException but never raises it (per-engine
    # failures are swallowed; blanket failures surface as DDGSException), so
    # the tool deliberately has no retry: any rate-limit-shaped exception is
    # a single soft error the agent can route around.
    from nymeria.tools import web_search_integrations as wsi
    from ddgs.exceptions import RatelimitException

    captured: dict = {}
    fake = _fake_ddgs(raises=[RatelimitException("slow down")], captured=captured)
    monkeypatch.setattr("ddgs.DDGS", fake)

    out = wsi.web_search_ddgs.func(query="q")

    assert out.startswith("[Error]: ddgs metasearch failed: slow down")
    assert captured["calls"] == 1


def test_ddgs_no_results_exception_returns_no_results(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    from ddgs.exceptions import DDGSException

    # The library raises instead of returning [] when every engine came back
    # empty; the tool reports that as a normal empty result, not an error.
    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(raises=[DDGSException("No results found.")]))

    assert wsi.web_search_ddgs.func(query="q") == "[No results]"


def test_ddgs_engine_failure_returns_soft_error(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi
    from ddgs.exceptions import DDGSException

    monkeypatch.setattr("ddgs.DDGS", _fake_ddgs(raises=[DDGSException("boom")]))

    out = wsi.web_search_ddgs.func(query="q")

    assert out.startswith("[Error]: ddgs metasearch failed: boom")


def test_ddgs_batch_mode_sections(monkeypatch):
    from nymeria.tools import web_search_integrations as wsi

    monkeypatch.setattr(
        "ddgs.DDGS",
        _fake_ddgs(results=[{"title": "Doc", "href": "https://d.example", "body": "b"}]),
    )

    out = wsi.web_search_ddgs.func(queries="alpha | beta")

    assert "=== Query 1/2: alpha ===" in out
    assert "=== Query 2/2: beta ===" in out


def test_ddgs_registered_in_optional_group():
    from nymeria.tools import CATALOG_TOOLS, SEED_TOOLS

    assert "web_search_ddgs" in CATALOG_TOOLS
    # Keyless does not mean default-on: quickstart opts it in per user, the
    # catalog itself stays opt-in.
    assert all(t.name != "web_search_ddgs" for t in SEED_TOOLS)


def test_build_site_filter_uses_per_engine_exclude_prefix():
    from nymeria.tools import web_search_integrations as wsi

    # One shared helper, only the exclude prefix differs per engine: Brave uses
    # "NOT site:", SearXNG and the ddgs metasearch use "-site:".
    assert (
        wsi._build_site_filter(
            "github.com, docs.rs", "pinterest.com", exclude_prefix="NOT site:"
        )
        == "(site:github.com OR site:docs.rs) NOT site:pinterest.com"
    )
    assert (
        wsi._build_site_filter(
            "github.com, docs.rs", "pinterest.com", exclude_prefix="-site:"
        )
        == "(site:github.com OR site:docs.rs) -site:pinterest.com"
    )
    # Single include drops the OR-grouping; empty inputs yield an empty filter.
    assert (
        wsi._build_site_filter("github.com", "", exclude_prefix="-site:")
        == "site:github.com"
    )
    assert wsi._build_site_filter("", "", exclude_prefix="-site:") == ""
