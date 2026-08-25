"""Regression tests for the FastAPI route surface and auth contracts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from nymeria import __version__
from nymeria.core.accounts import AccountsRepo
from nymeria.triggers import api as api_module


class FakeAgent:
    def __init__(self, data_dir: Path):
        from nymeria.core.chat_bindings import ChatBindingsRepo
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


# Intentional route changes must update this snapshot. Regenerate it from Nymeria/
# with:
#   PYTHONPATH=. python3 tests/test_api_route_inventory.py
# and paste the printed body between the brackets below.
EXPECTED_ROUTES = [
    ('/activity', ('GET',)),
    ('/admin/chatapp/bindings', ('GET',)),
    ('/admin/chatapp/bindings/by-chat', ('DELETE',)),
    ('/admin/chatapp/bindings/claim', ('POST',)),
    ('/admin/chatapp/bindings/claim-via-bot', ('POST',)),
    ('/admin/chatapp/bindings/lookup', ('GET',)),
    ('/admin/chatapp/bindings/switch', ('POST',)),
    ('/admin/platform/link-codes/claim', ('POST',)),
    ('/admin/telegram-bots', ('GET',)),
    ('/admin/telegram-bots/{bot_id}/seen', ('POST',)),
    ('/admin/users', ('GET',)),
    ('/admin/users', ('POST',)),
    ('/admin/users/{user_id}', ('DELETE',)),
    ('/admin/users/{user_id}', ('GET',)),
    ('/admin/users/{user_id}', ('PATCH',)),
    ('/admin/users/{user_id}/platforms', ('GET',)),
    ('/admin/users/{user_id}/platforms', ('POST',)),
    ('/admin/users/{user_id}/platforms/{provider}/{provider_user_id}', ('DELETE',)),
    ('/admin/users/{user_id}/tokens', ('GET',)),
    ('/admin/users/{user_id}/tokens', ('POST',)),
    ('/admin/users/{user_id}/tokens/rotate', ('POST',)),
    ('/admin/users/{user_id}/tokens/{token_hash_prefix}', ('DELETE',)),
    ('/agents/templates', ('GET',)),
    ('/agents/threads', ('GET',)),
    ('/agents/threads', ('POST',)),
    ('/autonomous/stream', ('GET',)),
    ('/browser-commands/{command_id}/result', ('POST',)),
    ('/chat', ('POST',)),
    ('/chat/sync', ('POST',)),
    ('/cli-config/{command_id}/result', ('POST',)),
    ('/cliproxy/apply-route', ('POST',)),
    ('/cliproxy/auth-files', ('GET',)),
    ('/cliproxy/auth-files', ('POST',)),
    ('/cliproxy/auth-files/{name}', ('DELETE',)),
    ('/cliproxy/auth-files/{name}', ('PATCH',)),
    ('/cliproxy/catalog', ('GET',)),
    ('/cliproxy/config', ('GET',)),
    ('/cliproxy/config', ('PATCH',)),
    ('/cliproxy/models', ('GET',)),
    ('/cliproxy/oauth/callback', ('POST',)),
    ('/cliproxy/oauth/start', ('POST',)),
    ('/cliproxy/oauth/status', ('GET',)),
    ('/cliproxy/status', ('GET',)),
    ('/cliproxy/verify', ('POST',)),
    ('/commands', ('GET',)),
    ('/commands/execute', ('POST',)),
    ('/commands/options/{ref}', ('GET',)),
    ('/connect/credentials/{prompt_id}/cancel', ('POST',)),
    ('/connect/credentials/{prompt_id}/exit', ('POST',)),
    ('/connect/credentials/{prompt_id}/prompt', ('GET',)),
    ('/connect/credentials/{prompt_id}/submit', ('POST',)),
    ('/connect/credentials/{prompt_id}/test', ('POST',)),
    ('/credential-bindings/{binding_id}', ('DELETE',)),
    ('/credential-prompts/{prompt_id}/cancel', ('POST',)),
    ('/credential-prompts/{prompt_id}/exit', ('POST',)),
    ('/credential-prompts/{prompt_id}/status', ('GET',)),
    ('/credential-prompts/{prompt_id}/submit', ('POST',)),
    ('/credential-prompts/{prompt_id}/test', ('POST',)),
    ('/credential-setup-sessions', ('POST',)),
    ('/credentials', ('GET',)),
    ('/credentials', ('POST',)),
    ('/credentials/{credential_id}', ('DELETE',)),
    ('/credentials/{credential_id}', ('GET',)),
    ('/credentials/{credential_id}', ('PATCH',)),
    ('/credentials/{credential_id}/bindings', ('GET',)),
    ('/credentials/{credential_id}/bindings', ('POST',)),
    ('/credentials/{credential_id}/test', ('POST',)),
    ('/devices/register', ('POST',)),
    ('/devices/{token}', ('DELETE',)),
    ('/health', ('GET',)),
    ('/health/stream', ('GET',)),
    ('/hooks', ('GET',)),
    ('/hooks', ('POST',)),
    ('/hooks/approvals', ('GET',)),
    ('/hooks/approvals/{record_id}/resolve', ('POST',)),
    ('/hooks/executions', ('GET',)),
    ('/hooks/schema', ('GET',)),
    ('/hooks/templates', ('GET',)),
    ('/hooks/templates/{template_id}/install', ('POST',)),
    ('/hooks/{hook_id}', ('DELETE',)),
    ('/hooks/{hook_id}', ('GET',)),
    ('/hooks/{hook_id}', ('PATCH',)),
    ('/hooks/{hook_id}/test', ('POST',)),
    ('/integrations/teams/webhook', ('POST',)),
    ('/integrations/whatsapp/webhook', ('GET',)),
    ('/integrations/whatsapp/webhook', ('POST',)),
    ('/llm/fallback-approvals', ('GET',)),
    ('/llm/fallback-approvals/{record_id}/resolve', ('POST',)),
    ('/mcp-servers', ('GET',)),
    ('/mcp-servers', ('POST',)),
    ('/mcp-servers/install', ('POST',)),
    ('/mcp-servers/install/preview', ('POST',)),
    ('/mcp-servers/install/preview-upload', ('POST',)),
    ('/mcp-servers/{server_id}', ('DELETE',)),
    ('/mcp-servers/{server_id}', ('GET',)),
    ('/mcp-servers/{server_id}', ('PUT',)),
    ('/mcp-servers/{server_id}/discover', ('POST',)),
    ('/mcp-servers/{server_id}/retry', ('POST',)),
    ('/mcp-servers/{server_id}/test', ('POST',)),
    ('/me', ('GET',)),
    ('/me', ('PATCH',)),
    ('/me/platform-link-codes', ('POST',)),
    ('/me/platforms', ('GET',)),
    ('/me/telegram-bots', ('GET',)),
    ('/me/telegram-bots', ('POST',)),
    ('/me/telegram-bots/{bot_id}', ('DELETE',)),
    ('/me/telegram-bots/{bot_id}', ('GET',)),
    ('/me/tokens', ('GET',)),
    ('/me/tokens', ('POST',)),
    ('/me/tokens/{token_hash_prefix}', ('DELETE',)),
    ('/models', ('GET',)),
    ('/models/available', ('GET',)),
    ('/models/available', ('POST',)),
    ('/notifications', ('DELETE',)),
    ('/notifications', ('GET',)),
    ('/notifications/channel-types', ('GET',)),
    ('/notifications/destinations', ('GET',)),
    ('/notifications/destinations', ('POST',)),
    ('/notifications/destinations/{dest_id}', ('DELETE',)),
    ('/notifications/destinations/{dest_id}', ('GET',)),
    ('/notifications/destinations/{dest_id}', ('PATCH',)),
    ('/notifications/destinations/{dest_id}/test', ('POST',)),
    ('/notifications/external', ('POST',)),
    ('/notifications/preferences', ('GET',)),
    ('/notifications/preferences', ('PATCH',)),
    ('/notifications/profiles', ('GET',)),
    ('/notifications/profiles', ('POST',)),
    ('/notifications/profiles/{profile_id}', ('DELETE',)),
    ('/notifications/profiles/{profile_id}', ('GET',)),
    ('/notifications/profiles/{profile_id}', ('PATCH',)),
    ('/notifications/read-all', ('POST',)),
    ('/notifications/{notification_id}', ('DELETE',)),
    ('/notifications/{notification_id}/read', ('POST',)),
    ('/platform/resolve', ('GET',)),
    ('/ready', ('GET',)),
    ('/report', ('POST',)),
    ('/restart', ('POST',)),
    ('/scheduler/missed-work/run', ('POST',)),
    ('/scheduler/status', ('GET',)),
    ('/settings', ('GET',)),
    ('/settings', ('PATCH',)),
    ('/settings/dream-prompts', ('GET',)),
    ('/settings/dream-prompts', ('PUT',)),
    ('/settings/env', ('GET',)),
    ('/settings/env/{key}', ('GET',)),
    ('/settings/global-skills', ('GET',)),
    ('/settings/global-skills', ('PUT',)),
    ('/settings/llm/providers', ('GET',)),
    ('/settings/llm/runtime', ('GET',)),
    ('/settings/llm/test', ('POST',)),
    ('/settings/llm/test-suite', ('POST',)),
    ('/settings/rag/catalog', ('GET',)),
    ('/settings/system-prompt', ('DELETE',)),
    ('/settings/system-prompt', ('GET',)),
    ('/settings/system-prompt', ('PUT',)),
    ('/skills', ('GET',)),
    ('/skills/install', ('POST',)),
    ('/skills/marketplace/search', ('GET',)),
    ('/skills/{name}', ('DELETE',)),
    ('/skills/{name}', ('GET',)),
    ('/status/turns', ('GET',)),
    ('/thread-teams', ('GET',)),
    ('/thread-teams', ('POST',)),
    ('/thread-teams/{team_id}', ('DELETE',)),
    ('/thread-teams/{team_id}', ('PATCH',)),
    ('/thread-teams/{team_id}/memories', ('GET',)),
    ('/thread-teams/{team_id}/memories', ('POST',)),
    ('/thread-teams/{team_id}/memories/{key}', ('DELETE',)),
    ('/threads', ('GET',)),
    ('/threads/import', ('POST',)),
    ('/threads/{thread_id}', ('DELETE',)),
    ('/threads/{thread_id}/attachment_limits', ('GET',)),
    ('/threads/{thread_id}/attachments/validate', ('POST',)),
    ('/threads/{thread_id}/attachments/{attachment_id}/download', ('GET',)),
    ('/threads/{thread_id}/branch', ('POST',)),
    ('/threads/{thread_id}/callable-tools', ('GET',)),
    ('/threads/{thread_id}/chatapp/bind-code', ('POST',)),
    ('/threads/{thread_id}/chatapp/bindings', ('GET',)),
    ('/threads/{thread_id}/chatapp/bindings/{binding_id}', ('DELETE',)),
    ('/threads/{thread_id}/checkpoint', ('GET',)),
    ('/threads/{thread_id}/claim', ('POST',)),
    ('/threads/{thread_id}/clear', ('POST',)),
    ('/threads/{thread_id}/compact', ('POST',)),
    ('/threads/{thread_id}/config', ('DELETE',)),
    ('/threads/{thread_id}/config', ('GET',)),
    ('/threads/{thread_id}/config', ('PATCH',)),
    ('/threads/{thread_id}/context', ('GET',)),
    ('/threads/{thread_id}/dream', ('POST',)),
    ('/threads/{thread_id}/export', ('GET',)),
    ('/threads/{thread_id}/history', ('GET',)),
    ('/threads/{thread_id}/metadata', ('GET',)),
    ('/threads/{thread_id}/metadata', ('PATCH',)),
    ('/threads/{thread_id}/notepad', ('GET',)),
    ('/threads/{thread_id}/notepad', ('PUT',)),
    ('/threads/{thread_id}/overview', ('GET',)),
    ('/threads/{thread_id}/prune', ('POST',)),
    ('/threads/{thread_id}/rewind', ('POST',)),
    ('/threads/{thread_id}/skills', ('GET',)),
    ('/threads/{thread_id}/status', ('GET',)),
    ('/threads/{thread_id}/stop', ('POST',)),
    ('/threads/{thread_id}/turn/stream', ('GET',)),
    ('/todos', ('GET',)),
    ('/todos', ('POST',)),
    ('/todos/thread-counts', ('GET',)),
    ('/todos/users', ('GET',)),
    ('/todos/{todo_id}', ('DELETE',)),
    ('/todos/{todo_id}', ('PATCH',)),
    ('/todos/{todo_id}/complete', ('POST',)),
    ('/todos/{todo_id}/delivery-report', ('POST',)),
    ('/tools', ('GET',)),
    ('/tools/categories', ('GET',)),
    ('/tools/custom', ('GET',)),
    ('/tools/custom', ('POST',)),
    ('/tools/custom/export', ('GET',)),
    ('/tools/custom/import', ('POST',)),
    ('/tools/custom/{tool_id}', ('DELETE',)),
    ('/tools/custom/{tool_id}', ('GET',)),
    ('/tools/custom/{tool_id}', ('PUT',)),
    ('/tools/custom/{tool_id}/test', ('POST',)),
    ('/tools/defaults', ('DELETE',)),
    ('/tools/defaults', ('GET',)),
    ('/tools/defaults', ('PUT',)),
    ('/tools/optional', ('GET',)),
    ('/tools/unified', ('POST',)),
    ('/tools/unified/{tool_id}', ('DELETE',)),
    ('/tools/unified/{tool_id}', ('PUT',)),
    ('/triggers', ('GET',)),
    ('/triggers', ('POST',)),
    ('/triggers/executions/recent', ('GET',)),
    ('/triggers/fire/{trigger_id}', ('POST',)),
    ('/triggers/sources/list', ('GET',)),
    ('/triggers/sources/reload', ('POST',)),
    ('/triggers/{trigger_id}', ('DELETE',)),
    ('/triggers/{trigger_id}', ('GET',)),
    ('/triggers/{trigger_id}', ('PATCH',)),
    ('/triggers/{trigger_id}/executions', ('GET',)),
    ('/triggers/{trigger_id}/test', ('POST',)),
    ('/ui-prompts/{prompt_id}/result', ('POST',)),
    ('/users/{user_id}/memories', ('GET',)),
    ('/users/{user_id}/memories', ('POST',)),
    ('/users/{user_id}/memories/search', ('GET',)),
    ('/users/{user_id}/memories/{key}', ('DELETE',)),
    ('/users/{user_id}/rag/index', ('DELETE',)),
    ('/users/{user_id}/rag/reindex', ('POST',)),
    ('/users/{user_id}/rag/search', ('GET',)),
    ('/users/{user_id}/rag/settings', ('GET',)),
    ('/users/{user_id}/rag/settings', ('PUT',)),
    ('/users/{user_id}/rag/stats', ('GET',)),
    ('/users/{user_id}/tools', ('GET',)),
    ('/users/{user_id}/tools/preferences', ('GET',)),
    ('/users/{user_id}/tools/reset', ('POST',)),
    ('/users/{user_id}/tools/search', ('GET',)),
    ('/users/{user_id}/tools/unified', ('GET',)),
    ('/users/{user_id}/tools/unified/{tool_id}/config', ('PUT',)),
    ('/users/{user_id}/tools/unified/{tool_id}/description', ('PUT',)),
    ('/users/{user_id}/tools/unified/{tool_id}/enable', ('PUT',)),
    ('/users/{user_id}/tools/{tool_name}/config', ('PUT',)),
    ('/voice/chat', ('POST',)),
    ('/voice/stt', ('POST',)),
    ('/voice/tts', ('POST',)),
    ('/workflows/approvals', ('GET',)),
    ('/workflows/approvals/{record_id}/resolve', ('POST',)),
    ('/workflows/approve', ('POST',)),
    ('/workflows/decline', ('POST',)),
    ('/workflows/pending', ('GET',)),
    ('/workflows/runs', ('GET',)),
    ('/workflows/runs/{workflow_id}', ('GET',)),
    ('/workflows/source', ('GET',)),
    ('/workflows/templates', ('GET',)),
    ('/workflows/templates/{template_id}/install', ('POST',)),
    ('/workflows/{workflow_id}/execute', ('POST',)),
    ('/workspace/download', ('GET',)),
]


def _schema_routes(app):
    return sorted(
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    )


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent


def test_api_schema_route_inventory_is_stable(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)

    actual = _schema_routes(client.app)
    added = sorted(set(actual) - set(EXPECTED_ROUTES))
    removed = sorted(set(EXPECTED_ROUTES) - set(actual))
    assert actual == EXPECTED_ROUTES, (
        "Route inventory drift. If intentional, regenerate EXPECTED_ROUTES from "
        "Nymeria/ with:\n"
        "    PYTHONPATH=. python3 tests/test_api_route_inventory.py\n"
        f"added: {added}\nremoved: {removed}"
    )
    assert agent.synced_tools == 1


def test_public_health_does_not_require_auth(tmp_path: Path, api_client_builder):
    client, _agent = _client(tmp_path, api_client_builder)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == __version__


def test_public_ready_checks_dependencies_without_auth(
    tmp_path: Path,
    api_client_builder,
):
    client, _agent = _client(tmp_path, api_client_builder)

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "skipped"


def test_public_ready_returns_503_when_dependency_fails(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(tmp_path, redis_enabled=True, redis_url=None)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["redis"] == {
        "status": "error",
        "detail": "REDIS_URL is unset",
    }


def test_public_ready_probes_on_dedicated_readiness_thread(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    # The blocking dependency checks must run on the dedicated single-thread
    # "readiness" executor, never the asyncio default executor, so a
    # saturated shared pool cannot make the process look unready (and invite
    # an orchestrator restart of the only agent runtime).
    import threading

    from nymeria.api.routers import system as system_module

    recorded: list[str] = []
    real_build = system_module._build_readiness

    def _recording_build(settings):
        recorded.append(threading.current_thread().name)
        return real_build(settings)

    monkeypatch.setattr(system_module, "_build_readiness", _recording_build)
    client, _agent = _client(tmp_path, api_client_builder)

    response = client.get("/ready")

    assert response.status_code == 200
    assert recorded, "readiness probe never ran the dependency checks"
    assert all(name.startswith("readiness") for name in recorded)


def test_api_responses_include_baseline_security_headers(
    tmp_path: Path,
    api_client_builder,
):
    client, _agent = _client(tmp_path, api_client_builder)

    response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in response.headers


def test_csp_locks_down_the_dangerous_directives(
    tmp_path: Path,
    api_client_builder,
):
    """The CSP's value is in what it forbids, so assert that, not its presence.

    `script-src` must never carry 'unsafe-inline': inline injection is the
    vector the policy exists to stop, and the SPA's own bootstrap scripts are
    admitted by hash instead (see `_build_csp`).
    """
    client, _agent = _client(tmp_path, api_client_builder)

    csp = client.get("/health").headers["content-security-policy"]
    directives = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in csp.split(";")
        if part.strip()
    }

    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert directives["object-src"] == "object-src 'none'"
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"
    assert directives["base-uri"] == "base-uri 'self'"
    assert directives["default-src"] == "default-src 'self'"


def test_csp_admits_every_inline_script_in_the_served_index(tmp_path: Path):
    """Regression for the failure mode a pinned hash list would have.

    One of the SPA's inline scripts embeds a build-hashed module filename, so
    its hash changes on every frontend build. If the header ever stops being
    derived from the index.html actually being served, the page dies with a
    console full of CSP violations and nothing else fails first.
    """
    import base64
    import hashlib

    from nymeria.triggers.api import (
        _INLINE_SCRIPT_RE,
        _build_csp,
        _frontend_static_dir,
    )

    frontend_dir = Path(_frontend_static_dir())
    index_path = frontend_dir / "index.html"
    if not index_path.is_file():
        pytest.skip("no built frontend in this checkout")

    csp = _build_csp(str(frontend_dir))
    script_src = next(
        part for part in csp.split(";") if part.strip().startswith("script-src")
    )

    inline_scripts = _INLINE_SCRIPT_RE.findall(index_path.read_bytes())
    assert inline_scripts, "index.html has no inline scripts; the regex may have rotted"
    for body in inline_scripts:
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
        assert f"'sha256-{digest}'" in script_src


def test_csp_allows_every_external_origin_the_build_loads(tmp_path: Path):
    """Fail here, not in the browser, when the frontend gains a new CDN.

    The first draft of this policy blocked the webfont stylesheet, its font
    files, and the Office.js shim, because those are loaded from origins the
    Tauri baseline never had to name. Nothing in the build pipeline would have
    reported that: the page renders, just wrong. So the invariant is executable.

    Only `<link>` and `<script>` tags are scanned. Anchor `href`s are plain
    navigation and no directive in this policy governs them.
    """
    import re as _re

    from nymeria.triggers.api import _build_csp, _frontend_static_dir

    frontend_dir = Path(_frontend_static_dir())
    index_path = frontend_dir / "index.html"
    if not index_path.is_file():
        pytest.skip("no built frontend in this checkout")

    csp = _build_csp(str(frontend_dir))
    html = index_path.read_text()

    loaded = _re.findall(
        r"<(?:link|script)\b[^>]*?(?:href|src)=\"(https?://[^\"]+)\"",
        html,
        _re.DOTALL | _re.IGNORECASE,
    )
    origins = {_re.match(r"https?://[^/]+", url).group(0) for url in loaded}
    assert origins, "index.html loads nothing external; the scan may have rotted"

    missing = sorted(origin for origin in origins if origin not in csp)
    assert not missing, (
        f"the built frontend loads from {missing}, which the CSP does not allow. "
        f"Add the origin to the _CSP_* constants in nymeria/triggers/api.py, or "
        f"self-host the asset."
    )


def test_csp_survives_a_missing_frontend_build(tmp_path: Path):
    """A source checkout with no built SPA must still get a policy, not a crash."""
    from nymeria.triggers.api import _build_csp

    csp = _build_csp(str(tmp_path))

    assert "script-src 'self'" in csp
    assert "sha256-" not in csp


def test_api_responses_include_request_id(
    tmp_path: Path,
    api_client_builder,
):
    client, _agent = _client(tmp_path, api_client_builder)

    generated = client.get("/health")
    propagated = client.get("/health", headers={"X-Request-ID": "req-test-123"})
    sanitized = client.get("/health", headers={"X-Request-ID": "bad\nvalue"})

    assert generated.headers["x-request-id"]
    assert propagated.headers["x-request-id"] == "req-test-123"
    assert sanitized.headers["x-request-id"] != "bad\nvalue"


def test_api_docs_and_schema_are_disabled_by_default(tmp_path: Path, api_client_builder):
    client, _agent = _client(tmp_path, api_client_builder)

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_and_schema_can_be_enabled(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path, api_docs_enabled=True)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "Nymeria API"
    assert schema.json()["info"]["version"] == __version__


def test_frontend_spa_fallback_serves_browser_routes(
    tmp_path: Path, monkeypatch, caplog, api_client_builder
):
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<main>Nymeria SPA</main>", encoding="utf-8")
    (frontend_dir / "wolfhead-transparent.png").write_bytes(b"fake-png")
    (frontend_dir / "_app").mkdir()
    (frontend_dir / "_app" / "version.json").write_text('{"version":"test"}', encoding="utf-8")
    (frontend_dir / "static").mkdir()
    (frontend_dir / "static" / "extra.txt").write_text("extra asset", encoding="utf-8")
    monkeypatch.setattr(api_module, "_frontend_static_dir", lambda: str(frontend_dir))
    caplog.set_level(logging.WARNING, logger="nymeria.triggers.api")
    client, _agent = _client(tmp_path, api_client_builder)

    root = client.get("/")
    fallback = client.get("/dashboard", headers={"accept": "text/html"})
    app_asset = client.get("/_app/version.json")
    root_asset = client.get("/wolfhead-transparent.png")
    nested_asset = client.get("/static/extra.txt")

    assert root.status_code == 200
    assert fallback.status_code == 200
    assert app_asset.status_code == 200
    assert root_asset.status_code == 200
    assert nested_asset.status_code == 200
    assert root.text == "<main>Nymeria SPA</main>"
    assert fallback.text == "<main>Nymeria SPA</main>"
    assert app_asset.json() == {"version": "test"}
    assert root_asset.content == b"fake-png"
    assert nested_asset.text == "extra asset"
    assert "Skipping frontend asset that conflicts with an API route: /_app" not in caplog.text


def test_frontend_spa_fallback_preserves_api_and_asset_404s(
    tmp_path: Path, monkeypatch, api_client_builder
):
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<main>Nymeria SPA</main>", encoding="utf-8")
    monkeypatch.setattr(api_module, "_frontend_static_dir", lambda: str(frontend_dir))
    client, _agent = _client(tmp_path, api_client_builder)

    api_miss = client.get("/threads/not-a-route/unknown", headers={"accept": "text/html"})
    asset_miss = client.get("/missing.js", headers={"accept": "text/html"})
    json_miss = client.get("/dashboard", headers={"accept": "application/json"})

    assert api_miss.status_code == 404
    assert asset_miss.status_code == 404
    assert json_miss.status_code == 404


def test_frontend_static_dir_prefers_packaged_frontend_with_index(
    tmp_path: Path, monkeypatch
):
    fake_api_file = tmp_path / "site-packages" / "nymeria" / "triggers" / "api.py"
    package_frontend = fake_api_file.parents[1] / "frontend"
    source_frontend = fake_api_file.parents[2] / "frontend"
    package_frontend.mkdir(parents=True)
    source_frontend.mkdir(parents=True)
    (package_frontend / "index.html").write_text("package", encoding="utf-8")
    (source_frontend / "index.html").write_text("source", encoding="utf-8")
    monkeypatch.setattr(api_module, "__file__", str(fake_api_file))

    assert api_module._frontend_static_dir() == str(package_frontend)


def test_frontend_static_dir_falls_back_to_source_frontend_when_package_is_empty(
    tmp_path: Path, monkeypatch
):
    fake_api_file = tmp_path / "checkout" / "nymeria" / "triggers" / "api.py"
    package_frontend = fake_api_file.parents[1] / "frontend"
    source_frontend = fake_api_file.parents[2] / "frontend"
    package_frontend.mkdir(parents=True)
    source_frontend.mkdir(parents=True)
    (source_frontend / "index.html").write_text("source", encoding="utf-8")
    monkeypatch.setattr(api_module, "__file__", str(fake_api_file))

    assert api_module._frontend_static_dir() == str(source_frontend)


def test_device_registration_is_bound_to_authenticated_user(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    token = agent.accounts_repo.issue_token("alice")

    response = client.post(
        "/devices/register",
        headers=api_client_builder.auth(token),
        json={
            "token": "device-token",
            "platform": "android",
            "user_id": "bob",
            "thread_ids": ["thread-1"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "registered", "platform": "android"}
    stored = json.loads((tmp_path / "fcm_tokens.json").read_text(encoding="utf-8"))
    assert stored == [
        {
            "token": "device-token",
            "platform": "android",
            "user_id": "alice",
            "thread_ids": ["thread-1"],
        }
    ]


def test_workspace_download_scopes_non_admin_to_own_generated_images(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    artifact = workspace_dir / "artifact.txt"
    artifact.write_text("workspace data", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    # Per-user generated-image dirs mirror tools.image_generation.generated_image_dir.
    alice_img_dir = workspace_dir / "images" / "generated" / "alice"
    alice_img_dir.mkdir(parents=True)
    alice_image = alice_img_dir / "gen.png"
    alice_image.write_bytes(b"alice-image")
    bob_img_dir = workspace_dir / "images" / "generated" / "bob"
    bob_img_dir.mkdir(parents=True)
    bob_image = bob_img_dir / "gen.png"
    bob_image.write_bytes(b"bob-image")
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(workspace_dir))

    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    user_token = agent.accounts_repo.issue_token("alice")
    admin_token = agent.accounts_repo.issue_token("admin")

    def _get(token, path):
        return client.get("/workspace/download", headers=api_client_builder.auth(token), params={"path": str(path)})

    # Non-admin: can read their own generated image, but not arbitrary workspace
    # files nor another user's generated images.
    assert _get(user_token, alice_image).status_code == 200
    assert _get(user_token, alice_image).content == b"alice-image"
    assert _get(user_token, artifact).status_code == 403
    assert _get(user_token, bob_image).status_code == 403

    # Admin: full workspace access; still blocked outside the workspace.
    assert _get(admin_token, artifact).status_code == 200
    assert _get(admin_token, artifact).text == "workspace data"
    assert _get(admin_token, outside).status_code == 403


def test_user_auth_contract_for_me_endpoint(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    token = agent.accounts_repo.issue_token("alice")

    missing = client.get("/me")
    invalid = client.get("/me", headers=api_client_builder.auth("nym_invalid"))
    valid = client.get("/me", headers=api_client_builder.auth(token))

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert valid.json() == {
        "id": "alice",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "user",
    }


def test_failed_auth_attempts_are_rate_limited_without_blocking_valid_tokens(
    tmp_path: Path, monkeypatch, request, api_client_builder
):
    api_module._reset_auth_failure_rate_limiter_for_tests()
    request.addfinalizer(api_module._reset_auth_failure_rate_limiter_for_tests)
    monkeypatch.setattr(api_module, "_AUTH_FAILURE_RATE_LIMIT", 2)
    monkeypatch.setattr(api_module, "_AUTH_FAILURE_RATE_WINDOW_SECONDS", 60.0)
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    token = agent.accounts_repo.issue_token("alice")

    first = client.get("/me")
    second = client.get("/me", headers={"Authorization": "Basic nope"})
    limited = client.get("/me", headers=api_client_builder.auth("nym_invalid_3"))
    valid = client.get("/me", headers=api_client_builder.auth(token))

    assert first.status_code == 401
    assert second.status_code == 401
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too many failed authentication attempts"
    assert int(limited.headers["retry-after"]) >= 1
    assert valid.status_code == 200
    assert valid.json()["id"] == "alice"


def test_admin_only_endpoint_rejects_user_and_allows_admin(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    user_token = agent.accounts_repo.issue_token("alice")
    admin_token = agent.accounts_repo.issue_token("admin")

    forbidden = client.get("/admin/users", headers=api_client_builder.auth(user_token))
    allowed = client.get("/admin/users", headers=api_client_builder.auth(admin_token))

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert {row["id"] for row in allowed.json()} == {"alice", "admin"}


def test_deprecated_nymeria_api_key_is_hidden_from_config_api(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    admin_token = agent.accounts_repo.issue_token("admin")

    listed = client.get("/settings/env", headers=api_client_builder.auth(admin_token))

    assert listed.status_code == 200
    entries = listed.json()["entries"]
    assert "nymeria_api_key" not in {entry["name"] for entry in entries}
    assert "NYMERIA_API_KEY" not in {entry["env_var"] for entry in entries}

    lower = client.get("/settings/env/nymeria_api_key", headers=api_client_builder.auth(admin_token))
    upper = client.get("/settings/env/NYMERIA_API_KEY", headers=api_client_builder.auth(admin_token))

    assert lower.status_code == 404
    assert upper.status_code == 404


def test_admin_bot_endpoint_rate_limit_is_per_admin_and_endpoint(
    tmp_path: Path, monkeypatch, request, api_client_builder
):
    api_module._reset_admin_bot_endpoint_rate_limiter_for_tests()
    request.addfinalizer(api_module._reset_admin_bot_endpoint_rate_limiter_for_tests)
    monkeypatch.setattr(api_module, "_BOT_ADMIN_ENDPOINT_RATE_LIMIT", 2)
    monkeypatch.setattr(api_module, "_BOT_ADMIN_ENDPOINT_RATE_WINDOW_SECONDS", 60.0)
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("service", "service@example.com", "Service", role="admin")
    agent.accounts_repo.create_user("other", "other@example.com", "Other", role="admin")
    service_token = agent.accounts_repo.issue_token("service")
    other_token = agent.accounts_repo.issue_token("other")

    for _ in range(2):
        response = client.get(
            "/admin/chatapp/bindings",
            headers=api_client_builder.auth(service_token),
            params={"provider": "telegram"},
        )
        assert response.status_code == 200

    limited = client.get(
        "/admin/chatapp/bindings",
        headers=api_client_builder.auth(service_token),
        params={"provider": "telegram"},
    )
    same_admin_other_endpoint = client.get(
        "/admin/chatapp/bindings/lookup",
        headers=api_client_builder.auth(service_token),
        params={"provider": "telegram", "platform_chat_id": "123"},
    )
    other_admin_same_endpoint = client.get(
        "/admin/chatapp/bindings",
        headers=api_client_builder.auth(other_token),
        params={"provider": "telegram"},
    )

    assert limited.status_code == 429
    assert limited.json()["detail"] == "Rate limit exceeded for admin bot endpoint"
    assert int(limited.headers["retry-after"]) >= 1
    assert same_admin_other_endpoint.status_code == 404
    assert other_admin_same_endpoint.status_code == 200
    api_module._reset_admin_bot_endpoint_rate_limiter_for_tests()


def test_admin_service_token_style_act_as_resolves_target_user(
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    agent.accounts_repo.create_user("service", "service@example.com", "Service", role="admin")
    service_token = agent.accounts_repo.issue_token("service")

    response = client.get("/me", headers=api_client_builder.auth(service_token, **{"X-Nymeria-Act-As": "alice"}))

    assert response.status_code == 200
    assert response.json() == {
        "id": "alice",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "user",
    }


def test_non_admin_act_as_is_rejected(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob")
    token = agent.accounts_repo.issue_token("alice")

    response = client.get("/me", headers=api_client_builder.auth(token, **{"X-Nymeria-Act-As": "bob"}))

    assert response.status_code == 403
    assert response.json()["detail"] == "Act-As requires admin"


def test_create_api_app_multi_container_mints_service_token(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """Full Docker stack (Redis on, no operator token): the api self-mints the
    internal service token onto the shared data volume so the sibling worker /
    mcp containers can read it without operator provisioning."""
    from nymeria.core.event_bus import EventBus
    from nymeria.core.service_bootstrap import SLIM_SERVICE_TOKEN_FILENAME

    # Only the mint side effect matters here; avoid standing up a real Redis bus.
    monkeypatch.setattr(
        "nymeria.core.event_bus.create_event_bus",
        lambda settings, **kwargs: EventBus(),
    )
    settings = api_client_builder.settings(
        tmp_path, redis_enabled=True, redis_url="redis://localhost:6379/0"
    )
    agent = FakeAgent(tmp_path)
    # Constructing the app runs the synchronous mint block before serving.
    api_client_builder.client(agent, settings)

    token_file = tmp_path / SLIM_SERVICE_TOKEN_FILENAME
    assert token_file.is_file()
    minted = token_file.read_text(encoding="utf-8").strip()
    assert minted.startswith("nym_")
    assert getattr(settings, "nymeria_service_token", None) == minted


def test_create_api_app_without_redis_does_not_mint_service_token(
    tmp_path: Path, api_client_builder
):
    """A local SQLite api (no Redis) must NOT self-mint; that path is unchanged
    so existing local/dev behavior and redis-off unit tests are unaffected."""
    from nymeria.core.service_bootstrap import SLIM_SERVICE_TOKEN_FILENAME

    settings = api_client_builder.settings(tmp_path)  # redis off by default
    agent = FakeAgent(tmp_path)
    api_client_builder.client(agent, settings)

    assert not (tmp_path / SLIM_SERVICE_TOKEN_FILENAME).exists()
    assert getattr(settings, "nymeria_service_token", None) is None


if __name__ == "__main__":
    # Dump the current route inventory formatted as the EXPECTED_ROUTES literal so
    # an intentional route change is a copy-paste rather than a hand-edit. The
    # schema route set is independent of settings/agent (frontend SPA mounts are
    # registered with include_in_schema=False), so a throwaway agent is enough.
    import tempfile

    with tempfile.TemporaryDirectory() as _tmp:
        _app = api_module.create_api_app(FakeAgent(Path(_tmp)))  # type: ignore[bad-argument-type]
        print("EXPECTED_ROUTES = [")
        for _path, _methods in _schema_routes(_app):
            print(f"    {(_path, _methods)!r},")
        print("]")
