"""Behavior of the admin-only /cliproxy router.

Mounted on a minimal FastAPI app with a stubbed management client so the
tests pin the router's own contracts: status degradation (absent config and
unreachable proxy both render, never raise), probe gating, the apply-route
shape math (the catalog is the single source of truth for root-vs-/v1 and
which key slot gets the cpx- gatekeeper), and the admin gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nymeria.api.routers import cliproxy as cliproxy_router_module
from nymeria.api.routers.cliproxy import create_cliproxy_router
from nymeria.cliproxy.management_client import (
    CLIProxyAuthError,
    CLIProxyUnreachable,
    CLIProxyUnsupported,
)
from nymeria.core.thread_config import (
    ActiveLLMFallback,
    ThreadConfig,
    ThreadLLMConfig,
)


class FakeManagementClient:
    """Scripted stand-in for CLIProxyManagementClient."""

    instances: list["FakeManagementClient"] = []
    probed: dict[str, bool] = {}
    auth_files: list[dict[str, Any]] = []
    knobs: dict[str, Any] = {}
    raise_on_probe: Optional[Exception] = None
    start_result: dict[str, str] = {"url": "https://auth.example/x", "state": "s1"}
    status_result: str = "wait"

    def __init__(self, base_url: str, secret: str, **kwargs: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self.calls: list[tuple[str, Any]] = []
        FakeManagementClient.instances.append(self)

    @property
    def management_html_url(self) -> str:
        return f"{self.base_url}/management.html"

    async def probe_providers(self, *, refresh: bool = False):
        if FakeManagementClient.raise_on_probe is not None:
            raise FakeManagementClient.raise_on_probe
        return dict(FakeManagementClient.probed)

    async def list_auth_files(self):
        return list(FakeManagementClient.auth_files)

    async def start_oauth(self, spec, *, project_id=None):
        self.calls.append(("start_oauth", (spec.id, project_id)))
        if not FakeManagementClient.probed.get(spec.id, True):
            raise CLIProxyUnsupported(f"{spec.id} unsupported")
        return dict(FakeManagementClient.start_result)

    async def oauth_callback(self, spec, *, redirect_url=None, code=None, state=None):
        self.calls.append(("oauth_callback", (spec.id, redirect_url, code, state)))

    async def auth_status(self, state):
        self.calls.append(("auth_status", state))
        return FakeManagementClient.status_result

    async def ensure_tool_prefix_disabled(self, name):
        self.calls.append(("ensure_tool_prefix_disabled", name))

    async def set_auth_file_disabled(self, name, *, disabled):
        self.calls.append(("set_auth_file_disabled", (name, disabled)))

    async def set_auth_file_fields(self, name, fields):
        self.calls.append(("set_auth_file_fields", (name, fields)))

    async def delete_auth_file(self, name):
        self.calls.append(("delete_auth_file", name))

    async def upload_auth_file(self, name, content):
        self.calls.append(("upload_auth_file", (name, content)))

    async def get_config_knobs(self, paths=None):
        return dict(FakeManagementClient.knobs)

    async def set_config_knob(self, path, value):
        self.calls.append(("set_config_knob", (path, value)))


class FakeThreadConfigManager:
    def __init__(self):
        self.saved: dict[str, ThreadConfig] = {}

    def get_config(self, thread_id: str):
        return self.saved.get(thread_id)

    def save_config(self, config: ThreadConfig) -> bool:
        self.saved[config.thread_id] = config
        return True


class FakeAgent:
    def __init__(self):
        self.thread_config_manager = FakeThreadConfigManager()
        self.credential_vault = SimpleNamespace(name="fake-vault")
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch):
    FakeManagementClient.instances = []
    FakeManagementClient.probed = {}
    FakeManagementClient.auth_files = []
    FakeManagementClient.knobs = {}
    FakeManagementClient.raise_on_probe = None
    FakeManagementClient.status_result = "wait"
    monkeypatch.setattr(
        cliproxy_router_module, "CLIProxyManagementClient", FakeManagementClient
    )
    yield


def make_app(*, settings=None, agent=None, admin: bool = True, thread_access=None):
    settings = settings or SimpleNamespace(
        cliproxy_management_url="http://proxy.test:8317",
        cliproxy_management_key="cpm-secret",
        anthropic_api_key=None,
        openai_api_key=None,
    )
    agent = agent or FakeAgent()

    def require_admin_user():
        if not admin:
            raise HTTPException(status_code=403, detail="Admin role required")
        # Mirrors the real ``AuthenticatedUser`` dataclass, whose identity field
        # is ``id``. A fake carrying ``user_id`` instead would let a route read
        # a non-existent attribute and silently pass None in production.
        return SimpleNamespace(id="admin", role="admin")

    app = FastAPI()
    app.include_router(
        create_cliproxy_router(
            require_admin_user,
            lambda: agent,
            lambda: settings,
            require_thread_access_fn=thread_access,
        )
    )
    return TestClient(app), settings, agent


def test_status_unconfigured_renders_instead_of_erroring():
    client, _, _ = make_app(
        settings=SimpleNamespace(
            cliproxy_management_url=None, cliproxy_management_key=None
        )
    )
    payload = client.get("/cliproxy/status").json()
    assert payload["configured"] is False
    assert payload["reachable"] is False
    assert len(payload["providers"]) >= 6
    assert all(p["supported"] is None for p in payload["providers"])


def test_status_degrades_when_proxy_unreachable():
    FakeManagementClient.raise_on_probe = CLIProxyUnreachable("connection refused")
    client, _, _ = make_app()
    payload = client.get("/cliproxy/status").json()
    assert payload["configured"] is True
    assert payload["reachable"] is False
    assert "refused" in payload["detail"]


def test_status_merges_probe_and_login_state():
    FakeManagementClient.probed = {
        "claude": True,
        "codex": True,
        "gemini-cli": True,
        "antigravity": True,
        "kimi": True,
        "grok": False,
    }
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "disabled": False},
        {"name": "codex-b.json", "provider": "codex", "disabled": True},
    ]
    client, _, _ = make_app()
    payload = client.get("/cliproxy/status").json()
    by_id = {p["id"]: p for p in payload["providers"]}
    assert payload["reachable"] is True
    assert by_id["claude"]["logged_in"] is True
    # Disabled auth files do not count as logged in.
    assert by_id["codex"]["logged_in"] is False
    assert by_id["grok"]["supported"] is False
    assert payload["management_html_url"] == "http://proxy.test:8317/management.html"


def test_oauth_start_returns_flow_and_state():
    FakeManagementClient.probed = {"claude": True}
    client, _, _ = make_app()
    payload = client.post(
        "/cliproxy/oauth/start", json={"provider": "claude"}
    ).json()
    assert payload == {
        "provider": "claude",
        "flow": "browser",
        "url": "https://auth.example/x",
        "state": "s1",
    }


def test_oauth_start_gates_unsupported_provider():
    FakeManagementClient.probed = {"grok": False}
    client, _, _ = make_app()
    response = client.post("/cliproxy/oauth/start", json={"provider": "grok"})
    assert response.status_code == 422


def test_oauth_start_unknown_provider_404s():
    client, _, _ = make_app()
    assert (
        client.post("/cliproxy/oauth/start", json={"provider": "qwen"}).status_code
        == 404
    )


def test_oauth_start_400_when_unconfigured():
    client, _, _ = make_app(
        settings=SimpleNamespace(
            cliproxy_management_url=None, cliproxy_management_key=None
        )
    )
    response = client.post("/cliproxy/oauth/start", json={"provider": "claude"})
    assert response.status_code == 400


def test_oauth_callback_requires_redirect_or_code_state():
    client, _, _ = make_app()
    response = client.post("/cliproxy/oauth/callback", json={"provider": "claude"})
    assert response.status_code == 400


def test_oauth_status_ok_runs_claude_fixup():
    FakeManagementClient.status_result = "ok"
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude"},
        {"name": "codex-b.json", "provider": "codex"},
    ]
    client, _, _ = make_app()
    payload = client.get(
        "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
    ).json()
    assert payload == {"status": "ok", "detail": ""}
    calls = [
        call
        for instance in FakeManagementClient.instances
        for call in instance.calls
        if call[0] == "ensure_tool_prefix_disabled"
    ]
    assert calls == [("ensure_tool_prefix_disabled", "claude-a.json")]


def test_oauth_status_confirms_ok_against_auth_files():
    """A bare proxy ok with no active auth file is reported as an error
    (the proxy's status endpoint answers ok for unknown/expired sessions),
    and a confirmed ok carries the account label as detail."""
    FakeManagementClient.status_result = "ok"
    FakeManagementClient.auth_files = []  # nothing landed
    client, _, _ = make_app()
    payload = client.get(
        "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
    ).json()
    assert payload["status"] == "error"
    assert "no active" in payload["detail"]

    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
    ]
    payload = client.get(
        "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
    ).json()
    assert payload == {"status": "ok", "detail": "max@x.io"}

    # A disabled entry does not count as a login.
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "disabled": True},
    ]
    payload = client.get(
        "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
    ).json()
    assert payload["status"] == "error"


def test_oauth_status_refuses_stale_session_ok_via_the_ledger():
    """The relogin trap through the REST route: an old, paste-less session
    (per the management client's in-process ledger) answering ok with a
    PRE-EXISTING auth file is refused; the fake client never stamps the
    ledger, so this stamps it directly (real stamps happen inside
    start_oauth/oauth_callback). Unknown states stay fail-open, which the
    sibling confirm test above exercises implicitly."""
    import time as time_module

    from nymeria.cliproxy import management_client

    FakeManagementClient.status_result = "ok"
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
    ]
    client, _, _ = make_app()
    management_client._oauth_session_ledger["s1"] = (
        time_module.monotonic() - management_client.SESSION_OK_GUARD_SECONDS - 60,
        False,
        True,
    )
    try:
        payload = client.get(
            "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
        ).json()
        assert payload["status"] == "error"
        assert "stale-session" in payload["detail"]

        # The same old session WITH a delivered callback stays trusted.
        management_client._oauth_session_ledger["s1"] = (
            management_client._oauth_session_ledger["s1"][0],
            True,
            True,
        )
        payload = client.get(
            "/cliproxy/oauth/status", params={"state": "s1", "provider": "claude"}
        ).json()
        assert payload == {"status": "ok", "detail": "max@x.io"}
    finally:
        management_client._oauth_session_ledger.clear()


def test_oauth_status_ok_for_codex_skips_fixup():
    FakeManagementClient.status_result = "ok"
    FakeManagementClient.auth_files = [
        {"name": "codex-b.json", "provider": "codex"},
    ]
    client, _, _ = make_app()
    client.get(
        "/cliproxy/oauth/status", params={"state": "s1", "provider": "codex"}
    )
    calls = [
        call
        for instance in FakeManagementClient.instances
        for call in instance.calls
        if call[0] == "ensure_tool_prefix_disabled"
    ]
    assert calls == []


class _FakeDataPlaneClient:
    """Stands in for httpx.AsyncClient on the proxy's /v1/models call."""

    response_body: dict | None = None
    calls: list[dict] = []

    def __init__(self, *, timeout: float):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, *, headers: dict):
        _FakeDataPlaneClient.calls.append({"url": url, "headers": headers})
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json=_FakeDataPlaneClient.response_body or {"data": []},
            request=request,
        )


