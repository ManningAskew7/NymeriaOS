"""Per-thread OAuth account binding and fail-closed account selection.

One Nymeria user can hold several OAuth accounts for one provider (two
Outlook mailboxes, say). Before this pass every consumer saw all of them and
the pickers fell back to "the first one", so a thread could send from the
wrong mailbox by omitting one argument. Now:

- an exact ``thread:<id>`` entry in a vault row's ``allowed_targets`` (what
  ``auth_bindings`` writes) BINDS that account to the thread: the resolver's
  view for that thread holds only its bound accounts;
- with several visible accounts and nothing selecting between them (no
  explicit ``account_id``, no configured default, no binding) the pickers
  refuse and list the accounts, instead of guessing;
- tools learn their calling thread from LangChain's runnable context, so the
  binding applies through the real tool boundary with no per-tool plumbing.

The rules are documented in ``docs/agent-systems/credentials.md``
("Which account a call uses"); the behaviours here were written before the
implementation, and each test names the one it pins.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo

USER = "alice"
THREAD = "thread-beta"
OTHER_THREAD = "thread-unbound"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Isolated vault + settings sandbox (mirrors test_auth_cache_utils_vault_first)."""
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.delenv("OUTLOOK_DEFAULT_ACCOUNT_ID", raising=False)

    db_path = tmp_path / "accounts.db"
    AccountsRepo(db_path).create_user(USER, "alice@example.com", "Alice")

    from nymeria.core import credential_vault as vault_module

    monkeypatch.setattr(vault_module, "_repo_instance", None, raising=False)
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_module, "get_credential_vault_repo", lambda db_path=None: repo)

    yield repo, tmp_path

    settings_module.get_settings.cache_clear()


def _mint(repo, *, provider, account_id, email, access_token, targets=("native_tool:*",)):
    expires = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(
        timespec="seconds"
    )
    return repo.create_credential(
        owner_type="user",
        owner_user_id=USER,
        name=email,
        provider=provider,
        kind="oauth_token",
        account_label=email,
        metadata={
            "provider_id": provider,
            "account_id": account_id,
            "email": email,
            "name": "Vault User",
            "scopes": ["scope-a"],
            "expires_at": expires,
            "client_id": "vault-client-id",
            "source": "oauth_callback",
        },
        scopes=["scope-a"],
        allowed_targets=list(targets),
        expires_at=expires,
        secret_fields={"access_token": access_token, "refresh_token": f"refresh-{account_id}"},
        created_by_user_id=USER,
    )


def _bind(repo, credential_id: str, thread_id: str) -> None:
    repo.add_allowed_target(credential_id, target=f"thread:{thread_id}", actor_user_id=USER)


def _two_outlook_accounts(repo):
    a = _mint(repo, provider="outlook", account_id="sales", email="sales@_prv_a.example",
              access_token="tokA")
    b = _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
              access_token="tokB")
    return a, b


# ---------------------------------------------------------------------------
# Resolver view (behaviours 1, 8, 9)
# ---------------------------------------------------------------------------


def test_bound_thread_sees_only_its_accounts(vault):
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    view = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert set(view.accounts) == {"beta"}
    assert view.thread_bound is True
    # The persist payload is never narrowed, only the selectable view.
    assert set(view.cache["accounts"]) == {"sales", "beta"}


def test_unbound_thread_and_no_thread_see_every_account(vault):
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    for thread_id in (OTHER_THREAD, None):
        view = resolve_oauth_cache(
            USER, "outlook", cache_filename="microsoft.json", thread_id=thread_id
        )
        assert set(view.accounts) == {"sales", "beta"}
        assert view.thread_bound is False


