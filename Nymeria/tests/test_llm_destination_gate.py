"""A per-thread base_url must not carry the OPERATOR's credential (E10-01).

``PATCH /threads/{id}/config`` is authenticated but deliberately NOT admin
gated, so any user may set ``llm_config.base_url`` on a thread they own. The
resolver then attached the server's provider key to whatever address that named
and sent it, on every turn of that thread, along with the global system prompt.

The gate cannot live on the route: ``thread_configs/<id>.json`` is reachable by
``file_write``, a seed tool (P4-03), so a validated PATCH schema is bypassed by
one file write. It lives where the value is CONSUMED.

The rule is not an allowlist. Naming the host configuration already points at is
not a redirection, which is what makes the flagship per-thread CLIProxy flow
(the desktop client AUTO-FILLS that URL and relies on the server's key) keep
working untouched. Naming anywhere else redirects the operator's credential, and
is refused ONLY when the operator's credential would actually ride to it: a
caller spending their OWN key at their own address is the existing
bring-your-own-endpoint capability and is left alone.

Every test here is differential about EGRESS, not about a return value. A green
"no key leaked" proves nothing if the fixture resolved no key in the first
place, so the config-provenance leg asserting the key DOES ride is load-bearing.
The precedent is ``anthropic_probe_base_url``, whose test asserts zero egress
after widening it mailed a live third-party key to Anthropic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nymeria.core.agent import NymeriaAgent
from nymeria.core.llm_credentials import LLMProviderCredential
from nymeria.core.llm_provider_utils import (
    configured_llm_destinations,
    destination_redirects_away_from_config,
)
from nymeria.core.thread_config import ThreadConfig, ThreadLLMConfig


_CLIPROXY = "http://cli-proxy-api-latest:8317/v1"
_ATTACKER = "https://attacker.example"
_OPERATOR_KEY = "operator-key-do-not-leak"


class _Settings:
    llm_provider = "openai"
    llm_model = "gpt-5.5"
    llm_fallback_models = ""
    llm_temperature = 1.0
    llm_max_tokens = None
    llm_top_p = None
    llm_top_k = None
    llm_frequency_penalty = None
    llm_presence_penalty = None
    llm_reasoning_effort = "medium"
    llm_extended_thinking = False
    llm_use_model_defaults = False
    llm_base_url = _CLIPROXY
    llm_background_base_url = None
    llm_context_length = None
    llm_ollama_num_ctx = None
    llm_provider_route = None
    openai_api_mode = "responses"
    llm_stream_max_retries = 2
    llm_stream_retry_initial_delay = 1.0
    llm_stream_retry_max_delay = 8.0
    llm_fallback_hold_seconds = 7200
    anthropic_api_key = _OPERATOR_KEY
    anthropic_direct_api_key = _OPERATOR_KEY
    openai_api_key = _OPERATOR_KEY
    openrouter_api_key = _OPERATOR_KEY

    def get_api_key_for_provider(self) -> str:
        return _OPERATOR_KEY


def _redirects(candidate, *, provider, settings):
    """The gate as an LLM caller asks it.

    The predicate itself takes a set of configured URLs and knows nothing about
    providers, so that the embedding and voice consumption points in the same
    audit cluster can pass their own. This wrapper is the LLM pairing, kept
    here so each test reads as one question rather than two.
    """
    return destination_redirects_away_from_config(
        candidate,
        configured=configured_llm_destinations(provider, settings=settings),
    )


def _make_settings(**overrides):
    settings = _Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class _FakeVault:
    """Enough vault to exercise ``${credential:...}`` end to end.

    The interpolation path is where the gate was bypassed, so these tests need
    a vault that really resolves references and really reports ownership; a
    sentinel object would make every reference a no-op and every test here
    green for the wrong reason.
    """

    def __init__(self, records):
        # id -> (owner_type, owner_user_id, {field: plaintext})
        self._records = records

    def get_credential(self, credential_id):
        record = self._records.get(credential_id)
        if record is None:
            return None
        owner_type, owner_user_id, _fields = record
        return SimpleNamespace(
            id=credential_id, owner_type=owner_type, owner_user_id=owner_user_id
        )

    def resolve_references(self, value, *, actor, target_type=None, target_id=None):
        from nymeria.core.credential_vault import (
            CREDENTIAL_REF_PATTERN,
            CredentialNotFound,
        )

        def _replace(match):
            credential_id, field = match.group(1), match.group(2)
            record = self._records.get(credential_id)
            if record is None:
                raise CredentialNotFound(credential_id)
            return record[2].get(field, "")

        return CREDENTIAL_REF_PATTERN.sub(_replace, value)


def _make_agent(
    *,
    thread_base_url: str | None = None,
    thread_api_key: str | None = None,
    thread_provider: str | None = None,
    role: str = "user",
    vault_credential: LLMProviderCredential | None = None,
    vault: object | None = None,
    **settings_overrides,
):
    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        agent = NymeriaAgent()

    settings = _Settings()
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    agent.settings = settings

    manager = MagicMock()
    if thread_base_url is None and thread_api_key is None and thread_provider is None:
        manager.get_config.return_value = None
    else:
        manager.get_config.return_value = ThreadConfig(
            thread_id="thread-1",
            llm_config=ThreadLLMConfig(
                base_url=thread_base_url,
                api_key=thread_api_key,
                provider=thread_provider,
            ),
        )
    manager.save_config.return_value = True
    agent.thread_config_manager = manager

    agent.accounts_repo = SimpleNamespace(
        get_thread_owner=lambda thread_id: "alice",
        get_user_by_id=lambda user_id: SimpleNamespace(id=user_id, role=role),
    )
    agent.credential_vault = vault if vault is not None else (
        object() if vault_credential else None
    )
    agent.invalidate_thread_config_cache = lambda thread_id: None

    patcher = patch(
        "nymeria.core.agent_llm_config.get_llm_provider_credential",
        return_value=vault_credential,
    )
    patcher.start()
    return agent, patcher


def _resolve(**kwargs):
    agent, patcher = _make_agent(**kwargs)
    try:
        return agent._get_llm_config_for_thread("thread-1"), agent
    finally:
        patcher.stop()


# --- the predicate on its own --------------------------------------------


def test_the_configured_host_is_not_a_redirection():
    """The flagship case. Same host, different path and trailing slash."""
    settings = _Settings()

    assert not _redirects(
        "http://cli-proxy-api-latest:8317", provider="openai", settings=settings
    )
    assert not _redirects(
        _CLIPROXY + "/", provider="openai", settings=settings
    )


def test_the_proxy_fronts_every_provider_it_serves_not_just_the_global_one():
    """A CLIProxy container serves the Anthropic path at its root and the
    OpenAI-compatible path at ``/v1``, so a global base URL pointing at it
    names a destination for BOTH providers.

    Two things follow, and the bug was having only the first. The proxy's own
    address must stay spellable for the non-global provider (otherwise the
    "Anthropic (Subscription)" per-thread option is refused on the reference
    deployment), and the VENDOR's canonical host must become a redirection for
    it, because the anthropic env key on a proxied deployment is a proxy-local
    ``cpx-*`` secret. Admitting ``api.anthropic.com`` as "the vendor's own
    host" mails a third-party-issued secret to Anthropic Inc.

    The tell was the asymmetry: ``api.openai.com`` was already refused here.
    """
    settings = _Settings()  # llm_provider=openai, llm_base_url=the proxy

    assert not _redirects(
        "http://cli-proxy-api-latest:8317", provider="anthropic", settings=settings
    )
    assert _redirects("https://api.anthropic.com", provider="anthropic", settings=settings)
    assert _redirects("https://api.openai.com", provider="openai", settings=settings)


def test_a_non_proxy_global_base_url_leaves_another_vendors_host_alone():
    """The counterpart, and the reason the proxy check is not just
    ``llm_base_url is set``. A plain gateway URL fronts the provider it was
    configured for and nothing else, so a thread pointed at Anthropic direct is
    naming Anthropic's real address to spend a real Anthropic key."""
    settings = _make_settings(llm_base_url="https://gateway.internal/v1")

    assert not _redirects(
        "https://api.anthropic.com", provider="anthropic", settings=settings
    )