def test_models_lists_via_server_resolved_gatekeeper(monkeypatch):
    """The data plane is hit with a gatekeeper the backend resolved through
    the management API; the key never rides the REST contract."""
    FakeManagementClient.knobs = {"api-keys": ["cpx-first"]}
    _FakeDataPlaneClient.response_body = {
        "data": [
            {"id": "gpt-5.5"},
            {"id": "claude-opus-4-7", "owned_by": "anthropic"},
        ]
    }
    _FakeDataPlaneClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDataPlaneClient)
    client, _, _ = make_app()
    response = client.get("/cliproxy/models")
    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {"id": "claude-opus-4-7", "owned_by": "anthropic"},
            {"id": "gpt-5.5", "owned_by": ""},
        ]
    }
    call = _FakeDataPlaneClient.calls[0]
    assert call["url"] == "http://proxy.test:8317/v1/models"
    assert call["headers"]["Authorization"] == "Bearer cpx-first"


def test_models_400_when_unconfigured():
    settings = SimpleNamespace(
        cliproxy_management_url="",
        cliproxy_management_key="",
        anthropic_api_key=None,
        openai_api_key=None,
    )
    client, _, _ = make_app(settings=settings)
    assert client.get("/cliproxy/models").status_code == 400


def test_auth_files_filter_by_provider():
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude"},
        {"name": "xai-c.json", "provider": "xai"},
    ]
    client, _, _ = make_app()
    payload = client.get(
        "/cliproxy/auth-files", params={"provider": "grok"}
    ).json()
    assert [entry["name"] for entry in payload] == ["xai-c.json"]