def test_a_row_locked_to_one_thread_is_readable_there_and_invisible_elsewhere(vault):
    # An operator can leave ONLY the thread target on a row. That thread reads
    # the row under its binding; every other thread is denied at the vault and
    # never sees the account, and the bound thread never falls open to the
    # rest of the user's accounts.
    repo, _ = vault
    _mint(repo, provider="outlook", account_id="sales", email="sales@_prv_a.example",
          access_token="tokA")
    _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
          access_token="tokB", targets=(f"thread:{THREAD}",))

    from nymeria.tools import outlook_email as oe
    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    bound = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert set(bound.accounts) == {"beta"} and bound.thread_bound is True
    assert oe.get_access_token(USER, thread_id=THREAD) == "tokB"

    elsewhere = resolve_oauth_cache(
        USER, "outlook", cache_filename="microsoft.json", thread_id=OTHER_THREAD
    )
    assert set(elsewhere.accounts) == {"sales"}
    assert oe.get_access_token(USER, thread_id=OTHER_THREAD) == "tokA"


def test_a_bound_row_that_cannot_be_read_fails_closed(vault, monkeypatch):
    # The binding exists but the secret is gone: handing the thread the other
    # accounts would be the wrong-mailbox path the binding closes. Refuse.
    repo, _ = vault
    _mint(repo, provider="outlook", account_id="sales", email="sales@_prv_a.example",
          access_token="tokA")
    b = _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
              access_token="tokB")
    _bind(repo, b.id, THREAD)

    from nymeria.core.credential_vault import CredentialSecretUnavailable
    from nymeria.tools import outlook_email as oe
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    real_get = repo.get_secret_field

    def broken_for_beta(credential_id, field_name, **kw):
        if credential_id == b.id:
            raise CredentialSecretUnavailable(credential_id)
        return real_get(credential_id, field_name, **kw)

    monkeypatch.setattr(repo, "get_secret_field", broken_for_beta)

    with pytest.raises(OAuthAccountSelectionError, match="cannot be read"):
        oe.get_access_token(USER, thread_id=THREAD)
    assert oe.get_access_token(USER, thread_id=OTHER_THREAD) == "tokA"


def test_a_legacy_account_sharing_an_id_with_a_bound_vault_row_yields_the_vault_row(vault):
    repo, _ = vault
    from nymeria.tools.auth_cache_utils import resolve_oauth_cache, save_token_cache

    save_token_cache(
        USER,
        "microsoft.json",
        {"accounts": {"beta": {"email": "old@example.com", "access_token": "tokOLD",
                               "refresh_token": "r", "expires_at": time.time() + 3600}}},
    )
    b = _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
              access_token="tokB")
    _bind(repo, b.id, THREAD)

    view = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert view.accounts["beta"]["access_token"] == "tokB"


def test_wildcard_thread_target_is_authorization_not_a_binding(vault):
    repo, _ = vault
    _mint(repo, provider="outlook", account_id="sales", email="sales@_prv_a.example",
          access_token="tokA", targets=("native_tool:*", "thread:*"))
    _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
          access_token="tokB", targets=("*",))

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    view = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert view.thread_bound is False
    assert set(view.cache["accounts"]) == {"sales", "beta"}


def test_legacy_accounts_drop_out_of_a_bound_view_but_stay_in_the_unbound_view(vault):
    repo, _ = vault
    from nymeria.tools.auth_cache_utils import resolve_oauth_cache, save_token_cache

    save_token_cache(
        USER,
        "microsoft.json",
        {"accounts": {"legacy": {"email": "legacy@example.com", "access_token": "tokL",
                                 "refresh_token": "rL", "expires_at": time.time() + 3600}}},
    )
    b = _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
              access_token="tokB")
    _bind(repo, b.id, THREAD)

    bound = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert set(bound.accounts) == {"beta"}

    unbound = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json")
    assert set(unbound.accounts) == {"legacy", "beta"}