def test_loopback_spellings_are_one_destination():
    """``127.0.0.1``, ``::1`` and ``localhost`` are the same machine.

    Treating them as three refused threads aimed at the very server
    configuration named: a deployment configured as ``localhost:8318`` while
    the client offers ``127.0.0.1``, and Ollama registered at ``localhost``
    while its own docs say ``127.0.0.1``. Collapsing them is host equality
    being correct, not an exemption, which the last two legs pin.

    The collapse stops at the three spellings the real flows use. Taking all of
    ``127.0.0.0/8`` would be a strict widening for no flow that exists:
    anything able to bind a spare loopback address could stand up a listener on
    a port configuration named and be handed the operator's key. And another
    PORT on the same loopback stays a redirection either way, which is what
    keeps the field from becoming credentialed port scanning.
    """
    settings = _make_settings(llm_base_url="http://localhost:8318")

    assert not _redirects("http://127.0.0.1:8318", provider="openai", settings=settings)
    assert not _redirects("http://[::1]:8318", provider="openai", settings=settings)
    assert _redirects("http://127.0.0.2:8318", provider="openai", settings=settings)
    assert _redirects("http://127.255.255.254:8318", provider="openai", settings=settings)
    assert _redirects("http://127.0.0.1:8000", provider="openai", settings=settings)


