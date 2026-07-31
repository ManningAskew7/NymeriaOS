"""Credential-vault helpers for native Nymeria tools."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from langchain_core.runnables import RunnableConfig

from .credential_registry import (
    credential_anchor_fields,
    destination_fields_for,
    get_provider_spec,
)
from .utils import get_user_id

logger = logging.getLogger(__name__)

NATIVE_TOOL_TARGET_TYPE = "native_tool"


@dataclass(frozen=True)
class NativeCredentialValue:
    value: str
    credential_id: str
    field_name: str


class CredentialDestinationRefused(ValueError):
    """A saved record supplies an address it cannot prove it also authenticates.

    Raised instead of returning ``None`` so the refusal cannot be mistaken for
    "nothing saved". Every integration spells its fallback
    ``_credential_value(...) or _settings_value(...) or VENDOR_DEFAULT``, and 95
    of those chains end in a hard-coded vendor host, so a silent None would not
    stop the request: it would send the credential to the vendor's public API
    instead. For a self-hosted provider (Baserow, NocoDB, Grist, Sentry and the
    rest) that is a disclosure to a third party, produced by the control meant to
    prevent one.

    ``ValueError`` because that is already what a bad address raises here
    (``service_integration_base.base_url``), so tool bodies and the tool node
    surface it the same way, with no new error plumbing.
    """


def provider_candidates(provider: str, aliases: Iterable[str]) -> set[str]:
    """Names a vault record's ``provider`` may carry and still match ``provider``.

    Public because ``credential_registry`` reuses the same matching semantics;
    the private name below is kept for existing callers/tests.
    """
    return {provider, *aliases, provider.replace("-", "_"), provider.replace("_", "-")}


_provider_candidates = provider_candidates


def _target_score(record: Any, tool_name: str, bound_ids: set[str]) -> int:
    allowed = set(record.allowed_targets or [])
    if record.id in bound_ids:
        return 0
    if f"{NATIVE_TOOL_TARGET_TYPE}:{tool_name}" in allowed:
        return 1
    if f"{NATIVE_TOOL_TARGET_TYPE}:*" in allowed or "*" in allowed:
        return 2
    # An empty allowed_targets used to score 3 here, as a usable-but-unscoped
    # candidate, because the vault then read it as "any target may read". It
    # denies now, so ranking such a record above 100 would only pick a
    # credential the vault is about to refuse.
    return 100


def _destination_anchor_fields(provider: str, field_names: list[str]) -> Optional[frozenset[str]]:
    """Fields a record must carry to be trusted with this DESTINATION lookup.

    ``None`` means "not a destination lookup, or nothing to protect", and the
    caller gates nothing. A non-empty set means the lookup decides where a
    request goes, so only a record that also proves it holds this provider's own
    credential may answer it.

    A lookup is a destination lookup when ANY requested name belongs to a
    destination group of THIS provider's spec. Per-spec membership is what makes
    that exact: keying on the requested primary alone left the gate disarmed for
    a tuple spelled ``("api_key", "base_url")``, and keying on a global name set
    would gate secret lookups, since ``value`` is a base-URL alias for searxng
    and a secret alias for 27 roles elsewhere.

    Fails OPEN for a provider with no registered spec, because without the spec
    there is no way to know what possession looks like and denying would break
    the integration. ``tests/test_credential_destination_gate.py`` asserts that
    arm is unreachable for every provider the tool modules actually name.
    """
    spec = get_provider_spec(provider)
    if spec is None:
        return None
    if not (set(field_names) & destination_fields_for(spec)):
        return None
    # Empty (searxng, which is a bare instance URL) is returned as-is rather
    # than collapsed to None: both are falsy so callers behave identically, and
    # keeping them apart means a caller can still tell "nothing to protect"
    # from "not a destination lookup".
    return credential_anchor_fields(spec)


def _destination_record_id(
    records: list[Any], anchor_fields: frozenset[str]
) -> tuple[Optional[str], frozenset[str]]:
    """The one record allowed to answer a destination lookup, and the anchor union.

    The union comes back with it so a refusal can say something TRUE about why.

    The first record holding any anchor field wins, and only if it holds EVERY
    anchor field held by any other candidate. Both halves are load-bearing, and
    each was a working bypass of the weaker rule they replace ("holds at least
    one anchor"):

    - Completeness. Contentful declares two independent tokens. A record naming
      only ``preview_token`` proved possession under the weaker rule, so it could
      steer the request that another record's ``delivery_token`` authenticated.
      36 of 187 providers have that multi-secret shape. Requiring the whole union
      means a record can only steer a secret it also supplies, and a planted
      record that names every anchor to satisfy this also wins those lookups, so
      it sends its own junk to its own address.
    - First. A system credential explicitly bound to the tool outranks an
      unbound user record, so without this a later user record could supply the
      address while the bound system record still supplied the key.

    Returning ``None`` here does NOT mean the caller may carry on. When a record
    that holds the requested address is refused, the caller raises rather than
    resolving, because its fallback chain ends in a hard-coded vendor host: see
    ``CredentialDestinationRefused``.

    Accepted behaviour change: an operator who deliberately SPLITS a provider's
    address and credential across two vault records must merge them. That split
    is precisely the attack shape, and ``native_credential_setup_hint`` already
    tells users to save one record carrying the required fields.
    """
    held = [
        (record, anchor_fields & set(record.secret_fields or ())) for record in records
    ]
    union = frozenset().union(*(names for _, names in held))
    holders = [(record, names) for record, names in held if names]
    if not holders:
        return None, union
    first, first_names = holders[0]
    if first_names != union:
        return None, union
    if any(record.provider != first.provider for record, _ in holders[1:]):
        # Different spellings of the provider are different CANDIDATE SETS, not
        # cosmetic. Ghost and PagerDuty resolve their address with the spec's
        # full alias list but each credential with one branch alias, so a record
        # saved under the other branch is visible to the address lookup and
        # invisible to the credential lookup. It could then satisfy this join by
        # merely NAMING a credential field it would never be asked for, and the
        # operator's record still answered. That was a working bypass. Whenever
        # the visible records disagree about the provider name, the join cannot
        # tell which of them a later lookup will reach, so it declines.
        return None, union
    return first.id, union


def get_native_credential_value(
    *,
    provider: str,
    field_names: Iterable[str],
    tool_name: str,
    config: Optional[RunnableConfig] = None,
    provider_aliases: Iterable[str] = (),
) -> Optional[NativeCredentialValue]:
    """Return the best matching credential-vault field for a native tool.

    Selection order favors explicit bindings, then explicit allowed target,
    then wildcard target. User-owned credentials are preferred over system
    credentials at the same target score.

    A credential with no allowed targets is NOT a candidate. It used to rank
    last but still win when nothing else matched, back when an empty list meant
    "any consumer may decrypt this". Empty now denies, so ranking it would only
    pick a credential the vault is about to refuse.

    A DESTINATION lookup (a base URL, host fragment or token endpoint) is only
    answered by the single record ``_destination_record_id`` picks. That is the
    whole of E10-02 slice B: the loop below re-runs per call and takes the first
    record holding any requested name, so without the join one record could
    supply the address while a different one supplied the secret that rides to
    it.

    Refusing RAISES ``CredentialDestinationRefused`` rather than returning None,
    and the difference is the whole point. Call sites spell their fallback
    ``_credential_value(...) or _settings_value(...) or VENDOR_DEFAULT``, so a
    silent None does not stop the request; it retargets it at the vendor's public
    API. For a self-hosted provider that means the operator's instance token
    goes to the vendor, which is the disclosure this control exists to prevent,
    caused by the control. Only a record that actually HELD one of the requested
    names raises; nothing saved is still a plain None, so the ordinary
    "no credential yet, use the default" path is untouched.
    """
    user_id = get_user_id(config)
    provider_names = _provider_candidates(provider, provider_aliases)
    field_list = list(dict.fromkeys(field_names))
    anchor_fields = _destination_anchor_fields(provider, field_list)
    refused_field = ""
    refused_partial = False
    anchor_union: frozenset[str] = frozenset()

    try:
        from ..core.credential_vault import (
            CredentialAccessDenied,
            CredentialSecretUnavailable,
            get_credential_vault_repo,
        )

        repo = get_credential_vault_repo()
        records = [
            record
            for record in repo.list_credentials(owner_user_id=user_id, include_system=True)
            if record.provider in provider_names and record.status == "active"
        ]
        bindings = [
            row
            for row in repo.list_bindings()
            if row.get("target_type") == NATIVE_TOOL_TARGET_TYPE
            and row.get("target_id") in {tool_name, "*"}
        ]
        bound_ids = {str(row["credential_id"]) for row in bindings}
        records.sort(
            key=lambda record: (
                _target_score(record, tool_name, bound_ids),
                0 if record.owner_type == "user" else 1,
                record.updated_at,
            )
        )
        eligible = [
            record for record in records if _target_score(record, tool_name, bound_ids) < 100
        ]
        destination_id, anchor_union = (
            _destination_record_id(eligible, anchor_fields)
            if anchor_fields
            else (None, frozenset())
        )
        for record in eligible:
            if anchor_fields and record.id != destination_id:
                # Would steer a request this record cannot prove it also
                # authenticates. Skip rather than abort: the record that carries
                # both may rank lower, which is the ordinary saved shape. Note
                # which field it held, so a refusal can be told apart from
                # nothing-saved once the search is exhausted.
                held = [name for name in field_list if name in (record.secret_fields or ())]
                if held and not refused_field:
                    refused_field = held[0]
                    refused_partial = bool(anchor_fields & set(record.secret_fields or ()))
                logger.debug(
                    "Credential %s may not supply a destination for %s; skipping",
                    record.id,
                    provider,
                )
                continue
            for field_name in field_list:
                if field_name not in record.secret_fields:
                    continue
                try:
                    value = repo.get_secret_field(
                        record.id,
                        field_name,
                        actor=user_id,
                        target_type=NATIVE_TOOL_TARGET_TYPE,
                        target_id=tool_name,
                    )
                except (CredentialAccessDenied, CredentialSecretUnavailable):
                    logger.debug(
                        "Credential %s was not usable for native tool %s",
                        record.id,
                        tool_name,
                        exc_info=True,
                    )
                    continue
                if value:
                    return NativeCredentialValue(
                        value=value,
                        credential_id=record.id,
                        field_name=field_name,
                    )
    except Exception:
        logger.debug("Native credential lookup failed for provider %s", provider, exc_info=True)

    if refused_field:
        if refused_partial:
            detail = (
                f"another saved {provider} credential holds fields it does not "
                f"({', '.join(sorted(anchor_union))}), so which record would authenticate "
                "the request is ambiguous. Keep one record per account, spelling the same "
                "field names in each"
            )
        else:
            detail = (
                f"it does not hold the {provider} credential itself, so it would steer a "
                "request something else authenticates. Add the credential to that same "
                "record"
            )
        raise CredentialDestinationRefused(
            f'A saved "{provider}" credential supplies "{refused_field}" but {detail}, or '
            f'remove "{refused_field}" from it and configure the address in settings instead.'
        )
    return None


def resolve_native_credential(
    *,
    provider: str,
    tool_name: str,
    settings_attr: str,
    config: Optional[RunnableConfig] = None,
    aliases: tuple[str, ...] = (),
    env_vars: tuple[str, ...] = (),
    field_names: Iterable[str] = ("api_key", "token", "value"),
    destination_label: str = "",
) -> Optional[str]:
    """Resolve a native-tool credential string: vault, then settings, then env.

    Tries the credential vault (``get_native_credential_value``), then the named
    ``Settings`` attribute, then each env var in ``env_vars`` order, returning the
    first truthy value (or None). ``field_names`` defaults to the standard API-key
    trio; pass a different tuple for non-key credentials (e.g. the SearXNG base
    URL). Replaces the per-provider ``_get_<provider>_api_key`` resolvers that each
    hand-rolled this vault -> settings -> env fallback.

    ``destination_label`` marks the resolved value as an ADDRESS, and screens it
    against the egress policy only when the VAULT answered. This function is the
    right place for that split because it is the only one that knows which leg
    won: a caller sees one string. The settings and env legs are operator
    configuration, and for the one provider that uses this today the documented
    value is an internal sidecar (``http://searxng:8080``), so screening those
    would delete the supported deployment. The vault leg is reachable by any
    identified caller over ``POST /credentials``. Same rule, same reasoning, as
    ``service_integration_base.signed_endpoint_url``.
    """
    try:
        cred = get_native_credential_value(
            provider=provider,
            provider_aliases=aliases,
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        )
    except CredentialDestinationRefused:
        # Unlike the inline call-site chains, this resolver's remaining legs are
        # BOTH operator configuration (a Settings attribute, then env vars) with
        # no vendor constant at the end, so a refused vault address can safely
        # fall through to them instead of failing the call.
        logger.debug("Refused a vault-supplied destination for %s; using settings", provider)
        cred = None
    if cred and cred.value:
        if not destination_label:
            return cred.value
        from ..core.http_policy import validate_http_egress_url

        try:
            return validate_http_egress_url(str(cred.value).strip(), label=destination_label)
        except ValueError as exc:
            # Falls through for the same reason the refusal arm above does, and
            # it has to: raising here would turn a planted record into a DENIAL
            # OF SERVICE. Any identified caller can POST a `searxng` credential
            # holding `http://127.0.0.1`, and a raise would then break the tool
            # for every user of the deployment even though the operator's own
            # `SEARXNG_BASE_URL` is configured and reachable. Refusing the
            # address and carrying on makes the planted record inert, which is
            # the outcome the operator's standing rule asks for.
            logger.warning(
                "Refused a vault-supplied %s for %s (%s); using operator configuration instead",
                destination_label,
                provider,
                exc,
            )

    from ..config import get_settings

    # Left-fold of ``or`` reproduces ``settings.X or env(A) or env(B)`` exactly,
    # including the empty-string edge (the original returns the last ``.get()``
    # verbatim when nothing is truthy).
    value = getattr(get_settings(), settings_attr)
    for env_var in env_vars:
        value = value or os.environ.get(env_var)
    return value


def native_credential_setup_hint(
    *,
    provider: str,
    field_names: Iterable[str],
    tool_name: str,
    env_var: str = "",
    display_name: str = "",
) -> str:
    label = display_name or provider
    fields = ", ".join(f'"{field}"' for field in field_names)
    target = f"{NATIVE_TOOL_TARGET_TYPE}:{tool_name}"
    text = (
        f"[Error]: No {label} credential found. Save one in Settings > Connections "
        f'with provider "{provider}", required field(s) {fields}, and allowed target "{target}" '
        f'or "native_tool:*".'
    )
    if env_var:
        text += f" Env fallback: {env_var}."
    return text