def test_refresh_through_a_bound_view_keeps_legacy_accounts_on_disk(vault):
    # Behaviour 9. The legacy half of the store is rewritten wholesale on
    # persist, from ``cache``; a bound view narrows only ``accounts``, so a
    # token refresh performed through the view must leave the legacy account
    # (which the view never showed) intact on disk.
    repo, _ = vault
    from nymeria.tools.auth_cache_utils import (
        load_token_cache,
        resolve_oauth_cache,
        save_token_cache,
    )

    save_token_cache(
        USER,
        "microsoft.json",
        {"pending_auth": {"flow": "device"},
         "accounts": {"legacy": {"email": "legacy@example.com", "access_token": "tokL",
                                 "refresh_token": "rL", "expires_at": 1.0}}},
    )
    b = _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
              access_token="tokB")
    _bind(repo, b.id, THREAD)

    view = resolve_oauth_cache(USER, "outlook", cache_filename="microsoft.json", thread_id=THREAD)
    assert "legacy" not in view.accounts
    view.accounts["beta"]["access_token"] = "tokB-refreshed"
    view.persist(view.cache)

    on_disk = load_token_cache(USER, "microsoft.json")
    assert on_disk["accounts"]["legacy"]["access_token"] == "tokL"
    assert on_disk["pending_auth"] == {"flow": "device"}
    assert "beta" not in on_disk["accounts"]  # vault accounts never land in the file
    refreshed = repo.get_secret_field(
        b.id, "access_token", actor=USER, target_type="native_tool", target_id="outlook"
    )
    assert refreshed == "tokB-refreshed"


# ---------------------------------------------------------------------------
# Shared picker (behaviours 2 to 7)
# ---------------------------------------------------------------------------

_A = {"email": "sales@_prv_a.example", "access_token": "tokA", "_vault_credential_id": "cred_a"}
_B = {"email": "beta@fa.example", "access_token": "tokB", "_vault_credential_id": "cred_b"}


def _pick(accounts, account_id=None, **kw):
    from nymeria.tools.auth_cache_utils import select_oauth_account

    return select_oauth_account(accounts, account_id, provider_label="Outlook", **kw)


def test_picker_explicit_outside_the_view_is_refused_and_names_the_scope():
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError) as bound:
        _pick({"beta": _B}, "sales", thread_id=THREAD, thread_bound=True)
    assert "'sales' is not available to this thread" in str(bound.value)
    assert "beta" in str(bound.value)

    with pytest.raises(OAuthAccountSelectionError) as unbound:
        _pick({"beta": _B, "sales": _A}, "nope")
    assert "'nope' is not available to this user" in str(unbound.value)


def test_picker_ambiguous_without_selection_lists_accounts_and_both_fixes():
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError) as exc:
        _pick({"sales": _A, "beta": _B}, thread_id=THREAD)
    text = str(exc.value)
    for needle in ("sales", "sales@_prv_a.example", "cred_a", "beta", "beta@fa.example",
                   "cred_b", "account_id", "auth_bindings", f'target_id="{THREAD}"'):
        assert needle in text, needle


def test_picker_ambiguous_without_a_thread_still_explains_binding():
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError) as exc:
        _pick({"sales": _A, "beta": _B})
    assert "target_id=<this thread id>" in str(exc.value)


def test_picker_default_wins_when_visible_and_is_ignored_when_the_binding_hid_it():
    assert _pick({"sales": _A, "beta": _B}, default_id="sales") == ("sales", _A)
    # Behaviour 6: the binding narrowed the view to beta; a global default of
    # sales cannot reach past it.
    assert _pick({"beta": _B}, default_id="sales", thread_id=THREAD, thread_bound=True) == (
        "beta", _B,
    )


def test_picker_single_account_needs_no_selection():
    assert _pick({"beta": _B}) == ("beta", _B)


def test_picker_thread_bound_to_several_still_requires_account_id():
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError) as exc:
        _pick({"sales": _A, "beta": _B}, thread_id=THREAD, thread_bound=True)
    assert "bound to several Outlook accounts" in str(exc.value)
    assert "Pass account_id explicitly" in str(exc.value)


def test_picker_empty_view_raises():
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError):
        _pick({})


# ---------------------------------------------------------------------------
# Outlook token path against the real vault (behaviours 1 to 5 end to end)
# ---------------------------------------------------------------------------


def test_outlook_token_follows_the_thread_binding(vault):
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)

    from nymeria.tools import outlook_email as oe
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    assert oe.get_access_token(USER, thread_id=THREAD) == "tokB"
    assert oe.get_account(USER, thread_id=THREAD)["email"] == "beta@fa.example"
    with pytest.raises(OAuthAccountSelectionError):
        oe.get_access_token(USER, "sales", thread_id=THREAD)
    with pytest.raises(OAuthAccountSelectionError):
        oe.get_access_token(USER, thread_id=OTHER_THREAD)
    with pytest.raises(OAuthAccountSelectionError):
        oe.get_access_token(USER)
    assert oe.get_access_token(USER, "sales") == "tokA"