def test_the_key_survives_being_keyed_again():
    """The predicate re-keys whatever it is handed, and the LLM caller hands it
    keys, so the key has to be IDEMPOTENT rather than merely well-formed.

    An IPv6 literal is where that bites: ``urlparse`` strips the brackets off
    ``.hostname``, and an unbracketed key reparses with a ``.port`` that
    raises, so the key becomes None, EVERY configured entry is dropped, and the
    gate then refuses the operator's own configured address.
    """
    from nymeria.core.llm_provider_utils import base_url_destination_key

    for value in (
        "http://[2001:db8::1]:8317/v1",
        "http://[2001:db8::1]/v1",
        "https://api.openai.com/v1",
        "http://cli-proxy-api-latest:8317/v1",
    ):
        key = base_url_destination_key(value)
        assert key is not None
        assert base_url_destination_key(key) == key

    settings = _make_settings(llm_base_url="http://[2001:db8::1]:8317/v1")
    assert not _redirects(
        "http://[2001:db8::1]:8317/v1", provider="openai", settings=settings
    )


def test_two_different_ipv6_hosts_are_two_destinations():
    """``[2001:db8::1]:8317`` and ``[2001:db8::1:8317]`` are a host with a port
    and a different host on the default port. An HTTP client dials them
    differently, so they must not collapse to one key."""
    from nymeria.core.llm_provider_utils import base_url_destination_key

    assert base_url_destination_key("http://[2001:db8::1]:8317") != base_url_destination_key(
        "http://[2001:db8::1:8317]"
    )


def test_a_bind_address_is_not_a_loopback_alias():
    """``0.0.0.0`` is a bind address, not a destination, and a name that merely
    resolves to the loopback is a DNS question this predicate must not ask."""
    settings = _make_settings(llm_base_url="http://localhost:8318")

    assert _redirects("http://0.0.0.0:8318", provider="openai", settings=settings)
    assert _redirects("http://local.attacker.example:8318", provider="openai", settings=settings)


def test_a_different_port_on_the_same_box_is_a_redirection():
    """Port is part of the identity: on the reference deployment the CLIProxy
    sidecar and the API itself differ only by port, so dropping it would let a
    thread aim at any service on the same host."""
    assert _redirects(
        "http://cli-proxy-api-latest:9999", provider="openai", settings=_Settings()
    )


def test_the_vendor_canonical_host_is_a_redirection_on_a_proxied_deployment():
    """The registry knowing an address is not the operator having chosen it.

    On a proxied deployment ``OPENAI_API_KEY`` holds a PROXY-LOCAL secret
    (``cpx-*``), so admitting ``api.openai.com`` because the registry knows it
    as OpenAI's address would mail a third-party-issued secret to OpenAI Inc.
    That is the ``anthropic_probe_base_url`` mistake, whose test asserts zero
    egress rather than zero result.
    """
    assert _redirects(
        "https://api.openai.com/v1", provider="openai", settings=_Settings()
    )


def test_the_vendor_canonical_host_is_allowed_when_nothing_else_is_configured():
    """The other half of the same rule, and the reason it is not an allowlist.

    With no base URL configured anywhere, the vendor's own address is by
    definition where this provider would have gone, and the key is the
    vendor's own. Differential against the test above: same URL, same
    provider, flipped purely by whether the operator named a destination.
    """
    assert not _redirects(
        "https://api.openai.com/v1",
        provider="openai",
        settings=_make_settings(llm_base_url=None),
    )


def test_a_plaintext_downgrade_to_a_configured_host_is_a_redirection():
    """Scheme is part of the destination identity.

    Without it, ``http://`` to a host configured as ``https://`` matches, and
    the operator's key goes onto the wire in clear text to the right address,
    which is a downgrade a caller should not be able to choose.
    """
    settings = _make_settings(llm_base_url="https://gateway.example/v1")

    assert not _redirects(
        "https://gateway.example", provider="openai", settings=settings
    )
    assert _redirects(
        "http://gateway.example", provider="openai", settings=settings
    )


def test_userinfo_cannot_disguise_the_real_host():
    """``http://configured.host@evil.example`` dials evil.example.

    The key must agree with what an HTTP client would actually do, or the
    gate can be walked past with a string that merely LOOKS configured.
    """
    assert _redirects(
        "http://cli-proxy-api-latest:8317@evil.example",
        provider="openai",
        settings=_Settings(),
    )


