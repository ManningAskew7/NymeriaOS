"""Central registry of provider credential specs for native integration tools.

Each ``*_service_integrations`` module declares one ``ProviderCredentialSpec``
per third-party provider it wraps and registers it at import time via
``register_provider_spec``. The spec is the single source of truth for that
provider's credential shape: the canonical vault ``provider`` key, accepted
aliases, the ordered field-name lookup tuples the module's config helpers pass
to ``_credential_value`` (order is behaviorally significant: the vault lookup
tries names in order and takes the first match), the human-facing required
field list used in setup hints, and the env/settings fallbacks.

``tools/__init__`` imports every integration module, so the registry is
complete whenever the tool system is loaded. Consumers:

- the module's own config helpers (they source ``_credential_value`` /
  ``_setup_hint`` arguments from the spec instead of inline literals),
- ``auth_test`` and the credential-management tools (which provider and
  fields does tool X need; is a usable credential saved),
- future surfacing per dev-todo #7/#8 (auth status in tool_search, the
  enable-time credential nudge).

A provider registered from two modules must be byte-identical (re-registration
of an equal spec is a no-op); conflicting re-registration is an import-time
error. Share one spec object across modules instead of re-declaring it.

Alias overlap across providers is legitimate and tolerated (aws and s3
mutually alias each other; the freshdesk/freshservice/freshworks_crm family
shares the "freshworks" alias). The name index resolves a contested name to
its canonical owner if one exists, else to the first-registered claimant;
each spec's own ``aliases`` tuple is preserved verbatim for the runtime vault
lookup either way. Only colliding canonical provider names raise.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialFieldGroup:
    """One credential field the provider needs, with its ordered name aliases.

    ``names`` is the exact ordered tuple the config helper passes to
    ``_credential_value`` for this field (first vault match wins).
    ``required=False`` marks fields with a non-credential fallback (typically a
    base URL with a settings attribute or constant default).
    """

    role: str
    names: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class ProviderCredentialSpec:
    """Credential shape for one third-party provider."""

    provider: str
    aliases: tuple[str, ...] = ()
    groups: tuple[CredentialFieldGroup, ...] = ()
    hint_fields: tuple[str, ...] = ()
    env_var: str = ""
    display_name: str = ""
    settings_attr: str = ""
    env_vars: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    # Explicit tool-name claims, for tools whose taxonomy service is too
    # coarse to identify the provider (the web_search_* family all share the
    # service "web"). Checked before the taxonomy in spec_for_tool.
    tools: tuple[str, ...] = ()

    def group(self, role: str) -> tuple[str, ...]:
        """Return the ordered field-name tuple for ``role`` (loud on typos)."""
        for grp in self.groups:
            if grp.role == role:
                return grp.names
        raise KeyError(
            f'provider "{self.provider}" has no credential field group with role "{role}"'
        )

    @property
    def required_groups(self) -> tuple[CredentialFieldGroup, ...]:
        return tuple(grp for grp in self.groups if grp.required)

    @property
    def service_keys(self) -> tuple[str, ...]:
        """Taxonomy service keys claiming this provider (defaults to itself)."""
        return self.services or (self.provider,)

    @property
    def label(self) -> str:
        return self.display_name or self.provider


def _lookup_candidates(name: str) -> set[str]:
    """Match-candidate expansion, mirroring native_credentials semantics.

    The import is function-local because it is now CYCLE-BREAKING, not merely
    lazy: `native_credentials` imports this module at module scope for the
    destination register, so the two are mutually dependent and a module-scope
    import back would not resolve. Same for the other two in this file.
    """
    from .native_credentials import provider_candidates

    return provider_candidates(name, ())


_lock = threading.Lock()
_SPECS: dict[str, ProviderCredentialSpec] = {}
_NAME_INDEX: dict[str, str] = {}
_CANONICAL_NAMES: dict[str, str] = {}
_SERVICE_INDEX: dict[str, str] = {}
_TOOL_INDEX: dict[str, str] = {}


def _validate(spec: ProviderCredentialSpec) -> None:
    if not spec.provider or spec.provider != spec.provider.strip():
        raise ValueError(f"invalid provider key: {spec.provider!r}")
    if not all(alias and alias == alias.strip() for alias in spec.aliases):
        raise ValueError(f'provider "{spec.provider}" declares an empty or padded alias')
    roles = [grp.role for grp in spec.groups]
    if len(roles) != len(set(roles)):
        raise ValueError(f'provider "{spec.provider}" declares duplicate group roles')
    for grp in spec.groups:
        if not grp.names or not all(grp.names):
            raise ValueError(
                f'provider "{spec.provider}" group "{grp.role}" has empty field names'
            )


def register_provider_spec(spec: ProviderCredentialSpec) -> ProviderCredentialSpec:
    """Register ``spec`` and return it (for module-local use).

    Registering an identical spec again is a no-op; a conflicting spec for the
    same provider raises at import time so drift between two modules wrapping
    the same provider is caught immediately.

    Cross-provider alias overlap does NOT raise: it exists in the real data
    (aws/s3, the freshworks family). For a contested name the index prefers
    the provider whose canonical name it is; failing that, the first
    registrant keeps it (module import order is fixed, so this is
    deterministic). Colliding canonical provider names are a genuine error.
    """
    _validate(spec)
    with _lock:
        existing = _SPECS.get(spec.provider)
        if existing is not None:
            if existing == spec:
                return existing
            raise ValueError(
                f'conflicting ProviderCredentialSpec registrations for "{spec.provider}"; '
                "share one spec object across modules instead of re-declaring it"
            )
        canonical_candidates = _lookup_candidates(spec.provider)
        for candidate in canonical_candidates:
            claimed = _CANONICAL_NAMES.get(candidate)
            if claimed is not None and claimed != spec.provider:
                raise ValueError(
                    f'canonical provider names collide on "{candidate}": '
                    f'"{claimed}" and "{spec.provider}"'
                )
        for service in spec.service_keys:
            claimed = _SERVICE_INDEX.get(service)
            if claimed is not None and claimed != spec.provider:
                raise ValueError(
                    f'taxonomy service "{service}" is claimed by both '
                    f'"{claimed}" and "{spec.provider}"'
                )
        for tool_name in spec.tools:
            claimed = _TOOL_INDEX.get(tool_name)
            if claimed is not None and claimed != spec.provider:
                raise ValueError(
                    f'tool "{tool_name}" is claimed by both '
                    f'"{claimed}" and "{spec.provider}"'
                )
        _SPECS[spec.provider] = spec
        for candidate in canonical_candidates:
            shadowed = _NAME_INDEX.get(candidate)
            if shadowed is not None and shadowed != spec.provider:
                logger.debug(
                    'canonical provider "%s" takes over name "%s" previously '
                    'aliased to "%s"',
                    spec.provider,
                    candidate,
                    shadowed,
                )
            _NAME_INDEX[candidate] = spec.provider
            _CANONICAL_NAMES[candidate] = spec.provider
        for alias in spec.aliases:
            for candidate in _lookup_candidates(alias):
                claimed = _NAME_INDEX.get(candidate)
                if claimed is None:
                    _NAME_INDEX[candidate] = spec.provider
                elif claimed != spec.provider:
                    logger.debug(
                        'alias "%s" of provider "%s" stays resolved to "%s" '
                        "(canonical owner or earlier registrant wins)",
                        candidate,
                        spec.provider,
                        claimed,
                    )
        for service in spec.service_keys:
            _SERVICE_INDEX[service] = spec.provider
        for tool_name in spec.tools:
            _TOOL_INDEX[tool_name] = spec.provider
    return spec


# ---------------------------------------------------------------------------
# Destination classification (E10-02 slice B)
# ---------------------------------------------------------------------------
#
# Which credential fields are DESTINATIONS, meaning their value decides where a
# request goes rather than what it proves. `native_credentials` uses this to
# refuse serving a destination from a record that does not also carry the
# provider's own credential, so one record cannot steer a request another record
# authenticates. The rule and its residuals are in `SECURITY.md` 2.5, shape (c).
#
# Keyed on the PRIMARY field name, `CredentialFieldGroup.names[0]`, because
# individual ALIASES do not partition: `value` is a `base_url` alias for searxng
# and an alias of 27 secret roles elsewhere, and `url` and `domain` are also
# `link_name` aliases. Primaries do not partition perfectly either, and the
# register resolves that by OVER-classifying, which is the safe direction: nine
# primaries here are declared by more than one spec and are a real host switch
# for only some of them (`api_version` is declared by five, and only Invoice
# Ninja changes registrable domain on it). Ties break toward destination, so a
# few lookups are gated that need not be; the reverse would be a hole.
#
# A ROLE-keyed register would give the identical result here (measured: zero
# differing specs), since classification runs over `spec.groups` and never over
# call sites. Primary-keyed is kept because it is the same key the non-proof
# register below needs, and THAT one genuinely cannot use roles alone (mailjet).
# Do not "simplify" this to roles without re-reading that constraint.
#
# `tests/test_credential_destination_gate.py` walks the tools package and fails
# when any requested primary is in neither this set nor the declared inert set,
# so a NEW destination field name cannot default into the inert half.
DESTINATION_PRIMARY_FIELDS: frozenset[str] = frozenset({
    # The value is a URL.
    "accounts_base_url", "api_base_url", "api_url", "app_base_url", "base_url",
    "clearbit_autocomplete_base_url", "clearbit_company_base_url",
    "clearbit_person_base_url", "connections_url", "content_base_url", "endpoint",
    "endpoint_url", "graphql_url", "instance_url", "jina_deepsearch_base_url",
    "jina_reader_base_url", "jina_search_base_url", "management_base_url",
    "preview_base_url", "public_base_url", "registry_url", "token_url",
    "tracking_base_url", "url", "webdav_url", "webhook_url",
    # The value is interpolated into a hostname.
    "app_name", "cloud_domain", "domain", "host", "instance", "region",
    "server_prefix", "shop_subdomain", "site", "subdomain",
    # The value switches the host between hard-coded vendor constants. Real host
    # changes, closed set. `api_version` is here because Invoice Ninja picks
    # between app.invoiceninja.com and invoicing.co on it; its other four sites
    # are path segments only, and the closed set is what makes that safe.
    "api_plan", "api_version", "classic_api", "environment", "sandbox",
})


def destination_fields_for(spec: ProviderCredentialSpec) -> frozenset[str]:
    """Every field name (all aliases) this provider treats as a destination.

    This, not the requested primary alone, is what decides whether a lookup is a
    destination lookup. Membership is judged PER SPEC, which is what makes the
    ambiguous aliases harmless: ``value`` is a ``base_url`` alias for searxng and
    an alias of 27 secret roles elsewhere, and only the owning spec can say which
    it is here. No spec shares a name between a destination group and any other
    group, so this classifies every lookup exactly (asserted in
    ``tests/test_credential_destination_gate.py``).
    """
    return frozenset(
        name
        for group in spec.groups
        if group.names[0] in DESTINATION_PRIMARY_FIELDS
        for name in group.names
    )


# Non-destination groups that are still NOT proof of possession: the public half
# of a keyed pair, a bare account identifier, a version, a header name, or a
# transport switch. A record carrying only one of these has demonstrated
# nothing, so it must not anchor a destination. Without this, Reddit's
# `client_id` would have anchored the very lookup that hands the operator's
# `client_secret` to a vault-supplied token endpoint.
#
# ONE set of names, matched against a group's ROLE and against its PRIMARY.
# Both keys are needed, and the whole register needs exactly three of them:
# mailjet is caught only by role, aws and s3 only by primary, and the other 33
# excluded groups spell their role and their primary the same word. An earlier
# version kept two sets and wrote 29 of the 40 names into both, which is a
# hazard rather than redundancy: editing one and not the other is a silent
# no-op in a security register. The counterexamples are real:
#
# - Mailjet's public half is `role="email_api_key"` with
#   `names=("api_key", "apiKey", "public_key", "publicKey", "username")`. Its
#   PRIMARY is `api_key`, a genuine secret name almost everywhere else, so a
#   primary-keyed check misses it and Mailjet's public key anchors a
#   destination. The role names it exactly.
# - The role `access_key` means opposite things in two specs: AWS declares it
#   with `names=("access_key_id", ...)`, the public half, while MessageBird
#   declares it with `names=("access_key", ...)`, its actual secret. So a
#   role-keyed check alone is wrong too, and AWS is caught by its primary.
#
# The list is explicit because the question it answers is semantic and the
# registry does not encode it. `required` was tried as a proxy and is wrong in
# both directions: Jira and Zendesk mark bearer auth `required=False` precisely
# because it is the ALTERNATIVE to basic auth, so a legitimate OAuth record
# holds nothing marked required, and sixteen genuine destinations are marked
# required because a self-hosted instance URL is mandatory.
#
# Judged per provider, not by name shape. `service_role` is Supabase's actual
# service-role key and DOES anchor; `account_key` and `organizer_key` are
# GoToWebinar identifiers whose own hint names `access_token` as the credential,
# and `user_key` is Pushover's user identifier, so none of the three does. The
# TLS and header entries (`ignore_ssl*`, `allow_unauthorized_certs`, `headers*`,
# `api_key_header`) are a separate defect of the same independence shape: they
# are not addresses, so this control does not cover them and they are tracked on
# their own.
#
# Every entry here excludes at least one real group, asserted by
# `test_no_non_proof_entry_is_dead_weight`. `link_name` used to be here and
# is not, because the alias-collision rule below already covers the only
# group that declares it (salesmate's, which aliases `url` and `domain`);
# an entry that excludes nothing reads as coverage it does not provide.
_NON_PROOF_NAMES: frozenset[str] = frozenset({
    "access_key_id", "account_id", "account_key", "account_sid",
    "algorithm", "allow_insecure", "allow_unauthorized_certs", "api_id",
    "api_key_header", "api_key_sid", "api_username", "app_id",
    "auth_header", "auth_id", "business_account_id", "client_id",
    "database", "email", "email_api_key", "force_path_style", "from_email",
    "headers", "headers_json", "hostname", "ignore_ssl",
    "ignore_ssl_issues", "intercom_version", "notion_version",
    "organizer_key", "phone_number_id", "public_key", "realm_id",
    "space_id", "tenant_id", "tracking_site_id", "user_key", "username",
    "vendor_id", "version",
})


def credential_anchor_fields(spec: ProviderCredentialSpec) -> frozenset[str]:
    """Field names whose presence proves a record holds this provider's own credential.

    Everything that is not a destination and whose group is not on the
    non-proof list above. Stated as an exclusion rather than an allowlist of secret-looking
    names because a name heuristic drifts in both directions: it reads
    ``api_key_header`` and ``allow_unauthorized_certs`` as secrets, and misses
    ``service_role``.

    All aliases of an anchor group count, not just its primary, because a record
    may store any of them. That is also what separates MessageBird's
    ``access_key`` (its actual secret) from AWS's ``access_key`` role, whose
    names lead with ``access_key_id``, the public half of a pair.

    A group is disqualified when ANY of its names is a destination primary, not
    just its own primary. Salesmate found this: its required ``link_name`` group
    aliases ``url`` and ``domain``, so a record holding nothing but ``domain``
    would have proved possession using a value that is an address everywhere
    else in the register. The rule is that a name which is a destination
    somewhere can never be proof of possession anywhere.

    This set is deliberately not the whole control. It says which names COULD
    prove possession; ``native_credentials`` decides whether a given record
    actually did, by requiring it to hold every anchor name any other visible
    record holds. That completeness rule is what makes an imperfect entry here
    survivable: a record naming an anchor it does not really own still has to
    out-hold the record it is trying to steer, and if it does, it also wins the
    secret lookup and only sends its own junk to its own address.

    An empty result means the provider declares nothing to protect and callers
    must NOT gate. ``searxng`` is the only one: a bare instance URL with no
    credential anywhere in its spec.
    """
    return frozenset(
        name
        for group in spec.groups
        if group.role not in _NON_PROOF_NAMES
        and group.names[0] not in _NON_PROOF_NAMES
        and not (set(group.names) & DESTINATION_PRIMARY_FIELDS)
        for name in group.names
    )


def get_provider_spec(name: str) -> Optional[ProviderCredentialSpec]:
    """Resolve a spec by canonical provider, alias, or dash/underscore variant."""
    if not name:
        return None
    cleaned = name.strip().lower()
    for candidate in (cleaned, cleaned.replace("-", "_"), cleaned.replace("_", "-")):
        canonical = _NAME_INDEX.get(candidate)
        if canonical is not None:
            return _SPECS.get(canonical)
    return None


def spec_for_tool(tool_name: str) -> Optional[ProviderCredentialSpec]:
    """Resolve the provider spec a native tool's credentials come from.

    Exact ``spec.tools`` claims win, then the integration taxonomy's
    longest-prefix service match (the same mapping the tool menus render).
    Tools nothing claims (and non-integration tools) return None, which
    consumers must treat as "no credential required": the safe default
    dev-todo #7 specifies.
    """
    if not tool_name:
        return None
    canonical = _TOOL_INDEX.get(tool_name)
    if canonical is not None:
        return _SPECS.get(canonical)
    from .integration_taxonomy import get_integration_grouping

    grouping = get_integration_grouping(tool_name)
    if not grouping:
        return None
    canonical = _SERVICE_INDEX.get(grouping["service"])
    if canonical is None:
        return None
    return _SPECS.get(canonical)


def iter_provider_specs() -> Iterator[ProviderCredentialSpec]:
    with _lock:
        specs = sorted(_SPECS.values(), key=lambda spec: spec.provider)
    yield from specs


def tools_hint_for_spec(spec: ProviderCredentialSpec) -> str:
    """Human/agent-facing one-liner describing what the provider needs."""
    required = ", ".join(f'"{grp.names[0]}"' for grp in spec.required_groups)
    if not required:
        return f"{spec.label}: no credential required (optional overrides only)."
    text = f"{spec.label}: provider \"{spec.provider}\", required field(s) {required}."
    if spec.env_var:
        text += f" Env fallback: {spec.env_var}."
    return text


def provider_credential_status(spec: ProviderCredentialSpec, user_id: str) -> str:
    """Vault presence status for ``spec`` as seen by ``user_id``.

    Returns "connected" (an active record covers every required field group),
    "pending" (only pending_setup records exist), "needs_setup" (required
    fields, nothing usable saved), or "optional" (no required fields). Reads
    metadata only; never touches secret values.

    Provider-level and metadata-only, so "connected" means a usable-looking
    record exists for the provider, not that a given tool's lookup will
    succeed: it does not replicate the runtime allowed_targets scoping, and a
    record whose ciphertext can no longer be decrypted still counts. Any
    vault/infra failure is swallowed and reads as nothing saved (fail-closed:
    the helper may understate, never overstate).
    """
    matches: list = []
    try:
        from ..core.credential_vault import get_credential_vault_repo
        from .native_credentials import provider_candidates

        # Exactly the candidate set the runtime lookup uses, so "connected"
        # here never claims a record get_native_credential_value would miss.
        candidates = provider_candidates(spec.provider, spec.aliases)
        repo = get_credential_vault_repo()
        matches = [
            record
            for record in repo.list_credentials(owner_user_id=user_id, include_system=True)
            if record.provider in candidates
        ]
    except Exception:
        logger.debug(
            "Credential status lookup failed for provider %s", spec.provider, exc_info=True
        )

    return _status_from_matches(spec, matches)


def _status_from_matches(spec: ProviderCredentialSpec, matches: list) -> str:
    required = spec.required_groups
    if not required:
        return "connected" if any(r.status == "active" for r in matches) else "optional"

    def _covers(record) -> bool:
        saved = set(record.secret_fields or ())
        return all(any(name in saved for name in grp.names) for grp in required)

    if any(record.status == "active" and _covers(record) for record in matches):
        return "connected"
    if any(record.status == "pending_setup" for record in matches):
        return "pending"
    return "needs_setup"


def provider_credential_status_map(
    specs: "Iterable[ProviderCredentialSpec]", user_id: str
) -> dict[str, str]:
    """Batch ``provider_credential_status``: one vault metadata read total.

    Same per-spec semantics and fail-closed behavior (a failed read counts as
    nothing saved), but the ``list_credentials`` metadata read happens once
    for the whole batch. Returns ``{spec.provider: status}``. Use this from
    whole-catalog serializers; the single-spec helper stays for one-off calls.
    """
    unique: dict[str, ProviderCredentialSpec] = {}
    for spec in specs:
        unique.setdefault(spec.provider, spec)
    if not unique:
        return {}
    records: list = []
    try:
        from ..core.credential_vault import get_credential_vault_repo

        records = get_credential_vault_repo().list_credentials(
            owner_user_id=user_id, include_system=True
        )
    except Exception:
        logger.debug("Batch credential status lookup failed", exc_info=True)

    from .native_credentials import provider_candidates

    out: dict[str, str] = {}
    for provider, spec in unique.items():
        candidates = provider_candidates(spec.provider, spec.aliases)
        matches = [record for record in records if record.provider in candidates]
        out[provider] = _status_from_matches(spec, matches)
    return out


def auth_status_for_tools(
    names: Iterable[str], user_id: str
) -> dict[str, tuple[str, str]]:
    """Batch credential axis for tool names: ``{name: (provider, status)}``.

    Tools with no provider spec are absent (= "no credential required"). One
    vault metadata read total, however many names are passed. The shared
    engine behind the tool_search overlay and the tool-list serializers
    (dev-todo #7).
    """
    spec_by_tool: dict[str, ProviderCredentialSpec] = {}
    for name in names:
        spec = spec_for_tool(name)
        if spec is not None:
            spec_by_tool[name] = spec
    statuses = provider_credential_status_map(spec_by_tool.values(), user_id)
    return {
        name: (spec.provider, statuses[spec.provider])
        for name, spec in spec_by_tool.items()
    }


__all__ = [
    "DESTINATION_PRIMARY_FIELDS",
    "CredentialFieldGroup",
    "ProviderCredentialSpec",
    "auth_status_for_tools",
    "credential_anchor_fields",
    "destination_fields_for",
    "get_provider_spec",
    "iter_provider_specs",
    "provider_credential_status",
    "provider_credential_status_map",
    "register_provider_spec",
    "spec_for_tool",
    "tools_hint_for_spec",
]