def test_outlook_single_account_is_used_from_any_thread(vault):
    repo, _ = vault
    _mint(repo, provider="outlook", account_id="beta", email="beta@fa.example",
          access_token="tokB")

    from nymeria.tools import outlook_email as oe

    assert oe.get_access_token(USER) == "tokB"
    assert oe.get_access_token(USER, thread_id=OTHER_THREAD) == "tokB"


def test_outlook_global_default_still_selects_among_unbound_accounts(vault, monkeypatch):
    repo, _ = vault
    _two_outlook_accounts(repo)
    monkeypatch.setenv("OUTLOOK_DEFAULT_ACCOUNT_ID", "sales")
    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()

    from nymeria.tools import outlook_email as oe

    assert oe.get_access_token(USER) == "tokA"


def test_outlook_binding_beats_the_global_default_end_to_end(vault, monkeypatch):
    # Behaviour 6 through the real resolver: the binding narrows the view to
    # beta before the configured default (sales) is consulted.
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)
    monkeypatch.setenv("OUTLOOK_DEFAULT_ACCOUNT_ID", "sales")
    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()

    from nymeria.tools import outlook_email as oe

    assert oe.get_access_token(USER, thread_id=THREAD) == "tokB"
    assert oe.get_access_token(USER, thread_id=OTHER_THREAD) == "tokA"


def test_outlook_thread_bound_to_two_accounts_still_needs_account_id_end_to_end(vault):
    # Behaviour 7 through the real resolver: a binding narrows, it does not pick.
    repo, _ = vault
    a, b = _two_outlook_accounts(repo)
    _bind(repo, a.id, THREAD)
    _bind(repo, b.id, THREAD)

    from nymeria.tools import outlook_email as oe
    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError

    with pytest.raises(OAuthAccountSelectionError, match="bound to several"):
        oe.get_access_token(USER, thread_id=THREAD)
    assert oe.get_access_token(USER, "sales", thread_id=THREAD) == "tokA"


# ---------------------------------------------------------------------------
# Ambient thread identity (behaviours 11 and 15)
# ---------------------------------------------------------------------------


def test_ambient_thread_id_reads_the_tool_call_config():
    from langchain_core.tools import tool

    from nymeria.tools.utils import ambient_thread_id

    assert ambient_thread_id() is None

    @tool
    def probe(x: int) -> str:
        """Report the ambient thread id."""
        return str(ambient_thread_id())

    assert probe.invoke({"x": 1}, config={"configurable": {"thread_id": "t-ambient"}}) == "t-ambient"
    assert probe.invoke({"x": 1}) == "None"


def test_ambient_thread_id_answers_with_the_parent_for_a_dream_shadow_turn(monkeypatch):
    # Dreams run in a generated shadow thread acting for a parent; the parent
    # is where the mailbox binding lives, so that is the thread the account
    # picker must see. Routed through the existing get_effective_thread_id.
    from langchain_core.tools import tool

    from nymeria.core import agent as agent_module
    from nymeria.tools.utils import ambient_thread_id

    class _Configs:
        def get_config(self, thread_id):
            return SimpleNamespace(shadow_parent_id="parent-thread" if thread_id == "shadow-1" else None)

    fake_agent = SimpleNamespace(thread_config_manager=_Configs())
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    @tool
    def probe(x: int) -> str:
        """Report the ambient thread id."""
        return str(ambient_thread_id())

    assert probe.invoke({"x": 1}, config={"configurable": {"thread_id": "shadow-1"}}) == "parent-thread"
    assert probe.invoke({"x": 1}, config={"configurable": {"thread_id": "plain"}}) == "plain"