def test_naming_nothing_is_not_a_redirection():
    """``""`` means explicit direct API and must stay spellable."""
    for value in ("", None, "   "):
        assert not _redirects(
            value, provider="openai", settings=_Settings()
        )


def test_the_background_tier_host_counts_as_configured():
    """Differential: the same URL flips purely on whether config names it.

    The background tier is a second, easy-to-miss field on the same resolution
    path, so it gets its own leg rather than being assumed.
    """
    host = "http://background.example:9000"

    assert _redirects(
        host, provider="openai", settings=_Settings()
    )
    assert not _redirects(
        host,
        provider="openai",
        settings=_make_settings(llm_background_base_url=host),
    )


def test_a_malformed_url_does_not_open_the_gate():
    """Anything unparseable must read as a redirection, not as 'no host'."""
    assert _redirects(
        "http://[oops", provider="openai", settings=_Settings()
    )


def test_configured_destinations_never_returns_the_empty_set_for_a_known_provider():
    """If this ever came back empty, every destination would read as a
    redirection and every thread would silently fall back.

    Asserted with settings that name NOTHING, so the registry half of the
    function is what has to answer. With a configured ``llm_base_url`` the set
    is non-empty from that alone and the assertion proves nothing.
    """
    assert configured_llm_destinations(
        "openai", settings=_make_settings(llm_base_url=None)
    )


# --- egress: the operator credential --------------------------------------


def test_the_operator_key_rides_to_a_configured_destination():
    """The differential leg. Without this, a green 'no leak' below could just
    mean the fixture never resolved a key at all."""
    config, _ = _resolve(thread_base_url=_CLIPROXY)

    assert config.base_url == _CLIPROXY
    assert config.api_key == _OPERATOR_KEY


def test_the_operator_key_never_rides_to_a_caller_named_destination():
    """The finding, closed. The thread keeps working on the configured
    provider rather than failing, and the attacker host is never contacted."""
    config, _ = _resolve(thread_base_url=_ATTACKER)

    assert config.base_url == _CLIPROXY
    assert _ATTACKER not in str(config.base_url or "")
    assert config.api_key == _OPERATOR_KEY  # rides to CLIProxy, where it always did


def test_a_caller_who_brings_their_own_key_keeps_their_own_endpoint():
    """Bring-your-own-endpoint is a real capability, not collateral.

    No operator credential is involved, so there is nothing to protect and
    refusing would only remove a feature.
    """
    config, _ = _resolve(thread_base_url=_ATTACKER, thread_api_key="my-own-key")

    assert config.base_url == _ATTACKER
    assert config.api_key == "my-own-key"
    assert config.api_key != _OPERATOR_KEY


def test_a_user_owned_vault_credential_also_counts_as_bringing_your_own():
    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        vault_credential=LLMProviderCredential(
            api_key="alice-own-key",
            base_url=None,
            credential_id="cred_alice",
            owner_type="user",
        ),
    )

    assert config.base_url == _ATTACKER
    assert config.api_key == "alice-own-key"


def test_a_system_owned_vault_credential_does_not():
    """The leak path the audit did not name.

    ``_list_visible_records`` passes ``include_system=True``, so a non-admin's
    thread can resolve the OPERATOR's vault row. Spending that at a
    caller-named address is exactly the theft the finding describes, just
    sourced from the vault instead of the environment.
    """
    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        vault_credential=LLMProviderCredential(
            api_key=_OPERATOR_KEY,
            base_url=None,
            credential_id="cred_system",
            owner_type="system",
        ),
    )

    assert config.base_url == _CLIPROXY
    assert config.base_url != _ATTACKER


def test_there_is_no_admin_exemption():
    """Deliberate, and pinned so it stays a decision rather than drift.

    An earlier revision exempted admins on the grounds that they already reach
    destination-plus-key through the admin-gated settings route. That reasoning
    does not survive the placement: the threat this gate sits at CONSUMPTION
    for is a config file planted by ``file_write``, and a planted file's author
    is not the thread's owner. Worse, on the documented solo deployment the
    default user IS the admin, so the exemption switched the control off
    exactly where the planted-file threat lives.
    """
    config, _ = _resolve(thread_base_url=_ATTACKER, role="admin")

    assert config.base_url == _CLIPROXY
    assert config.base_url != _ATTACKER


# --- provenance: whose key is it, really ----------------------------------


def test_a_credential_reference_to_the_operators_record_is_not_bringing_your_own():
    """The bypass. Presence of a key is not ownership of one.

    ``llm_config.api_key`` accepts ``${credential:id.field}``, and the vault
    deliberately lets every identified principal READ a system-owned record,
    because resolving the deployment-wide LLM key is what makes an ordinary
    user's turn work. So a caller can point at the operator's credential
    without ever holding it, and a check that only asked "is there a key
    here?" read that as the caller supplying their own, which opened the gate
    and mailed the operator's key to the caller's address.
    """
    vault = _FakeVault({"cred_system": ("system", None, {"api_key": _OPERATOR_KEY})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_system.api_key}",
        vault=vault,
    )

    assert config.base_url == _CLIPROXY
    assert config.base_url != _ATTACKER


