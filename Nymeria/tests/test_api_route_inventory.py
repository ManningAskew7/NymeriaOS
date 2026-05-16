"""Regression tests for the FastAPI route surface and auth contracts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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


EXPECTED_ROUTES = [
    ("/activity", ("GET",)),
    ("/admin/chatapp/bindings", ("GET",)),
    ("/admin/chatapp/bindings/by-chat", ("DELETE",)),
    ("/admin/chatapp/bindings/claim", ("POST",)),
    ("/admin/chatapp/bindings/claim-via-bot", ("POST",)),
    ("/admin/chatapp/bindings/lookup", ("GET",)),
    ("/admin/chatapp/bindings/switch", ("POST",)),
    ("/admin/platform/link-codes/claim", ("POST",)),
    ("/admin/telegram-bots", ("GET",)),
    ("/admin/telegram-bots/{bot_id}/seen", ("POST",)),
    ("/admin/users", ("GET",)),
    ("/admin/users", ("POST",)),
    ("/admin/users/{user_id}", ("DELETE",)),
    ("/admin/users/{user_id}", ("GET",)),
    ("/admin/users/{user_id}", ("PATCH",)),
    ("/admin/users/{user_id}/platforms", ("GET",)),
    ("/admin/users/{user_id}/platforms", ("POST",)),
    ("/admin/users/{user_id}/platforms/{provider}/{provider_user_id}", ("DELETE",)),
    ("/admin/users/{user_id}/tokens", ("GET",)),
    ("/admin/users/{user_id}/tokens", ("POST",)),
    ("/admin/users/{user_id}/tokens/rotate", ("POST",)),
    ("/admin/users/{user_id}/tokens/{token_hash_prefix}", ("DELETE",)),
    ("/agents/templates", ("GET",)),
    ("/agents/threads", ("GET",)),
    ("/agents/threads", ("POST",)),
    ("/autonomous/stream", ("GET",)),
    ("/chat", ("POST",)),
    ("/chat/sync", ("POST",)),
    ("/commands", ("GET",)),
    ("/commands/execute", ("POST",)),
    ("/credential-bindings/{binding_id}", ("DELETE",)),
    ("/credential-setup-sessions", ("POST",)),
    ("/credentials", ("GET",)),
    ("/credentials", ("POST",)),
    ("/credentials/{credential_id}", ("DELETE",)),
    ("/credentials/{credential_id}", ("GET",)),
    ("/credentials/{credential_id}", ("PATCH",)),
    ("/credentials/{credential_id}/bindings", ("GET",)),
    ("/credentials/{credential_id}/bindings", ("POST",)),
    ("/credentials/{credential_id}/test", ("POST",)),
    ("/devices/register", ("POST",)),
    ("/devices/{token}", ("DELETE",)),
    ("/health", ("GET",)),
    ("/mcp-servers", ("GET",)),
    ("/mcp-servers", ("POST",)),
    ("/mcp-servers/install", ("POST",)),
    ("/mcp-servers/install/preview", ("POST",)),
    ("/mcp-servers/install/preview-upload", ("POST",)),
    ("/mcp-servers/{server_id}", ("DELETE",)),
    ("/mcp-servers/{server_id}", ("GET",)),
    ("/mcp-servers/{server_id}", ("PUT",)),
    ("/mcp-servers/{server_id}/discover", ("POST",)),
    ("/mcp-servers/{server_id}/retry", ("POST",)),
    ("/mcp-servers/{server_id}/test", ("POST",)),
    ("/me", ("GET",)),
    ("/me", ("PATCH",)),
    ("/me/platform-link-codes", ("POST",)),
    ("/me/platforms", ("GET",)),
    ("/me/telegram-bots", ("GET",)),
    ("/me/telegram-bots", ("POST",)),
    ("/me/telegram-bots/{bot_id}", ("DELETE",)),
    ("/me/telegram-bots/{bot_id}", ("GET",)),
    ("/me/tokens", ("GET",)),
    ("/me/tokens", ("POST",)),
    ("/me/tokens/{token_hash_prefix}", ("DELETE",)),
    ("/models", ("GET",)),
    ("/models/available", ("GET",)),
    ("/notifications", ("GET",)),
    ("/notifications/read-all", ("POST",)),
    ("/notifications/{notification_id}/read", ("POST",)),
    ("/platform/resolve", ("GET",)),
    ("/ready", ("GET",)),
    ("/report", ("POST",)),
    ("/restart", ("POST",)),
    ("/settings", ("GET",)),
    ("/settings", ("PATCH",)),
    ("/settings/env", ("GET",)),
    ("/settings/env/{key}", ("GET",)),
    ("/settings/global-skills", ("GET",)),
    ("/settings/global-skills", ("PUT",)),
    ("/settings/llm/providers", ("GET",)),
    ("/settings/llm/runtime", ("GET",)),
    ("/settings/llm/test", ("POST",)),
    ("/settings/llm/test-suite", ("POST",)),
    ("/skills", ("GET",)),
    ("/skills/install", ("POST",)),
    ("/skills/marketplace/search", ("GET",)),
    ("/skills/{name}", ("DELETE",)),
    ("/skills/{name}", ("GET",)),
    ("/thread-teams", ("GET",)),
    ("/thread-teams", ("POST",)),
    ("/thread-teams/{team_id}", ("DELETE",)),
    ("/thread-teams/{team_id}", ("PATCH",)),
    ("/threads", ("GET",)),
    ("/threads/import", ("POST",)),
    ("/threads/metadata/migrate", ("POST",)),
    ("/threads/{thread_id}", ("DELETE",)),
    ("/threads/{thread_id}/attachments/validate", ("POST",)),
    ("/threads/{thread_id}/branch", ("POST",)),
    ("/threads/{thread_id}/callable-tools", ("GET",)),
    ("/threads/{thread_id}/chatapp/bind-code", ("POST",)),
    ("/threads/{thread_id}/chatapp/bindings", ("GET",)),
    ("/threads/{thread_id}/chatapp/bindings/{binding_id}", ("DELETE",)),
    ("/threads/{thread_id}/claim", ("POST",)),
    ("/threads/{thread_id}/clear", ("POST",)),
    ("/threads/{thread_id}/compact", ("POST",)),
    ("/threads/{thread_id}/config", ("DELETE",)),
    ("/threads/{thread_id}/config", ("GET",)),
    ("/threads/{thread_id}/config", ("PATCH",)),
    ("/threads/{thread_id}/context", ("GET",)),
    ("/threads/{thread_id}/export", ("GET",)),
    ("/threads/{thread_id}/history", ("GET",)),
    ("/threads/{thread_id}/metadata", ("GET",)),
    ("/threads/{thread_id}/metadata", ("PATCH",)),
    ("/threads/{thread_id}/skills", ("GET",)),
    ("/threads/{thread_id}/status", ("GET",)),
    ("/threads/{thread_id}/stop", ("POST",)),
    ("/todos", ("GET",)),
    ("/todos", ("POST",)),
    ("/todos/thread-counts", ("GET",)),
    ("/todos/users", ("GET",)),
    ("/todos/{todo_id}", ("DELETE",)),
    ("/todos/{todo_id}", ("PATCH",)),
    ("/todos/{todo_id}/complete", ("POST",)),
    ("/tools", ("GET",)),
    ("/tools/categories", ("GET",)),
    ("/tools/custom", ("GET",)),
    ("/tools/custom", ("POST",)),
    ("/tools/custom/export", ("GET",)),
    ("/tools/custom/import", ("POST",)),
    ("/tools/custom/{tool_id}", ("DELETE",)),
    ("/tools/custom/{tool_id}", ("GET",)),
    ("/tools/custom/{tool_id}", ("PUT",)),
    ("/tools/custom/{tool_id}/test", ("POST",)),
    ("/tools/defaults", ("DELETE",)),
    ("/tools/defaults", ("GET",)),
    ("/tools/defaults", ("PUT",)),
    ("/tools/optional", ("GET",)),
    ("/tools/unified", ("POST",)),
    ("/tools/unified/{tool_id}", ("DELETE",)),
    ("/tools/unified/{tool_id}", ("PUT",)),
    ("/triggers", ("GET",)),
    ("/triggers", ("POST",)),
    ("/triggers/executions/recent", ("GET",)),
    ("/triggers/fire/{trigger_id}", ("POST",)),
    ("/triggers/sources/list", ("GET",)),
    ("/triggers/sources/reload", ("POST",)),
    ("/triggers/{trigger_id}", ("DELETE",)),
    ("/triggers/{trigger_id}", ("GET",)),
    ("/triggers/{trigger_id}", ("PATCH",)),
    ("/triggers/{trigger_id}/executions", ("GET",)),
    ("/triggers/{trigger_id}/test", ("POST",)),
    ("/users/{user_id}/memories", ("GET",)),
    ("/users/{user_id}/memories", ("POST",)),
    ("/users/{user_id}/memories/search", ("GET",)),
    ("/users/{user_id}/memories/{key}", ("DELETE",)),
    ("/users/{user_id}/rag/index", ("DELETE",)),
    ("/users/{user_id}/rag/reindex", ("POST",)),
    ("/users/{user_id}/rag/search", ("GET",)),
    ("/users/{user_id}/rag/settings", ("GET",)),
    ("/users/{user_id}/rag/settings", ("PUT",)),
    ("/users/{user_id}/rag/stats", ("GET",)),
    ("/users/{user_id}/tools", ("GET",)),
    ("/users/{user_id}/tools/preferences", ("GET",)),
    ("/users/{user_id}/tools/reset", ("POST",)),
    ("/users/{user_id}/tools/search", ("GET",)),
    ("/users/{user_id}/tools/unified", ("GET",)),
    ("/users/{user_id}/tools/unified/{tool_id}/config", ("PUT",)),
    ("/users/{user_id}/tools/unified/{tool_id}/description", ("PUT",)),
    ("/users/{user_id}/tools/unified/{tool_id}/enable", ("PUT",)),
    ("/users/{user_id}/tools/{tool_name}/config", ("PUT",)),
    ("/voice/chat", ("POST",)),
    ("/voice/stt", ("POST",)),
    ("/voice/tts", ("POST",)),
    ("/workspace/download", ("GET",)),
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


def _auth(token: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


def test_api_schema_route_inventory_is_stable(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)

    assert _schema_routes(client.app) == EXPECTED_ROUTES
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


def test_api_responses_include_baseline_security_headers(
    tmp_path: Path,
    api_client_builder,
):
    client, _agent = _client(tmp_path, api_client_builder)

    response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


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
        headers=_auth(token),
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


def test_workspace_download_requires_admin_and_workspace_path(
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
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(workspace_dir))

    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    user_token = agent.accounts_repo.issue_token("alice")
    admin_token = agent.accounts_repo.issue_token("admin")

    user_response = client.get(
        "/workspace/download",
        headers=_auth(user_token),
        params={"path": str(artifact)},
    )
    outside_response = client.get(
        "/workspace/download",
        headers=_auth(admin_token),
        params={"path": str(outside)},
    )
    admin_response = client.get(
        "/workspace/download",
        headers=_auth(admin_token),
        params={"path": str(artifact)},
    )

    assert user_response.status_code == 403
    assert outside_response.status_code == 403
    assert admin_response.status_code == 200
    assert admin_response.text == "workspace data"


def test_user_auth_contract_for_me_endpoint(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    token = agent.accounts_repo.issue_token("alice")

    missing = client.get("/me")
    invalid = client.get("/me", headers=_auth("nym_invalid"))
    valid = client.get("/me", headers=_auth(token))

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
    limited = client.get("/me", headers=_auth("nym_invalid_3"))
    valid = client.get("/me", headers=_auth(token))

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

    forbidden = client.get("/admin/users", headers=_auth(user_token))
    allowed = client.get("/admin/users", headers=_auth(admin_token))

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert {row["id"] for row in allowed.json()} == {"alice", "admin"}


def test_deprecated_nymeria_api_key_is_hidden_from_config_api(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    admin_token = agent.accounts_repo.issue_token("admin")

    listed = client.get("/settings/env", headers=_auth(admin_token))

    assert listed.status_code == 200
    entries = listed.json()["entries"]
    assert "nymeria_api_key" not in {entry["name"] for entry in entries}
    assert "NYMERIA_API_KEY" not in {entry["env_var"] for entry in entries}

    lower = client.get("/settings/env/nymeria_api_key", headers=_auth(admin_token))
    upper = client.get("/settings/env/NYMERIA_API_KEY", headers=_auth(admin_token))

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
            headers=_auth(service_token),
            params={"provider": "telegram"},
        )
        assert response.status_code == 200

    limited = client.get(
        "/admin/chatapp/bindings",
        headers=_auth(service_token),
        params={"provider": "telegram"},
    )
    same_admin_other_endpoint = client.get(
        "/admin/chatapp/bindings/lookup",
        headers=_auth(service_token),
        params={"provider": "telegram", "platform_chat_id": "123"},
    )
    other_admin_same_endpoint = client.get(
        "/admin/chatapp/bindings",
        headers=_auth(other_token),
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

    response = client.get("/me", headers=_auth(service_token, **{"X-Nymeria-Act-As": "alice"}))

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

    response = client.get("/me", headers=_auth(token, **{"X-Nymeria-Act-As": "bob"}))

    assert response.status_code == 403
    assert response.json()["detail"] == "Act-As requires admin"