class _GraphStub:
    """Stand-in for the policy HTTP client factory used by graph_request."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status_code=200, text='{"value": []}', json=lambda: {"value": []})


def test_outlook_tool_resolves_the_calling_threads_binding_without_plumbing(vault, monkeypatch):
    # Behaviour 11: the thread id enters ONLY through the tool call's config;
    # nothing in outlook_list_emails passes it down, yet the bound mailbox is
    # the one whose token reaches Graph.
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)

    from nymeria.tools import outlook_email as oe

    graph = _GraphStub()
    monkeypatch.setattr(oe, "_http_client", graph)

    bound_result = oe.outlook_list_emails.invoke(
        {"limit": 1}, config={"configurable": {"user_id": USER, "thread_id": THREAD}}
    )
    assert "Error" not in bound_result
    assert graph.calls[-1]["headers"]["Authorization"] == "Bearer tokB"

    unbound_result = oe.outlook_list_emails.invoke(
        {"limit": 1}, config={"configurable": {"user_id": USER, "thread_id": OTHER_THREAD}}
    )
    assert "Several Outlook accounts are connected" in unbound_result
    assert f'target_id="{OTHER_THREAD}"' in unbound_result
    assert len(graph.calls) == 1  # the refusal never reached Graph


# ---------------------------------------------------------------------------
# Google picker (behaviour 10)
# ---------------------------------------------------------------------------


def test_google_credentials_follow_the_thread_binding(vault):
    pytest.importorskip("google.oauth2.credentials")
    repo, _ = vault
    _mint(repo, provider="google_calendar", account_id="work", email="work@example.com",
          access_token="gtokA")
    b = _mint(repo, provider="google_calendar", account_id="bot", email="bot@example.com",
              access_token="gtokB")
    _bind(repo, b.id, THREAD)

    from nymeria.tools.auth_cache_utils import (
        OAuthAccountSelectionError,
        get_google_credentials,
    )

    bound = get_google_credentials(USER, "google_calendar", ["scope-a"], thread_id=THREAD)
    assert bound.token == "gtokB"
    with pytest.raises(OAuthAccountSelectionError):
        get_google_credentials(USER, "google_calendar", ["scope-a"])
    with pytest.raises(OAuthAccountSelectionError):
        get_google_credentials(USER, "google_calendar", ["scope-a"], account_id="work",
                               thread_id=THREAD)
    explicit = get_google_credentials(USER, "google_calendar", ["scope-a"], account_id="work")
    assert explicit.token == "gtokA"


def test_google_api_request_returns_the_selection_error_as_its_result(vault):
    pytest.importorskip("googleapiclient.discovery")
    repo, _ = vault
    _mint(repo, provider="google_docs", account_id="one", email="one@example.com",
          access_token="g1")
    _mint(repo, provider="google_docs", account_id="two", email="two@example.com",
          access_token="g2")

    from nymeria.tools.auth_cache_utils import google_api_request

    ok, message = google_api_request(
        USER, "google_docs", ["scope-a"], lambda service: service,
        service_name="docs", service_version="v1", api_label="Google Docs",
    )
    assert ok is False
    assert "Several Google Docs accounts are connected" in message


# ---------------------------------------------------------------------------
# Trigger source (behaviour 12)
# ---------------------------------------------------------------------------


def test_outlook_trigger_source_cannot_poll_a_mailbox_outside_its_threads_binding(
    vault, monkeypatch
):
    repo, _ = vault
    _, b = _two_outlook_accounts(repo)
    _bind(repo, b.id, THREAD)

    import httpx

    from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError
    from nymeria.triggers.sources.outlook_email_source import OutlookEmailSource

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(headers)
        return SimpleNamespace(status_code=200, text="", json=lambda: {"value": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    source = OutlookEmailSource()
    # A first poll only records a baseline and returns; give the source a
    # recent baseline so the bound-account poll really reaches Graph.
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with pytest.raises(OAuthAccountSelectionError):
        source.check({"account_id": "sales"}, {"last_check_time": recent}, USER, thread_id=THREAD)
    assert calls == []  # refused before any Graph traffic

    events = source.check(
        {"account_id": "beta"}, {"last_check_time": recent, "seen_ids": []}, USER,
        thread_id=THREAD,
    )
    assert events == []
    assert calls[-1]["Authorization"] == "Bearer tokB"