def test_a_credential_reference_to_the_callers_own_record_is():
    """Differential against the test above: same shape, ownership flipped."""
    vault = _FakeVault({"cred_alice": ("user", "alice", {"api_key": "alice-own-key"})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_alice.api_key}",
        vault=vault,
    )

    assert config.base_url == _ATTACKER
    assert config.api_key == "alice-own-key"


def test_a_reference_to_another_users_record_is_not_bringing_your_own():
    """User-owned is not enough; it has to be owned by THIS caller."""
    vault = _FakeVault({"cred_bob": ("user", "bob", {"api_key": "bob-own-key"})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_bob.api_key}",
        vault=vault,
    )

    assert config.base_url == _CLIPROXY


def test_an_empty_resolved_key_does_not_count_as_bringing_your_own():
    """A key that resolves to nothing is not a key.

    The provider factory backfills the operator's env key whenever
    ``config.api_key`` is falsy, so an empty secret field would otherwise buy
    the operator's credential at an address of the caller's choosing while
    looking, to a presence check, like the caller had brought their own.
    """
    vault = _FakeVault({"cred_empty": ("user", "alice", {"api_key": ""})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_empty.api_key}",
        vault=vault,
    )

    assert config.base_url == _CLIPROXY


def test_an_unknown_credential_provenance_fails_closed_at_both_read_sites():
    """The field has two consumers that want OPPOSITE defaults.

    One asks "is this the caller's own key", where ``"user"`` opens the gate.
    The other asks "is this the operator's own configuration", where
    ``"system"`` exempts the address from being gated at all. So neither real
    value is a safe default, and a record built without the field has to be
    refused by both rather than waved through by one. Here that record carries
    an attacker address AND the operator's key, so a miss on either site is a
    live leak.
    """
    credential = LLMProviderCredential(api_key=_OPERATOR_KEY, base_url=_ATTACKER, credential_id="c")

    assert credential.owner_type == ""

    # `llm_base_url` has to be unset or the global arm resolves a destination
    # first and the vault-record arm is never reached, which would make this
    # test green without exercising the site it is about.
    config, _ = _resolve(vault_credential=credential, llm_base_url=None)

    assert config.base_url != _ATTACKER


def test_an_empty_override_cannot_borrow_provenance_from_a_second_record():
    """The bypass that two spellings of one question allowed.

    ONE vault record supplies both halves: a real key, so the caller owns a
    user-owned credential for this provider, and an empty field, so the
    thread's override resolves to nothing. The provenance test asked about the
    RESOLVED key and therefore fell through to the vault arm and answered "the
    caller's own"; the assignment asked about RAW PRESENCE and shipped the
    empty string, which the provider factory backfills with the operator's env
    key. Two REST calls a non-admin can make, no file write, no injection.

    What must hold is about EGRESS, not about the address: a caller who really
    does hold their own key may name any destination, so the assertion is that
    the key which rides is theirs and is not the empty string the factory
    would replace with the operator's.
    """
    vault = _FakeVault(
        {"cred_alice": ("user", "alice", {"api_key": "ALICE-OWN-KEY", "blank": ""})}
    )

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_alice.blank}",
        vault=vault,
        vault_credential=LLMProviderCredential(
            api_key="ALICE-OWN-KEY",
            base_url=None,
            credential_id="cred_alice",
            owner_type="user",
        ),
    )

    assert config.api_key == "ALICE-OWN-KEY"
    assert config.api_key != _OPERATOR_KEY
    assert config.api_key != ""


def test_an_empty_override_beside_the_operators_own_record_is_still_refused():
    """The same shape with the ownership flipped.

    An empty override plus a SYSTEM-owned vault record is the operator's
    credential either way, so the destination has to be refused rather than
    merely re-keyed. This is the leg that would still leak if the empty-key
    fix had been written as "trust the vault arm".
    """
    vault = _FakeVault({"cred_op": ("system", None, {"blank": ""})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_op.blank}",
        vault=vault,
        vault_credential=LLMProviderCredential(
            api_key=_OPERATOR_KEY,
            base_url=None,
            credential_id="cred_op",
            owner_type="system",
        ),
    )

    assert config.base_url == _CLIPROXY


