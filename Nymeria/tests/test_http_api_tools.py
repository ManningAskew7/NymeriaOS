"""Tests for general-purpose HTTP/API primitive tools."""

from __future__ import annotations

import json
import asyncio

import httpx

from nymeria.core.custom_tools import (
    execute_http_tool,
    _sync_execute_http,
    _sync_http_request,
    _extract_json_path,
    _PATH_NOT_FOUND,
)
from nymeria.core.http_policy import HTTPPolicyConfig, evaluate_http_url
from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
from nymeria.tools.http_api import TOOL_VERSION, _api_discover_impl, _http_request_impl
from nymeria.tools.definitions.custom_tool_schema import HTTPToolConfig
from nymeria.tools.metadata import SecurityLevel, get_all_tool_metadata


PUBLIC_TEST_POLICY = HTTPPolicyConfig(resolve_dns=False)


def test_http_request_success_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.params["q"] == "nymeria"
        assert json.loads(request.content) == {"name": "test"}
        return httpx.Response(
            201,
            json={"id": "item-1"},
            headers={"content-type": "application/json"},
            request=request,
        )

    result = _http_request_impl(
        method="POST",
        url="https://api.example.com/items",
        headers={"X-Test": "1"},
        query={"q": "nymeria"},
        body={"name": "test"},
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["tool_version"] == TOOL_VERSION
    assert result["http_ok"] is True
    assert result["format_ok"] is True
    assert result["response"]["status_code"] == 201
    assert result["body_type"] == "json"
    assert result["body_format"] == "json"
    assert result["json_parse_ok"] is True
    assert result["body"] == {"id": "item-1"}


def test_http_request_non_success_includes_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            text="missing",
            headers={"content-type": "text/plain"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/missing",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is False
    assert result["response"]["status_code"] == 404
    assert result["error"]["type"] == "http_status"
    assert result["body"] == "missing"


def test_http_request_redirect_status_is_classified_separately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            text="redirecting",
            headers={"location": "/next"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/start",
        follow_redirects=False,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is False
    assert result["format_ok"] is True
    assert result["response"]["headers"]["location"] == "/next"
    assert result["error"]["type"] == "http_redirect"
    assert result["error"]["location"] == "/next"
    assert result["error"]["redirect_url"] == "https://api.example.com/next"


def test_http_request_text_truncation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="abcdefghijklmnopqrstuvwxyz",
            headers={"content-type": "text/plain"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/large",
        max_response_chars=5,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["body_truncated"] is True
    assert result["body"] is None
    assert result["body_preview"].startswith("abcde")


def test_http_request_forced_json_parse_error_marks_format_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>not json</html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/page",
        response_format="json",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is True
    assert result["format_ok"] is False
    assert result["json_parse_ok"] is False
    assert result["error"]["type"] == "response_parse_error"
    assert result["parse_error"].startswith("JSON parse failed")


def test_http_request_truncated_json_uses_preview_without_changing_body_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": list(range(50))},
            headers={"content-type": "application/json"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/items",
        max_response_chars=20,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["body_type"] == "json"
    assert result["body_format"] == "json"
    assert result["json_parse_ok"] is True
    assert result["body_truncated"] is True
    assert result["body"] is None
    assert result["body_preview"].startswith("{")


def test_http_request_timeout_error_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/slow",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["tool_version"] == TOOL_VERSION
    assert result["http_ok"] is None
    assert result["format_ok"] is None
    assert result["error"]["type"] == "ConnectTimeout"
    assert "timed out" in result["error"]["message"]


def test_http_request_too_many_redirects_error_shape():
    """The ``_HTTPTooManyRedirects`` arm reports a ``TooManyRedirects`` error
    and attaches the redirect chain accumulated before bailing."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Always bounce onward so the follower exceeds max_redirects.
        return httpx.Response(
            302,
            headers={"location": "https://api.example.com/next"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/start",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
        policy_config=HTTPPolicyConfig(resolve_dns=False, max_redirects=1),
    )

    assert result["ok"] is False
    assert result["http_ok"] is None
    assert result["error"]["type"] == "TooManyRedirects"
    assert "redirect count" in result["error"]["message"]
    assert isinstance(result["redirect_chain"], list)
    assert result["redirect_chain"]  # at least one hop recorded before bailing


def test_http_request_connect_error_reports_exception_type():
    """A non-timeout ``httpx.RequestError`` flows through the merged
    transport-error arm and reports its concrete class name and message."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/down",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is None
    assert result["error"]["type"] == "ConnectError"
    assert result["error"]["message"] == "connection refused"


def test_http_request_unexpected_error_is_caught_and_shaped():
    """A non-httpx exception hits the generic arm: it is shaped as an error
    result (and audited/redacted via the shared finisher) rather than
    propagating out of the tool."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("kaboom")

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/boom",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is None
    assert result["error"]["type"] == "RuntimeError"
    assert result["error"]["message"] == "kaboom"


def test_http_request_error_path_redacts_sensitive_values():
    """The consolidated error finisher redacts the returned result, so secrets
    surfaced in a transport error message or URL are scrubbed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("auth failed for token=SECRET123", request=request)

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/x?token=SECRET123",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
        redact_values=["SECRET123"],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "ConnectError"
    assert "SECRET123" not in json.dumps(result)
    assert "[redacted]" in result["error"]["message"]


def test_http_request_validation_error_has_consistent_fields():
    result = _http_request_impl(
        method="GET",
        url="not-a-url",
    )

    assert result["ok"] is False
    assert result["tool_version"] == TOOL_VERSION
    assert result["http_ok"] is None
    assert result["format_ok"] is None
    assert result["error"]["type"] == "validation_error"


def test_http_request_binary_body_is_marked_omitted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/file.pdf",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["body_type"] == "binary"
    assert result["body"] is None
    assert result["body_omitted"] is True
    assert "Binary response omitted" in result["body_preview"]


def test_api_discover_finds_openapi_json():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "info": {"title": "Example API", "version": "1.0"},
                    "servers": [{"url": "https://api.example.com"}],
                    "paths": {
                        "/items": {
                            "get": {"summary": "List items"},
                            "post": {"summary": "Create item"},
                        }
                    },
                },
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(404, text="not found", request=request)

    result = _api_discover_impl(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["tool_version"] == TOOL_VERSION
    assert result["found"] is True
    assert result["spec_url"] == "https://api.example.com/openapi.json"
    assert result["summary"]["title"] == "Example API"
    assert result["summary"]["path_count"] == 1
    assert result["summary"]["sample_paths"][0]["methods"] == ["GET", "POST"]


def test_api_discover_finds_openapi_yaml():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.yaml":
            return httpx.Response(
                200,
                text=(
                    "openapi: 3.0.0\n"
                    "info:\n"
                    "  title: YAML API\n"
                    "  version: '1'\n"
                    "paths:\n"
                    "  /status:\n"
                    "    get:\n"
                    "      summary: Status\n"
                ),
                headers={"content-type": "application/yaml"},
                request=request,
            )
        return httpx.Response(404, text="not found", request=request)

    result = _api_discover_impl(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["found"] is True
    assert result["spec_format"] == "yaml"
    assert result["summary"]["title"] == "YAML API"


def test_api_discover_extracts_swagger_ui_initializer_spec_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    '<script src="./swagger-ui-bundle.js"></script>'
                    '<script src="./swagger-initializer.js"></script>'
                    "</body></html>"
                ),
                headers={"content-type": "text/html"},
                request=request,
            )
        if request.url.path == "/swagger-initializer.js":
            return httpx.Response(
                200,
                text=(
                    "const defaultDefinitionUrl = \"https://other.example.com/swagger.json\";\n"
                    "const ossServices = `api.example.com=https://api.example.com/api/v3/openapi.json`;\n"
                    "window.ui = SwaggerUIBundle({ url: definitionURL });"
                ),
                headers={"content-type": "application/javascript"},
                request=request,
            )
        if request.url.path == "/api/v3/openapi.json":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.0.0",
                    "info": {"title": "Swagger UI API", "version": "1"},
                    "paths": {"/status": {"get": {"summary": "Status"}}},
                },
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(404, text="not found", request=request)

    result = _api_discover_impl(
        base_url="https://api.example.com",
        docs_url="https://api.example.com/",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is True
    assert result["found"] is True
    assert result["spec_url"] == "https://api.example.com/api/v3/openapi.json"
    assert result["summary"]["title"] == "Swagger UI API"
    assert any(
        row["url"] == "https://api.example.com/swagger-initializer.js"
        and row["result"] == "script_spec_links_found"
        for row in result["tried"]
    )


def test_http_policy_blocks_private_dns_resolution():
    decision = evaluate_http_url(
        "https://example.test/data",
        config=HTTPPolicyConfig(),
        resolver=lambda host, port: ["10.0.0.5"],
    )

    assert decision.allowed is False
    assert decision.reason == "private_network"
    assert decision.resolved_ips == ("10.0.0.5",)


def test_http_request_blocks_loopback_before_transport_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked request should not hit transport")

    result = _http_request_impl(
        method="GET",
        url="http://127.0.0.1:8000/admin",
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["http_ok"] is None
    assert result["format_ok"] is None
    assert result["error"]["type"] == "blocked_network_target"
    assert result["policy"]["reason"] == "loopback_network"


def test_http_request_allows_explicit_internal_host_allowlist():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    result = _http_request_impl(
        method="GET",
        url="http://127.0.0.1:8000/health",
        transport=httpx.MockTransport(handler),
        policy_config=HTTPPolicyConfig(
            internal_allowlist=("127.0.0.1:8000",),
            resolve_dns=False,
        ),
    )

    assert result["ok"] is True
    assert result["policy"]["reason"] == "internal_allowlist"


def test_http_request_blocks_redirect_to_private_target():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://127.0.0.1:8000/admin"},
                request=request,
            )
        raise AssertionError("private redirect target should not be requested")

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/start",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "blocked_network_target"
    assert result["policy"]["reason"] == "loopback_network"
    assert result["redirect_chain"][0]["redirect_url"] == "https://127.0.0.1:8000/admin"


def test_http_request_blocks_https_to_http_redirect_by_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://api.example.com/plain"},
            request=request,
        )

    result = _http_request_impl(
        method="GET",
        url="https://api.example.com/start",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "blocked_network_target"
    assert result["policy"]["reason"] == "https_to_http_redirect"


def test_api_discover_blocks_private_base_url():
    result = _api_discover_impl(
        base_url="http://127.0.0.1:8000",
        policy_config=PUBLIC_TEST_POLICY,
    )

    assert result["ok"] is False
    assert result["found"] is False
    assert result["error"]["type"] == "blocked_network_target"
    assert result["policy"]["reason"] == "loopback_network"


def test_custom_http_tool_uses_shared_policy_for_blocked_targets():
    result = asyncio.run(
        execute_http_tool(
            HTTPToolConfig(method="GET", url="http://127.0.0.1:8000/admin"),
            {},
        )
    )

    assert result.startswith("[Error]: blocked_network_target")


def test_sync_execute_http_runs_without_an_event_loop():
    """The sync tool entry point must work on a worker thread (no event loop).

    Regression for the prior ``asyncio.get_event_loop().run_until_complete`` body,
    which raised ``RuntimeError`` when called from a thread without a running loop
    (as ``asyncio.to_thread`` and the StructuredTool sync path both do).
    """

    async def driver() -> str:
        return await asyncio.to_thread(
            _sync_execute_http,
            HTTPToolConfig(method="GET", url="http://127.0.0.1:8000/admin"),
            {},
        )

    result = asyncio.run(driver())
    assert result.startswith("[Error]: blocked_network_target")


def test_sync_and_async_http_paths_agree():
    """``_sync_execute_http`` and ``execute_http_tool`` share one core, so identical input yields identical output."""
    config = HTTPToolConfig(method="GET", url="http://127.0.0.1:8000/admin")

    sync_result = _sync_execute_http(config, {})
    async_result = asyncio.run(execute_http_tool(config, {}))
    direct_core = _sync_http_request(config, {})

    assert sync_result == async_result == direct_core
    assert sync_result.startswith("[Error]: blocked_network_target")


def test_http_api_tools_are_optional_with_metadata():
    core_names = {tool.name for tool in SEED_TOOLS}

    assert "http_request" not in core_names
    assert "api_discover" not in core_names
    assert "http_request" in CATALOG_TOOLS
    assert "api_discover" in CATALOG_TOOLS

    for tool_name in ("http_request", "api_discover"):
        meta = get_all_tool_metadata(tool_name)
        assert meta is not None
        assert meta.security_level == SecurityLevel.MODERATE
        assert meta.default_enabled is False


# ---------------------------------------------------------------------------
# response_path extraction (`_extract_json_path`)
#
# Regression for the lenient fallback: a misconfigured response_path used to
# silently yield the parent object or the entire response body. It now returns
# the `_PATH_NOT_FOUND` sentinel on any unresolved segment, and the caller
# surfaces an error instead of leaking the unfiltered payload.
# ---------------------------------------------------------------------------


def test_extract_json_path_nested_field():
    assert _extract_json_path({"data": {"temperature": 21}}, "$.data.temperature") == 21


def test_extract_json_path_array_index():
    data = {"items": [{"id": "a"}, {"id": "b"}]}
    assert _extract_json_path(data, "$.items[1]") == {"id": "b"}


def test_extract_json_path_array_wildcard_keeps_full_list():
    assert _extract_json_path({"items": [1, 2, 3]}, "$.items[*]") == [1, 2, 3]


def test_extract_json_path_missing_top_level_field_is_not_found():
    assert _extract_json_path({"a": 1}, "$.b") is _PATH_NOT_FOUND


def test_extract_json_path_missing_nested_field_is_not_found():
    # Previously returned the parent dict ({"b": 1}) rather than signaling a miss.
    assert _extract_json_path({"a": {"b": 1}}, "$.a.c") is _PATH_NOT_FOUND


def test_extract_json_path_traversal_into_non_dict_is_not_found():
    # Previously returned the whole root body.
    assert _extract_json_path({"a": 5}, "$.a.b") is _PATH_NOT_FOUND


def test_extract_json_path_index_out_of_bounds_is_not_found():
    # Previously returned None, which the caller rendered as the literal "None".
    assert _extract_json_path({"items": [1, 2]}, "$.items[5]") is _PATH_NOT_FOUND


def test_extract_json_path_index_on_non_list_is_not_found():
    # Asking for [0] on a dict-valued field is a genuine mismatch.
    assert _extract_json_path({"data": {"x": 1}}, "$.data[0]") is _PATH_NOT_FOUND


def test_extract_json_path_chained_index_is_not_found():
    # The anchored match rejects chained indexing. A prefix match used to accept
    # "items[0][1]" as "items[0]" and silently drop the "[1]".
    assert _extract_json_path({"items": [10, 20]}, "$.items[0][1]") is _PATH_NOT_FOUND


def test_extract_json_path_trailing_chars_after_index_is_not_found():
    # Likewise, trailing characters after a valid index used to be dropped.
    assert _extract_json_path({"items": [10, 20]}, "$.items[0]extra") is _PATH_NOT_FOUND


def test_extract_json_path_non_numeric_index_is_not_found():
    # Always returned not-found (the regex requires a digit or "*"); this pins
    # that it now exits via the malformed-bracket branch.
    assert _extract_json_path({"items": [10, 20]}, "$.items[abc]") is _PATH_NOT_FOUND


def test_extract_json_path_non_dollar_prefix_is_not_found():
    # A malformed path (no leading "$.") is a config error, not "return all".
    assert _extract_json_path({"a": 1}, "data") is _PATH_NOT_FOUND


def test_extract_json_path_present_null_resolves_to_none_not_sentinel():
    # A legitimately-present null is distinct from a path that did not match.
    result = _extract_json_path({"data": None}, "$.data")
    assert result is None
    assert result is not _PATH_NOT_FOUND


# `_sync_http_request` does not plumb a `transport` into `_http_request_impl`,
# so (unlike the `_http_request_impl` tests above) these end-to-end cases stub
# the impl via its function-local import seam rather than `httpx.MockTransport`.
def _fake_http_impl(payload):
    """A drop-in `_http_request_impl` returning a fixed successful body."""

    def _impl(**_kwargs):
        return {"ok": True, "body": payload}

    return _impl


def test_sync_http_request_response_path_match_extracts_value(monkeypatch):
    monkeypatch.setattr(
        "nymeria.tools.http_api._http_request_impl",
        _fake_http_impl({"data": {"temperature": 21}}),
    )
    config = HTTPToolConfig(
        method="GET",
        url="https://api.example.com/weather",
        response_path="$.data.temperature",
    )

    assert _sync_http_request(config, {}) == "21"


def test_sync_http_request_response_path_no_match_returns_error_not_body(monkeypatch):
    payload = {"data": {"temperature": 21}, "secret": "TOPSECRET"}
    monkeypatch.setattr(
        "nymeria.tools.http_api._http_request_impl",
        _fake_http_impl(payload),
    )
    config = HTTPToolConfig(
        method="GET",
        url="https://api.example.com/weather",
        response_path="$.data.humidity",
    )

    result = _sync_http_request(config, {})

    assert result == (
        "[Error]: response_path '$.data.humidity' did not match the response body"
    )
    # The unfiltered body (and any secret inside it) must not leak into the result.
    assert "TOPSECRET" not in result