def test_auth_files_filter_accepts_both_gemini_spellings():
    # v7 binaries list the entry as "gemini-cli", older ones as "gemini";
    # the filter must keep both and still exclude other providers.
    FakeManagementClient.auth_files = [
        {"name": "gemini-a.json", "provider": "gemini-cli"},
        {"name": "gemini-b.json", "provider": "gemini"},
        {"name": "claude-c.json", "provider": "claude"},
    ]
    client, _, _ = make_app()
    payload = client.get(
        "/cliproxy/auth-files", params={"provider": "gemini-cli"}
    ).json()
    assert [entry["name"] for entry in payload] == [
        "gemini-a.json",
        "gemini-b.json",
    ]


def test_status_counts_a_v7_spelled_gemini_login():
    # The live v7.1.61 listing spelling; exact-matching it against the
    # catalog's "gemini" read a completed login as logged out.
    FakeManagementClient.probed = {"gemini-cli": True}
    FakeManagementClient.auth_files = [
        {"name": "gemini-a.json", "provider": "gemini-cli", "disabled": False},
    ]
    client, _, _ = make_app()
    by_id = {
        p["id"]: p for p in client.get("/cliproxy/status").json()["providers"]
    }
    assert by_id["gemini-cli"]["logged_in"] is True
    assert by_id["antigravity"]["logged_in"] is False


def test_import_auth_file_confirms_active_login_and_runs_claude_fixup():
    """POST /cliproxy/auth-files ports the headless --cliproxy-auth-file
    ladder: upload, then CONFIRM an active login is listed (the confirm-
    on-ok trust rule); a confirmed Claude import also gets the
    tool_prefix_disabled fixup."""
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
    ]
    client, _, _ = make_app()
    payload = client.post(
        "/cliproxy/auth-files",
        json={
            "provider": "claude",
            "name": "claude-a.json",
            "content": '{"access_token": "tok"}',
        },
    ).json()
    assert payload == {"status": "ok", "account": "max@x.io", "detail": ""}
    calls = [c for i in FakeManagementClient.instances for c in i.calls]
    assert ("upload_auth_file", ("claude-a.json", b'{"access_token": "tok"}')) in calls
    assert ("ensure_tool_prefix_disabled", "claude-a.json") in calls


def test_import_auth_file_reports_inactive_honestly():
    """An upload the proxy ACCEPTED but does not list as an active login
    (disabled/expired/wrong provider) must not read as success."""
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "disabled": True},
    ]
    client, _, _ = make_app()
    payload = client.post(
        "/cliproxy/auth-files",
        json={"provider": "claude", "name": "claude-a.json", "content": "{}"},
    ).json()
    assert payload["status"] == "inactive"
    assert "not an active login" in payload["detail"]