def test_a_deleted_credential_id_does_not_take_the_turn_down():
    """``CredentialNotFound`` is a LookupError, so it used to escape reference
    resolution entirely. Config resolution runs on read paths (thread
    overview, ``file_read``) as well as turns, so one stale id in one thread
    file crashed all of them."""
    vault = _FakeVault({})

    config, _ = _resolve(
        thread_base_url="${credential:cred_gone.base_url}",
        vault=vault,
    )

    assert config.base_url == _CLIPROXY


def test_the_refusal_never_discloses_a_resolved_secret():
    """``base_url`` interpolates ``${credential:...}`` too, so the resolved
    candidate can BE a decrypted secret. Everything downstream of the refusal
    is a disclosure channel: a WARNING log, a field persisted to disk, a
    ``GET /threads/{id}/config`` response, and note text folded into the next
    turn's message. Only the destination KEY may travel."""
    vault = _FakeVault({"cred_system": ("system", None, {"api_key": _OPERATOR_KEY})})

    agent, patcher = _make_agent(
        thread_base_url="${credential:cred_system.api_key}", vault=vault
    )
    try:
        agent._get_llm_config_for_thread("thread-1")
    finally:
        patcher.stop()

    tc = agent.thread_config_manager.get_config.return_value
    assert _OPERATOR_KEY not in str(tc.rejected_llm_base_url)
    assert _OPERATOR_KEY not in str(tc.pending_fallback_note)


# --- the credentialed internal probe --------------------------------------


def test_the_local_server_probe_never_sees_a_rejected_destination():
    """``agent_llm_config`` probes any loopback/private base_url WITH the key
    attached, BEFORE the main call, so a caller could port-scan the container
    network and have the operator key mailed to whatever answered.

    This is why loopback and private-LAN destinations are not exempt from the
    gate even though they cannot leave the building. The differential leg
    below is what makes the empty ``seen`` meaningful: the probe fires for a
    local destination the caller is entitled to, so an empty list for the
    refused one is the gate working, not the probe being unreachable.
    """
    seen: list[str] = []

    def _detect(base_url, api_key=None, **kwargs):
        seen.append(str(base_url))
        return None

    agent, patcher = _make_agent(thread_base_url="http://127.0.0.1:6379")
    try:
        with patch("nymeria.core.agent_llm_config.detect_local_server_type", _detect):
            config = agent._get_llm_config_for_thread("thread-1")
    finally:
        patcher.stop()

    assert "127.0.0.1:6379" not in str(config.base_url or "")
    assert not any("6379" in url for url in seen)

    # Same probe, same fixture, caller entitled to the destination.
    seen.clear()
    agent, patcher = _make_agent(
        thread_base_url="http://127.0.0.1:6379", thread_api_key="my-own-key"
    )
    try:
        with patch("nymeria.core.agent_llm_config.detect_local_server_type", _detect):
            agent._get_llm_config_for_thread("thread-1")
    finally:
        patcher.stop()

    assert any("6379" in url for url in seen)


def test_owning_a_credential_is_not_the_same_as_the_operators_key_staying_home():
    """Whose key SHIPS, not whether the caller owns one somewhere.

    The key precedence is thread override > vault record > environment, so a
    caller who owns any user-owned record for this provider owns "a"
    credential while their thread override still names the OPERATOR's record.
    An ownership test that was an OR over both arms read that as
    bring-your-own and opened the gate on the operator's key. `auth_write`
    lets an agent create the user-owned record, and `GET /credentials`
    discloses the system record's id, so both halves are reachable.
    """
    vault = _FakeVault({"cred_system": ("system", None, {"api_key": _OPERATOR_KEY})})

    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_api_key="${credential:cred_system.api_key}",
        vault=vault,
        vault_credential=LLMProviderCredential(
            api_key="alice-own-key",
            base_url=None,
            credential_id="cred_alice",
            owner_type="user",
        ),
    )

    assert config.base_url == _CLIPROXY
    assert config.base_url != _ATTACKER


def test_a_vault_record_is_a_destination_source_too_and_is_gated():
    """The gate covers every caller-provenance destination, not one field.

    A user-owned vault record carries a `base_url`, and it is consulted even
    by a thread that names no destination of its own, so gating only
    `llm_config.base_url` left the same theft one indirection away: the
    address comes from the caller's record while the key comes from the
    operator's, and the thread looks untouched.
    """
    vault = _FakeVault({"cred_system": ("system", None, {"api_key": _OPERATOR_KEY})})

    config, _ = _resolve(
        thread_api_key="${credential:cred_system.api_key}",
        llm_base_url=None,
        vault=vault,
        vault_credential=LLMProviderCredential(
            api_key="alice-own-key",
            base_url=_ATTACKER,
            credential_id="cred_alice",
            owner_type="user",
        ),
    )

    assert _ATTACKER not in str(config.base_url or "")


