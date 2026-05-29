"""Tests for MCP inbound auth and Act-As pinning (C-4)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import nymeria.mcp_auth as mcp_auth


# -- effective_act_as -------------------------------------------------------


def test_effective_act_as_no_identity_is_passthrough():
    # STDIO / local mode: no resolved identity, caller value preserved.
    assert mcp_auth.effective_act_as("alice") == "alice"
    assert mcp_auth.effective_act_as(None) is None


def test_effective_act_as_admin_keeps_act_as():
    token = mcp_auth.set_identity({"user_id": "admin", "role": "admin"})
    try:
        assert mcp_auth.effective_act_as("victim") == "victim"
        assert mcp_auth.effective_act_as("default") == "admin"
        assert mcp_auth.effective_act_as(None) == "admin"
    finally:
        mcp_auth.reset_identity(token)


def test_effective_act_as_non_admin_is_pinned_to_self():
    token = mcp_auth.set_identity({"user_id": "bob", "role": "user"})
    try:
        # A non-admin cannot Act-As anyone else, regardless of the argument.
        assert mcp_auth.effective_act_as("victim") == "bob"
        assert mcp_auth.effective_act_as("default") == "bob"
        assert mcp_auth.effective_act_as(None) == "bob"
    finally:
        mcp_auth.reset_identity(token)


# -- MCPAuthMiddleware ------------------------------------------------------


async def _inner(request):
    return JSONResponse({"identity": mcp_auth.current_identity()})


def _client() -> TestClient:
    inner = Starlette(routes=[Route("/x", _inner, methods=["GET", "POST"])])
    app = mcp_auth.MCPAuthMiddleware(inner, resolve_base_url="http://backend.test")
    return TestClient(app)


def test_middleware_rejects_missing_bearer(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    resp = _client().post("/x")
    assert resp.status_code == 401


def test_middleware_rejects_invalid_bearer(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)

    async def fake_resolve(base_url, token):
        return None

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_middleware_accepts_valid_bearer_and_sets_identity(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)

    async def fake_resolve(base_url, token):
        return {"user_id": "bob", "role": "user"}

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}


def test_middleware_allow_unauthenticated_escape_hatch(monkeypatch):
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "true")
    resp = _client().post("/x")
    assert resp.status_code == 200
    assert resp.json()["identity"] is None


def test_middleware_skips_auth_for_options(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    # OPTIONS must not be challenged with 401 (no auth header on preflight).
    resp = _client().options("/x")
    assert resp.status_code != 401