def test_import_confirm_cannot_borrow_a_preexisting_login():
    """The confirm matches the entry listed under the UPLOADED name: a
    pre-existing active login for the same provider must never bless a
    dead/mismatched import (the auth-file twin of the relogin trap)."""
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
        {"name": "claude-b.json", "provider": "claude", "disabled": True},
    ]
    client, _, _ = make_app()
    payload = client.post(
        "/cliproxy/auth-files",
        json={"provider": "claude", "name": "claude-b.json", "content": "{}"},
    ).json()
    assert payload["status"] == "inactive"
    assert "disabled" in payload["detail"]

    # A wrong-provider pick reports what the proxy parsed, not success.
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
        {"name": "codex-b.json", "provider": "codex"},
    ]
    payload = client.post(
        "/cliproxy/auth-files",
        json={"provider": "claude", "name": "codex-b.json", "content": "{}"},
    ).json()
    assert payload["status"] == "inactive"
    assert "codex" in payload["detail"]

    # An accepted upload the proxy does not list at all is inactive too.
    FakeManagementClient.auth_files = [
        {"name": "claude-a.json", "provider": "claude", "email": "max@x.io"},
    ]
    payload = client.post(
        "/cliproxy/auth-files",
        json={"provider": "claude", "name": "claude-c.json", "content": "{}"},
    ).json()
    assert payload["status"] == "inactive"
    assert "does not list" in payload["detail"]


def test_import_auth_file_rejects_bad_names_and_bad_json():
    client, _, _ = make_app()
    for bad_name in ("../evil.json", "auths/x.json", ".hidden.json", "x.txt", ""):
        response = client.post(
            "/cliproxy/auth-files",
            json={"provider": "claude", "name": bad_name, "content": "{}"},
        )
        assert response.status_code == 400, bad_name
    response = client.post(
        "/cliproxy/auth-files",
        json={"provider": "claude", "name": "a.json", "content": "not json"},
    )
    assert response.status_code == 400
    # JSON-parseable but not UTF-8-encodable content (a lone surrogate)
    # must be a 400, not an unhandled 500. Sent as a raw ASCII-escaped
    # body because httpx's own json= encoder refuses surrogates.
    import json as json_module

    body = json_module.dumps(
        {"provider": "claude", "name": "a.json", "content": '{"k": "\ud800"}'}
    )
    response = client.post(
        "/cliproxy/auth-files",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    # Nothing reached the proxy on any refusal.
    calls = [c for i in FakeManagementClient.instances for c in i.calls]
    assert all(call[0] != "upload_auth_file" for call in calls)


def test_patch_auth_file_dispatches_status_and_priority():
    client, _, _ = make_app()
    response = client.patch(
        "/cliproxy/auth-files/claude-a.json",
        json={"disabled": True, "priority": 7},
    )
    assert response.status_code == 200
    calls = FakeManagementClient.instances[-1].calls
    assert ("set_auth_file_disabled", ("claude-a.json", True)) in calls
    assert ("set_auth_file_fields", ("claude-a.json", {"priority": 7})) in calls


def test_patch_config_rejects_unknown_knobs():
    client, _, _ = make_app()
    response = client.patch(
        "/cliproxy/config", json={"knobs": {"debug": True}}
    )
    assert response.status_code == 400


def test_apply_route_global_claude_writes_anthropic_root_key():
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    client, _, _ = make_app()
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        payload = client.post(
            "/cliproxy/apply-route",
            json={"provider": "claude", "gatekeeper_key": "cpx-gate"},
        ).json()

    updates = captured["updates"]
    assert updates.llm_provider == "anthropic"
    # Claude rides the proxy ROOT (no /v1); the gatekeeper goes to
    # ANTHROPIC_API_KEY, never the DIRECT slot.
    assert updates.llm_base_url == "http://proxy.test:8317"
    assert updates.anthropic_api_key == "cpx-gate"
    assert updates.anthropic_direct_api_key is None
    assert payload["base_url"] == "http://proxy.test:8317"
    assert payload["model"] == "claude-opus-4-7"


def test_apply_route_global_codex_writes_openai_v1_responses():
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    client, _, _ = make_app()
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        payload = client.post(
            "/cliproxy/apply-route",
            json={
                "provider": "codex",
                "model": "gpt-5.5",
                "gatekeeper_key": "cpx-gate",
            },
        ).json()

    updates = captured["updates"]
    assert updates.llm_provider == "openai"
    assert updates.llm_base_url == "http://proxy.test:8317/v1"
    assert updates.openai_api_mode == "responses"
    assert updates.openai_api_key == "cpx-gate"
    assert payload["api_mode"] == "responses"


def test_apply_route_reuses_configured_gatekeeper_key():
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    settings = SimpleNamespace(
        cliproxy_management_url="http://proxy.test:8317",
        cliproxy_management_key="cpm-secret",
        anthropic_api_key="cpx-existing",
        openai_api_key=None,
    )
    client, _, _ = make_app(settings=settings)
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        response = client.post(
            "/cliproxy/apply-route", json={"provider": "claude"}
        )
    assert response.status_code == 200
    assert captured["updates"].anthropic_api_key == "cpx-existing"


def test_apply_route_auto_resolves_gatekeeper_via_management_api():
    """Fresh install: no key in the request or settings -> the route reads
    the proxy's first configured api-key (or mints one) instead of 422ing,
    because no frontend can supply the masked knob itself."""
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = {"api-keys": ["cpx-first", "cpx-second"]}
    client, _, _ = make_app()
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        response = client.post(
            "/cliproxy/apply-route", json={"provider": "claude"}
        )
    assert response.status_code == 200
    assert captured["updates"].anthropic_api_key == "cpx-first"


def test_apply_route_mints_gatekeeper_when_proxy_has_none():
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = {"api-keys": []}
    client, _, _ = make_app()
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        response = client.post(
            "/cliproxy/apply-route", json={"provider": "codex"}
        )
    assert response.status_code == 200
    minted = captured["updates"].openai_api_key
    assert minted.startswith("cpx-nymeria-")
    writes = [
        call
        for instance in FakeManagementClient.instances
        for call in instance.calls
        if call[0] == "set_config_knob"
    ]
    assert writes == [("set_config_knob", ("api-keys", [minted]))]


def test_apply_route_rejects_non_gatekeeper_settings_key():
    """A REAL provider key in the settings slot (an sk- OPENAI_API_KEY on a
    codex route) must not be adopted as the proxy gatekeeper: it is
    validated against the proxy's api-keys list and loses to a configured
    cpx- key."""
    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": False}

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = {"api-keys": ["cpx-first"]}
    settings = SimpleNamespace(
        cliproxy_management_url="http://proxy.test:8317",
        cliproxy_management_key="secret",
        anthropic_api_key=None,
        openai_api_key="sk-real-openai-key",
    )
    client, _, _ = make_app(settings=settings)
    import unittest.mock as mock

    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        response = client.post(
            "/cliproxy/apply-route", json={"provider": "codex"}
        )
    assert response.status_code == 200
    assert captured["updates"].openai_api_key == "cpx-first"


def test_models_route_reads_but_never_mints(monkeypatch):
    """A key-less proxy has an OPEN data plane: the models route must go
    out unauthenticated rather than mint (a mint would flip the proxy to
    key-required as a side effect of a READ)."""
    FakeManagementClient.knobs = {"api-keys": []}
    _FakeDataPlaneClient.response_body = {"data": [{"id": "gpt-5.5"}]}
    _FakeDataPlaneClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDataPlaneClient)
    client, _, _ = make_app()
    response = client.get("/cliproxy/models")
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["models"]] == ["gpt-5.5"]
    assert "Authorization" not in _FakeDataPlaneClient.calls[0]["headers"]
    writes = [
        call
        for instance in FakeManagementClient.instances
        for call in instance.calls
        if call[0] == "set_config_knob"
    ]
    assert writes == []