def test_an_operator_owned_vault_record_may_name_its_own_destination():
    """Differential against the test above: a SYSTEM record is the operator's
    own configuration, so its base_url is config provenance and is not gated.
    Without this leg the test above could pass by gating everything."""
    config, _ = _resolve(
        llm_base_url=None,
        vault_credential=LLMProviderCredential(
            api_key=_OPERATOR_KEY,
            base_url="https://operator-gateway.example",
            credential_id="cred_system",
            owner_type="system",
        ),
    )

    assert config.base_url == "https://operator-gateway.example"


def test_the_vault_credentials_owner_type_is_read_from_the_record():
    """The one field the whole vault arm turns on, and its dataclass default
    is the fail-open value. Every other test here injects a hand-built
    credential, so nothing else would notice if this kwarg were dropped in a
    refactor: system records would start reading as caller-owned."""
    from nymeria.core.llm_credentials import get_llm_provider_credential

    record = SimpleNamespace(
        id="cred_system",
        owner_type="system",
        owner_user_id=None,
        name="operator llm",
        provider="openai",
        kind="api_key",
        status="active",
        allowed_targets=["llm_provider:*"],
        metadata={},
        secret_fields=["api_key"],
    )
    vault = SimpleNamespace(
        list_credentials=lambda **kwargs: [record],
        get_secret_field=lambda *a, **k: _OPERATOR_KEY,
        list_bindings=lambda **kwargs: [],
    )

    resolved = get_llm_provider_credential("openai", vault=vault, owner_user_id="alice")

    assert resolved is not None
    assert resolved.owner_type == "system"


# --- not refusing what the deployment legitimately supports ----------------


def test_a_global_base_url_does_not_suppress_another_providers_own_host():
    """`LLM_BASE_URL` names a destination for the GLOBAL provider only.

    Letting it suppress every other provider's canonical host refused threads
    pointed at the operator's own documented backup provider, whose key is a
    real key for that vendor going to that vendor. The fix map names exactly
    this ("point a thread at CLIProxy, Ollama or OpenRouter") as the capability
    the control must not cost.
    """
    proxied = _Settings()  # llm_provider="openai", llm_base_url=CLIProxy

    for provider, url in (
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("ollama", "http://localhost:11434"),
    ):
        assert not _redirects(
            url, provider=provider, settings=proxied
        ), provider

    # Still suppressed for the provider the global setting actually names.
    assert _redirects(
        "https://api.openai.com/v1", provider="openai", settings=proxied
    )


# --- the fallback chain ---------------------------------------------------


def test_a_refused_destination_never_reappears_on_a_fallback_candidate():
    """The gate guards ONE assignment; the fallback chain builds its own.

    ``base_url_for_provider`` reads settings, the vault and the registry and
    deliberately never reads the per-thread field, so a refused destination
    cannot come back through a fallback candidate. Nothing pinned that, and
    the whole control would be void if it changed.
    """
    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        # Neither entry may repeat the active provider AND model: the chain
        # skips that pair, so spelling the global `openai:gpt-5.5` here would
        # silently reduce this to a single-candidate test.
        llm_fallback_models="anthropic:claude-opus-4-8,openai:gpt-5.5-mini",
    )

    assert len(config.fallbacks) == 2
    for candidate in config.fallbacks:
        assert _ATTACKER not in str(candidate.base_url or "")


def test_the_openai_arm_of_a_cliproxy_deployment_stays_on_the_proxy():
    """One container fronts both providers, so a per-thread provider override
    is answerable from the operator's own setting.

    Without this the openai arm fell through to api.openai.com holding a
    proxy-local ``cpx-*`` key that cannot authenticate there, which also made
    the refusal path land somewhere useless rather than somewhere correct.
    """
    config, _ = _resolve(
        thread_base_url=_ATTACKER,
        thread_provider="openai",
        llm_provider="anthropic",
        llm_base_url="http://cli-proxy-api:8317",
    )

    assert config.base_url == "http://cli-proxy-api:8317/v1"
    assert config.base_url != _ATTACKER


# --- telling the user, once -----------------------------------------------


