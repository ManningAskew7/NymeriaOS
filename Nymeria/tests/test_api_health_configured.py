"""``GET /health`` reports whether the deployment is already set up (#323).

A client pointed at an existing deployment (the Tauri desktop app, a phone)
has nothing pre-auth to tell it the server is configured, so it opens on the
install-shaped setup hub. ``configured`` is the coarse signal: the deployment
is CLAIMED (its bootstrap token file is gone, or more than one account
exists) and the LLM provider is set. Coarse on purpose: the endpoint is
unauthenticated, so it never says which provider or account. Not covered:
the real ``Settings.get_api_key_for_provider`` chain (patched here).
"""

from __future__ import annotations

from pathlib import Path

from nymeria.core.accounts import AccountsRepo


class _Agent:
    def __init__(self, data_dir: Path):
        from nymeria.core.chat_bindings import ChatBindingsRepo

        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)

    def sync_agent_tools(self):
        return None


def _client(tmp_path, api_client_builder, *, provider="anthropic", key="sk-test", claimed=True):
    settings = api_client_builder.settings(tmp_path)
    settings.llm_provider = provider
    settings.get_api_key_for_provider = lambda: key
    agent = _Agent(tmp_path)
    # The bootstrap admin exists on every deployment before /health can
    # answer, so the owner row alone never means "claimed".
    agent.accounts_repo.ensure_bootstrap_admin(tmp_path)
    assert agent.accounts_repo.bootstrap_token_path.exists()
    if claimed:
        agent.accounts_repo.bootstrap_token_path.unlink()
    return api_client_builder.client(agent, settings), agent


def _configured(client) -> bool:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    return body["configured"]


def test_a_fresh_deployment_with_its_bootstrap_token_unused_is_not_configured(
    tmp_path, api_client_builder
):
    # The realistic first boot: provider and key in the env, the default
    # admin minted, nobody has signed in. The install hub is right here.
    client, _ = _client(tmp_path, api_client_builder, claimed=False)
    assert _configured(client) is False


def test_a_claimed_deployment_with_a_keyed_provider_is_configured(tmp_path, api_client_builder):
    client, _ = _client(tmp_path, api_client_builder)
    assert _configured(client) is True


def test_a_second_account_counts_as_claimed_even_with_the_token_file_present(
    tmp_path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder, claimed=False)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner", role="admin")
    assert _configured(client) is True


def test_a_claimed_deployment_without_the_providers_key_is_not_configured(
    tmp_path, api_client_builder
):
    client, _ = _client(tmp_path, api_client_builder, key=None)
    assert _configured(client) is False


def test_a_keyless_local_provider_counts_as_set(tmp_path, api_client_builder):
    client, _ = _client(tmp_path, api_client_builder, provider="ollama", key=None)
    assert _configured(client) is True


def test_no_provider_name_is_not_configured(tmp_path, api_client_builder):
    client, _ = _client(tmp_path, api_client_builder, provider="", key="sk-test")
    assert _configured(client) is False


def test_an_accounts_store_failure_reads_not_configured_and_health_stays_ok(
    tmp_path, api_client_builder, monkeypatch
):
    client, agent = _client(tmp_path, api_client_builder, claimed=False)

    def boom():
        raise RuntimeError("accounts.db locked")

    monkeypatch.setattr(agent.accounts_repo, "count_users", boom)
    assert _configured(client) is False


def test_an_unresolvable_agent_reads_not_configured_and_health_stays_ok(
    tmp_path, api_client_builder
):
    from nymeria.api.routers.system import deployment_configured

    def no_agent():
        raise RuntimeError("agent not initialised")

    assert deployment_configured(no_agent, lambda: None) is False


def test_health_never_names_the_provider_or_account(tmp_path, api_client_builder):
    client, _ = _client(tmp_path, api_client_builder)
    body = client.get("/health").json()
    # A change-detector by design (a leakage control, testing-standards
    # "build-failing gates"): a new /health field is a deliberate decision
    # about what an unauthenticated caller may learn, so widen this on
    # purpose, never in passing.
    assert set(body) == {"status", "version", "configured"}