def test_apply_route_422_when_gatekeeper_resolution_fails():
    class _BrokenKnobsClient(FakeManagementClient):
        async def get_config_knobs(self, paths=None):
            raise CLIProxyUnreachable("proxy down")

    import unittest.mock as mock

    from nymeria.api.routers import cliproxy as router_module

    client, _, _ = make_app()
    with mock.patch.object(
        router_module, "CLIProxyManagementClient", _BrokenKnobsClient
    ):
        response = client.post(
            "/cliproxy/apply-route", json={"provider": "claude"}
        )
    assert response.status_code == 422


def test_apply_route_thread_writes_thread_llm_config():
    accessed: list[str] = []

    def thread_access(user, thread_id):
        accessed.append(thread_id)

    client, _, agent = make_app(thread_access=thread_access)
    response = client.post(
        "/cliproxy/apply-route",
        json={
            "provider": "grok",
            "scope": "thread",
            "thread_id": "t-1",
            "gatekeeper_key": "cpx-gate",
        },
    )
    assert response.status_code == 200
    saved = agent.thread_config_manager.saved["t-1"]
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.base_url == "http://proxy.test:8317/v1"
    assert saved.llm_config.api_key == "cpx-gate"
    assert saved.llm_config.openai_api_mode == "chat_completions"
    assert saved.llm_config.model == "grok-4.3"
    assert saved.active_llm_fallback is None
    # Same guarantees as PATCH /threads/{id}/config: access checked/claimed,
    # cached per-thread graph evicted so the route applies to the next turn.
    assert accessed == ["t-1"]
    assert agent.invalidated == ["t-1"]


def test_apply_route_thread_requires_thread_id():
    client, _, _ = make_app()
    response = client.post(
        "/cliproxy/apply-route",
        json={"provider": "claude", "scope": "thread", "gatekeeper_key": "k"},
    )
    assert response.status_code == 400


def test_all_routes_require_admin():
    client, _, _ = make_app(admin=False)
    assert client.get("/cliproxy/catalog").status_code == 403
    assert client.get("/cliproxy/status").status_code == 403
    assert (
        client.post("/cliproxy/oauth/start", json={"provider": "claude"}).status_code
        == 403
    )
    assert client.get("/cliproxy/auth-files").status_code == 403
    assert (
        client.post("/cliproxy/apply-route", json={"provider": "claude"}).status_code
        == 403
    )


def test_auth_error_maps_to_502():
    FakeManagementClient.raise_on_probe = CLIProxyAuthError("key rejected")
    client, _, _ = make_app()
    # status degrades rather than erroring even on auth failure
    payload = client.get("/cliproxy/status").json()
    assert payload["reachable"] is False
    assert "rejected" in payload["detail"]


def test_router_is_registered_in_app_factory():
    """The app factory must mount the router (registration drift guard)."""
    import inspect

    from nymeria.triggers import api as api_module

    source = inspect.getsource(api_module.create_api_app)
    assert "create_cliproxy_router" in source