def test_the_user_is_told_once_and_not_once_per_turn():
    """The locked precedent is a persisted note, never a per-turn injection.

    Guarded on the refused HOST rather than on the note, because the note latch
    is consumed at the start of the next turn: without the host guard a
    persistently bad config would re-latch forever.
    """
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        agent._get_llm_config_for_thread("thread-1")
        saves_after_first = agent.thread_config_manager.save_config.call_count
        tc = agent.thread_config_manager.get_config.return_value

        assert saves_after_first == 1
        assert tc.rejected_llm_base_url == _ATTACKER
        assert tc.pending_fallback_note is not None
        assert _ATTACKER in tc.pending_fallback_note["text"]
        assert tc.pending_fallback_note["kind"] == "destination"

        # Second turn on the same bad config, with the note consumed the way
        # the real turn start consumes it. The HOST guard is what has to stop
        # the re-latch here, not the occupied note slot.
        tc.pending_fallback_note = None
        agent._get_llm_config_for_thread("thread-1")
        assert agent.thread_config_manager.save_config.call_count == saves_after_first
    finally:
        patcher.stop()


def test_a_changed_destination_is_a_new_fact_and_re_notes():
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        agent._get_llm_config_for_thread("thread-1")
        tc = agent.thread_config_manager.get_config.return_value
        tc.pending_fallback_note = None
        tc.llm_config.base_url = "https://other-attacker.example"

        agent._get_llm_config_for_thread("thread-1")

        assert tc.rejected_llm_base_url == "https://other-attacker.example"
        assert agent.thread_config_manager.save_config.call_count == 2
    finally:
        patcher.stop()


def test_a_pending_fallback_note_is_not_clobbered():
    """One pending slot, and a fallback note already in it is the end of an
    episode the model has not been told about yet. Deferring costs nothing:
    nothing is written, so the host guard still misses next turn."""
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        tc = agent.thread_config_manager.get_config.return_value
        tc.pending_fallback_note = {"kind": "transport", "text": "[System info]: earlier"}

        agent._get_llm_config_for_thread("thread-1")

        assert tc.pending_fallback_note["kind"] == "transport"
        assert tc.rejected_llm_base_url is None
        assert agent.thread_config_manager.save_config.call_count == 0

        # Slot freed: the notice is not lost, it lands on the next resolution.
        tc.pending_fallback_note = None
        agent._get_llm_config_for_thread("thread-1")

        assert tc.pending_fallback_note["kind"] == "destination"
    finally:
        patcher.stop()


def test_an_accepted_destination_clears_a_past_refusal():
    """The field means "currently refused", not "ever refused".

    Left uncleared it would pin ``has_customizations()`` true forever, so the
    config file could never be deleted again, and a bad-then-fixed-then-same-bad
    sequence would be silent because the latch guard would still match.
    """
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        agent._get_llm_config_for_thread("thread-1")
        tc = agent.thread_config_manager.get_config.return_value
        assert tc.rejected_llm_base_url is not None

        tc.llm_config.base_url = _CLIPROXY
        agent._get_llm_config_for_thread("thread-1")

        assert tc.rejected_llm_base_url is None
    finally:
        patcher.stop()


def test_removing_the_address_entirely_also_clears_the_refusal():
    """Deleting the offending `base_url` is the natural fix, and it has to
    clear the marker too. Clearing only on an ACCEPTED destination missed it,
    leaving `has_customizations()` pinned true and the next identical mistake
    silent."""
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        agent._get_llm_config_for_thread("thread-1")
        tc = agent.thread_config_manager.get_config.return_value
        assert tc.rejected_llm_base_url is not None

        tc.llm_config.base_url = None
        agent._get_llm_config_for_thread("thread-1")

        assert tc.rejected_llm_base_url is None
    finally:
        patcher.stop()


def test_a_failed_note_save_does_not_fail_the_turn():
    """The notice is how the user is told, not how the credential is
    protected. A persistence failure must not take the turn down with it."""
    agent, patcher = _make_agent(thread_base_url=_ATTACKER)
    try:
        agent.thread_config_manager.save_config.side_effect = RuntimeError("disk full")
        config = agent._get_llm_config_for_thread("thread-1")
    finally:
        patcher.stop()

    assert config.base_url == _CLIPROXY


def test_the_notice_summary_does_not_read_as_a_model_switch():
    """It rides the fallback-notice channel, so without its own branch the
    one-line copy would claim a model failed and was swapped."""
    from nymeria.core.agent_history import _fallback_notice_summary
    from nymeria.core.agent_llm_config import rejected_destination_note_stamp

    summary = _fallback_notice_summary(rejected_destination_note_stamp(_ATTACKER))

    assert "switched to" not in summary
    assert "failing" not in summary
    assert "endpoint" in summary


@pytest.mark.parametrize("thread_base_url", ["", None])
def test_a_thread_that_names_no_destination_is_untouched(thread_base_url):
    agent, patcher = _make_agent(thread_base_url=thread_base_url, thread_api_key="k")
    try:
        agent._get_llm_config_for_thread("thread-1")
    finally:
        patcher.stop()

    tc = agent.thread_config_manager.get_config.return_value
    assert getattr(tc, "rejected_llm_base_url", None) is None
