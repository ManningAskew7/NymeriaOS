"""Tests for the MCP discovery registry HTTP clients (mcp_registry_client).

Focused on the shared request/error envelope (``_fetch_registry_json``) that
both ``list()`` fetchers and ``fetch_detail`` route through: the per-call error
labels, the 404 special case, JSON parsing, and the per-fetcher caching and
payload mapping that wrap it. The egress-policy gate on construction is covered
separately in test_mcp_runtime.py.
"""

from __future__ import annotations

import pytest

from nymeria.core import mcp_registry_client as rc
from nymeria.core.http_policy import (
    HTTPPolicyDecision,
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
)
from nymeria.core.mcp_registry_client import (
    OfficialMCPRegistryFetcher,
    RegistryError,
    SmitheryFetcher,
    _fetch_registry_json,
)


class FakeSession:
    """Minimal stand-in for requests.Session (Smithery touches .headers)."""

    def __init__(self):
        self.headers: dict = {}


class FakeResp:
    def __init__(self, *, status_code=200, json_data=None, text="", json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._json_data


def _install_fake_get(monkeypatch, responses):
    """Patch requests_get_with_policy to consume ``responses`` in order.

    Each item is either a FakeResp (returned as the (resp, redirect_chain,
    policy) tuple the callers unpack) or an Exception instance (raised). Returns
    a record dict so tests can assert cache short-circuiting (``count``) and the
    kwargs the helper forwarded on the last call (``last``).
    """
    queue = list(responses)
    calls = {"count": 0, "last": None}

    def fake_get(url, *, session=None, params=None, timeout=None, **kwargs):
        calls["count"] += 1
        calls["last"] = {"url": url, "session": session, "params": params, "timeout": timeout}
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, [], None

    monkeypatch.setattr(rc, "requests_get_with_policy", fake_get)
    return calls


@pytest.fixture
def official(monkeypatch):
    # Bypass egress validation (covered separately) so construction is hermetic.
    monkeypatch.setattr(rc, "validate_http_egress_url", lambda url, **kw: url)
    return OfficialMCPRegistryFetcher(
        http_session=FakeSession(), base_url="https://registry.example.com"
    )


# ---- Official list() ----

def test_official_list_maps_and_caches(official, monkeypatch):
    payload = {
        "servers": [
            {
                "server": {
                    "id": "io.x/a",
                    "name": "A",
                    "description": "desc",
                    "packages": [
                        {"registry_type": "npm", "name": "@x/a", "version": "1.0.0"}
                    ],
                }
            }
        ]
    }
    calls = _install_fake_get(monkeypatch, [FakeResp(json_data=payload)])

    entries = official.list("a")
    assert len(entries) == 1
    assert entries[0].id == "io.x/a"
    assert entries[0].source == "official"
    assert entries[0].install_hint == "npx -y @x/a@1.0.0"

    # The helper forwards the caller's params, session, and timeout unchanged.
    assert calls["last"]["params"] == {"limit": 25, "search": "a"}
    assert calls["last"]["timeout"] == 15
    assert calls["last"]["session"] is official._http

    # Second call for the same query is served from the TTL cache (no new fetch).
    again = official.list("a")
    assert again == entries
    assert calls["count"] == 1


def test_official_list_http_error_label(official, monkeypatch):
    _install_fake_get(monkeypatch, [FakeResp(status_code=503, text="boom")])
    with pytest.raises(RegistryError, match="official registry HTTP 503"):
        official.list()


def test_official_list_non_json_label(official, monkeypatch):
    _install_fake_get(monkeypatch, [FakeResp(json_error=True)])
    with pytest.raises(RegistryError, match="official registry returned non-JSON"):
        official.list()


def test_official_list_transport_failure_label(official, monkeypatch):
    _install_fake_get(monkeypatch, [RuntimeError("conn reset")])
    with pytest.raises(RegistryError, match="official registry request failed"):
        official.list()


# ---- Official fetch_detail() ----

def test_fetch_detail_404_label(official, monkeypatch):
    _install_fake_get(monkeypatch, [FakeResp(status_code=404, text="nope")])
    with pytest.raises(
        RegistryError, match="server not found in official registry: io.x/missing"
    ):
        official.fetch_detail("io.x/missing")


def test_fetch_detail_picks_latest_and_caches(official, monkeypatch):
    payload = {
        "servers": [
            {"server": {"id": "io.x/a", "description": "old"}, "_meta": {"isLatest": False}},
            {"server": {"id": "io.x/a", "description": "new"}, "_meta": {"isLatest": True}},
        ]
    }
    calls = _install_fake_get(monkeypatch, [FakeResp(json_data=payload)])

    detail = official.fetch_detail("io.x/a")
    assert detail["description"] == "new"

    again = official.fetch_detail("io.x/a")
    assert again == detail
    assert calls["count"] == 1


def test_fetch_detail_http_error_label(official, monkeypatch):
    # A non-404 HTTP error still uses the shared "official registry HTTP" label.
    _install_fake_get(monkeypatch, [FakeResp(status_code=502, text="bad gw")])
    with pytest.raises(RegistryError, match="official registry HTTP 502"):
        official.fetch_detail("io.x/a")


# ---- Smithery list() ----

def test_smithery_list_maps(monkeypatch):
    fetcher = SmitheryFetcher(http_session=FakeSession())
    payload = {
        "servers": [
            {"qualifiedName": "x/a", "displayName": "A", "description": "d"}
        ]
    }
    _install_fake_get(monkeypatch, [FakeResp(json_data=payload)])

    entries = fetcher.list("q")
    assert len(entries) == 1
    assert entries[0].id == "x/a"
    assert entries[0].source == "smithery"
    assert entries[0].install_hint is None


def test_smithery_list_http_error_label(monkeypatch):
    fetcher = SmitheryFetcher(http_session=FakeSession())
    _install_fake_get(monkeypatch, [FakeResp(status_code=500, text="err")])
    with pytest.raises(RegistryError, match="smithery HTTP 500"):
        fetcher.list()


# ---- Shared helper directly ----

def test_fetch_registry_json_blocked_arm_uses_label(monkeypatch):
    def fake_get(url, **kwargs):
        raise HTTPPolicyRedirectLimit([])

    monkeypatch.setattr(rc, "requests_get_with_policy", fake_get)
    with pytest.raises(RegistryError, match="smithery request blocked"):
        _fetch_registry_json(FakeSession(), "https://x", label="smithery")


def test_fetch_registry_json_violation_arm_uses_label(monkeypatch):
    # The sibling policy exception lands in the same "request blocked" arm.
    decision = HTTPPolicyDecision(allowed=False, reason="blocked for test", target="https://x")

    def fake_get(url, **kwargs):
        raise HTTPPolicyViolation(decision)

    monkeypatch.setattr(rc, "requests_get_with_policy", fake_get)
    with pytest.raises(RegistryError, match="official registry request blocked"):
        _fetch_registry_json(FakeSession(), "https://x", label="official registry")


def test_fetch_registry_json_not_found_only_when_message_supplied(monkeypatch):
    # Without not_found_message, a 404 falls through to the generic HTTP arm
    # (preserving the list() paths, which never special-case 404).
    _install_fake_get(monkeypatch, [FakeResp(status_code=404, text="x")])
    with pytest.raises(RegistryError, match="official registry HTTP 404"):
        _fetch_registry_json(FakeSession(), "https://x", label="official registry")