def test_config_masks_gatekeeper_keys():
    FakeManagementClient.knobs = {
        "api-keys": ["cpx-aaaaaaaabbbbbbbbcccc", "short"],
        "request-retry": 1,
    }
    client, _, _ = make_app()
    payload = client.get("/cliproxy/config").json()
    keys = payload["knobs"]["api-keys"]
    assert keys[0].startswith("cpx-aaaa") and keys[0].endswith("cc")
    assert "cpx-aaaaaaaabbbbbbbbcccc" not in str(payload)
    assert keys[1] == "***"
    # Non-key knobs pass through untouched.
    assert payload["knobs"]["request-retry"] == 1


def test_management_key_is_masked_in_settings_env():
    """The CLIProxy management secret must be treated as a secret by
    GET /settings/env (no bare *_key suffix rule covers it)."""
    from nymeria.api.routers.settings import _is_secret_setting_key

    assert _is_secret_setting_key("cliproxy_management_key") is True
    assert _is_secret_setting_key("cliproxy_management_url") is False


def test_auth_error_maps_to_502_on_action_routes():
    FakeManagementClient.raise_on_probe = CLIProxyAuthError("key rejected")

    async def raising_start(self, spec, *, project_id=None):
        raise CLIProxyAuthError("key rejected")

    original = FakeManagementClient.start_oauth
    FakeManagementClient.start_oauth = raising_start
    try:
        client, _, _ = make_app()
        response = client.post(
            "/cliproxy/oauth/start", json={"provider": "claude"}
        )
        assert response.status_code == 502
        assert "rejected" in response.json()["detail"]
    finally:
        FakeManagementClient.start_oauth = original


def test_patch_config_rejects_masked_gatekeeper_round_trip():
    """GET masks api-keys, so PATCHing a masked value back must 400 instead of
    clobbering the proxy's real key list."""
    client, _, _ = make_app()
    response = client.patch(
        "/cliproxy/config",
        json={"knobs": {"api-keys": ["cpx-aaaa…cc", "cpx-real-key"]}},
    )
    assert response.status_code == 400
    assert "masked" in response.json()["detail"]
    response = client.patch(
        "/cliproxy/config", json={"knobs": {"api-keys": ["***"]}}
    )
    assert response.status_code == 400


# --- Direct unit tests for the extracted apply-route branch helpers (F11) ---
# The end-to-end `/cliproxy/apply-route` tests above are the characterization
# net; these pin branch-internal paths the route tests do not fully exercise.


def _spec(provider: str):
    return cliproxy_router_module._require_spec(provider)


def test_apply_route_thread_helper_skips_gate_when_no_access_fn():
    agent = FakeAgent()
    resp = cliproxy_router_module._apply_route_thread(
        agent=agent,
        admin=SimpleNamespace(user_id="admin"),
        spec=_spec("grok"),
        thread_id="t-9",
        model="grok-4.3",
        base_url="http://proxy.test:8317/v1",
        gatekeeper="cpx-gate",
        require_thread_access_fn=None,
    )
    saved = agent.thread_config_manager.saved["t-9"]
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.api_key == "cpx-gate"
    assert saved.llm_config.openai_api_mode == "chat_completions"
    assert resp.scope == "thread"
    assert resp.thread_id == "t-9"
    # No access fn -> no gate raised; the cache eviction still fires.
    assert agent.invalidated == ["t-9"]


def test_apply_route_thread_helper_merges_existing_config_and_clears_fallback():
    agent = FakeAgent()
    existing = ThreadConfig(thread_id="t-keep")
    existing.active_llm_fallback = ActiveLLMFallback(
        provider="anthropic",
        model="claude-opus-4-7",
        source_provider="openai",
        source_model="gpt-5.5",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    existing.llm_config = ThreadLLMConfig(temperature=0.4)
    agent.thread_config_manager.saved["t-keep"] = existing

    cliproxy_router_module._apply_route_thread(
        agent=agent,
        admin=None,
        spec=_spec("grok"),
        thread_id="t-keep",
        model="grok-4.3",
        base_url="http://proxy.test:8317/v1",
        gatekeeper="cpx-new",
        require_thread_access_fn=None,
    )
    saved = agent.thread_config_manager.saved["t-keep"]
    # Merged onto the SAME llm_config object (unrelated fields preserved), the
    # route fields overwritten, and the stale fallback cleared.
    assert saved.llm_config is not None
    assert saved.llm_config is existing.llm_config
    assert saved.llm_config.temperature == 0.4
    assert saved.llm_config.api_key == "cpx-new"
    assert saved.active_llm_fallback is None


def test_apply_route_thread_helper_claude_writes_none_api_mode():
    agent = FakeAgent()
    claude = _spec("claude")
    # Catalog invariant: the anthropic route carries no openai api mode, so the
    # `spec.api_mode or None` branch must write None (not "").
    assert not claude.api_mode
    cliproxy_router_module._apply_route_thread(
        agent=agent,
        admin=None,
        spec=claude,
        thread_id="t-c",
        model="claude-opus-4-7",
        base_url="http://proxy.test:8317",
        gatekeeper="cpx-gate",
        require_thread_access_fn=None,
    )
    saved = agent.thread_config_manager.saved["t-c"]
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "anthropic"
    assert saved.llm_config.openai_api_mode is None


def test_apply_route_thread_helper_missing_thread_id_raises_400():
    with pytest.raises(HTTPException) as exc:
        cliproxy_router_module._apply_route_thread(
            agent=FakeAgent(),
            admin=None,
            spec=_spec("claude"),
            thread_id=None,
            model="claude-opus-4-7",
            base_url="http://proxy.test:8317",
            gatekeeper="cpx-gate",
            require_thread_access_fn=None,
        )
    assert exc.value.status_code == 400


def test_apply_route_thread_helper_tolerates_agent_without_evictor():
    # An agent lacking `invalidate_thread_config_cache` must not crash (the
    # callable() guard skips eviction).
    agent = SimpleNamespace(thread_config_manager=FakeThreadConfigManager())
    resp = cliproxy_router_module._apply_route_thread(
        agent=agent,
        admin=None,
        spec=_spec("grok"),
        thread_id="t-x",
        model="grok-4.3",
        base_url="http://proxy.test:8317/v1",
        gatekeeper="cpx-gate",
        require_thread_access_fn=None,
    )
    assert resp.thread_id == "t-x"
    assert agent.thread_config_manager.saved["t-x"].llm_config.api_key == "cpx-gate"


def test_apply_route_global_helper_propagates_restart_required():
    import unittest.mock as mock

    from nymeria.api.routers import settings as settings_router_module

    captured: dict[str, Any] = {}

    def fake_apply(updates, *, settings, agent, get_settings_fn):
        captured["updates"] = updates
        return {"restart_required": True}

    settings = SimpleNamespace()
    with mock.patch.object(
        settings_router_module, "apply_server_settings_update", fake_apply
    ):
        resp = cliproxy_router_module._apply_route_global(
            agent=FakeAgent(),
            settings=settings,
            spec=_spec("codex"),
            model="gpt-5.5",
            base_url="http://proxy.test:8317/v1",
            gatekeeper="cpx-gate",
            get_settings_fn=lambda: settings,
        )
    assert resp.scope == "global"
    assert resp.restart_required is True
    assert captured["updates"].openai_api_key == "cpx-gate"
    assert captured["updates"].openai_api_mode == "responses"


# --- credential verification (verify_cliproxy_credential) ------------------
# A login confirm only proves the proxy LISTS an enabled auth file, so the
# chain follows it with one real data-plane completion. These pin the verdict
# classification, which is the part carrying judgement calls.


def _verify(spec_id: str, probe_response: Any, *, knobs: dict | None = None):
    import asyncio
    import unittest.mock as mock

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = knobs if knobs is not None else {"api-keys": ["cpx-key"]}
    client = FakeManagementClient("http://proxy.test:8317", "secret")
    captured: dict[str, Any] = {}

    async def fake_probe(request, *, settings=None, vault=None, owner_user_id=None):
        captured["request"] = request
        if isinstance(probe_response, Exception):
            raise probe_response
        return probe_response

    with mock.patch.object(
        settings_router_module, "_test_llm_provider_config", fake_probe
    ):
        verdict, detail = asyncio.run(
            cliproxy_router_module.verify_cliproxy_credential(
                client,
                "http://proxy.test:8317",
                _spec(spec_id),
            )
        )
    return verdict, detail, captured


def test_verify_credential_ok_reports_the_probed_model():
    verdict, detail, captured = _verify(
        "claude", SimpleNamespace(ok=True, message="")
    )

    assert verdict == "ok"
    assert detail == "claude-opus-4-7"
    request = captured["request"]
    # Probes the data plane, as the native provider, with the server-resolved
    # gatekeeper. Claude uses the proxy ROOT (the SDK appends /v1/messages).
    assert request.llm_provider == "anthropic"
    assert request.llm_base_url == "http://proxy.test:8317"
    # SecretStr: the schema keeps the gatekeeper out of reprs and logs.
    assert request.api_key.get_secret_value() == "cpx-key"


def test_verify_credential_uses_v1_and_api_mode_for_openai_shaped_specs():
    _verdict, _detail, captured = _verify(
        "codex", SimpleNamespace(ok=True, message="")
    )

    request = captured["request"]
    assert request.llm_provider == "openai"
    assert request.llm_base_url == "http://proxy.test:8317/v1"
    assert request.openai_api_mode == "responses"


# The next three pin the MESSAGE fallback, which is what runs when the probe
# reports no status at all (transport error, timeout, missing key). The
# structured-status path is pinned separately below; a real HTTP failure always
# carries a status and takes that path instead.


def test_verify_credential_message_fallback_treats_rate_limit_as_proof_of_reach():
    """429 means the request REACHED the upstream, so the credential is good.

    This probe carries no OAuth billing fingerprint (that lives in
    vendor/react_agent/nodes.py), so a subscription path can answer 429 on a
    premium model with a perfectly valid token. Calling that a failure would
    tell users a working login is broken.
    """

    verdict, _detail, _captured = _verify(
        "claude",
        SimpleNamespace(ok=False, message="429 rate_limit_error: quota"),
    )

    assert verdict == "ok"


def test_verify_credential_message_fallback_flags_a_rejected_credential():
    verdict, detail, _captured = _verify(
        "claude",
        SimpleNamespace(ok=False, message="401 invalid_grant"),
    )

    assert verdict == "auth_failed"
    assert "401" in detail


def test_verify_credential_message_fallback_is_inconclusive_for_anything_else():
    verdict, detail, _captured = _verify(
        "claude",
        SimpleNamespace(ok=False, message="503 upstream unavailable"),
    )

    assert verdict == "inconclusive"
    assert "503" in detail


def test_verify_credential_reads_the_status_field_not_the_response_body():
    """A body carrying "401" must not be read as a rejected credential.

    ``_test_llm_provider_config`` reports the upstream status as a FIELD and
    embeds the response BODY in the message, so classifying on the message
    would let a request id, quota name or port turn a transient 500 into
    "your login failed" and send the user back through a browser OAuth flow
    they do not need. The structured status wins whenever it is present.
    """

    verdict, _detail, _captured = _verify(
        "claude",
        SimpleNamespace(
            ok=False,
            status_code=500,
            message="Provider returned HTTP 500: {'request_id': 'req_401_x'}",
        ),
    )

    assert verdict == "inconclusive"


def test_verify_credential_classifies_a_structured_401_as_auth_failed():
    verdict, _detail, _captured = _verify(
        "claude",
        SimpleNamespace(
            ok=False, status_code=401, message="Provider returned HTTP 401: nope"
        ),
    )

    assert verdict == "auth_failed"


def test_verify_credential_classifies_a_structured_429_as_reach():
    verdict, detail, _captured = _verify(
        "claude",
        SimpleNamespace(
            ok=False, status_code=429, message="Provider returned HTTP 429: slow down"
        ),
    )

    assert verdict == "ok"
    assert detail == "claude-opus-4-7"


def test_verify_credential_probes_unauthenticated_when_no_gatekeeper():
    """A key-less proxy has an OPEN data plane, and a read must never mint.

    The sentinel matters: passing None instead would hand the choice to the
    probe's credential chain, which resolves the operator's REAL vendor key
    from the vault or settings and sends it to the proxy, and which hard-fails
    with "missing_api_key" whenever the proxy URL is not loopback-shaped (a
    Docker service name is not), making every verification inconclusive on the
    reference deployment.
    """

    _verdict, _detail, captured = _verify(
        "claude", SimpleNamespace(ok=True, message=""), knobs={"api-keys": []}
    )

    assert captured["request"].api_key.get_secret_value() == "not-needed"
    client = FakeManagementClient.instances[-1]
    assert [name for name, _ in client.calls if name == "set_config_knob"] == []


def test_verify_credential_never_reaches_the_probes_credential_fallback():
    """Pin the consequence, not just the value: the vault is never consulted.

    ``_test_llm_provider_config`` only walks vault -> settings -> env when it
    receives no key at all, so a truthy api_key is what keeps the operator's
    own Anthropic/OpenAI credential out of a proxy-verification request.
    """

    import asyncio
    import unittest.mock as mock

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = {"api-keys": []}
    client = FakeManagementClient("http://proxy.test:8317", "secret")
    seen: dict[str, Any] = {}

    async def fake_probe(request, *, settings=None, vault=None, owner_user_id=None):
        seen["api_key"] = request.api_key
        return SimpleNamespace(ok=True, message="")

    with mock.patch.object(
        settings_router_module, "_test_llm_provider_config", fake_probe
    ):
        asyncio.run(
            cliproxy_router_module.verify_cliproxy_credential(
                client,
                # A non-loopback base URL: the shape that used to fail closed.
                "http://cli-proxy-api:8317",
                _spec("claude"),
            )
        )

    assert seen["api_key"] is not None
    assert seen["api_key"].get_secret_value() == "not-needed"


def test_verify_credential_without_a_known_model_is_inconclusive():
    import asyncio
    import unittest.mock as mock

    from nymeria.api.routers import settings as settings_router_module

    FakeManagementClient.knobs = {"api-keys": ["cpx-key"]}
    client = FakeManagementClient("http://proxy.test:8317", "secret")
    spec = _spec("claude")
    stripped = type(spec)(**{**spec.__dict__, "default_model": ""})
    called = False

    async def fake_probe(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return SimpleNamespace(ok=True, message="")

    with mock.patch.object(
        settings_router_module, "_test_llm_provider_config", fake_probe
    ):
        verdict, detail = asyncio.run(
            cliproxy_router_module.verify_cliproxy_credential(
                client, "http://proxy.test:8317", stripped
            )
        )

    assert verdict == "inconclusive"
    assert "no model is known" in detail
    assert called is False


def test_verify_route_passes_the_callers_identity_and_the_agent_vault():
    """The probe resolves stored credentials, so it needs the real caller.

    ``AuthenticatedUser`` carries ``id``; reading a ``user_id`` that does not
    exist would silently pass None here and quietly change which credentials
    the probe can resolve. Pin both halves of the plumbing.
    """
    import unittest.mock as mock

    client, _settings, agent = make_app()
    captured: dict[str, Any] = {}

    async def fake_verify(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return "ok", "claude-opus-4-7"

    with mock.patch.object(
        cliproxy_router_module, "verify_cliproxy_credential", fake_verify
    ):
        resp = client.post("/cliproxy/verify", json={"provider": "claude"})

    assert resp.status_code == 200
    assert resp.json() == {"verdict": "ok", "detail": "claude-opus-4-7"}
    assert captured["owner_user_id"] == "admin"
    assert captured["vault"] is agent.credential_vault
