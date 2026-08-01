"""Shared foundation helpers for the ``*_service_integrations`` tool modules.

Each ``<area>_service_integrations.py`` module wraps a family of third-party HTTP
APIs as ``@tool`` functions. Historically every module re-declared the same small
foundation layer (JSON dumping, value filtering, base-URL validation, credential
and settings resolution, setup hints). Those copies were byte-identical yet
maintained independently, so a single fix had to be applied ~29 times and the
copies had begun to drift.

This module is the canonical home for the stable, behavior-identical helpers. Each
integration module imports them under its existing private name so the per-module
monkeypatch test seam is preserved, e.g.::

    from .service_integration_base import (
        credential_value as _credential_value,
        dump_json,
    )

The aliasing is mandatory: the integration tests do
``monkeypatch.setattr(<module>, "_credential_value", ...)`` and the tool bodies
call the bare name ``_credential_value(...)``, which resolves the module global at
call time and picks up the patch. Tool bodies must therefore keep calling the bare
aliased names rather than ``service_integration_base.credential_value`` directly.

Deliberately NOT centralized here yet (the copies genuinely diverge in
agent-visible behavior, so consolidating them is a behavior decision, not a pure
refactor):

* ``_request_json`` / ``_request_any``: the per-module copies differ in
  error-detail key precedence, non-JSON-200 fallback (raise vs return text), and
  form encoding (``commerce_billing`` flattens nested form fields). Folding them
  into one superset would change runtime error strings and fallback shapes.
* ``_csv_to_list``: some copies are newline-tolerant and some are not.

``dump_json`` and ``clamp_limit`` carry a per-module parameter (the
``_MAX_JSON_CHARS`` budget, the ``default``/``max_value`` clamp bounds), so each
module keeps a thin local ``_dump_json`` / ``_limit`` wrapper that binds its own
value and delegates here, rather than aliasing the shared helper directly.

A few modules also keep a LOCAL copy of an otherwise-shared helper because their
copy has drifted in an agent-visible way (so the canonical helper here is not a
drop-in for them):

* ``filtered`` vs local ``_filtered_params``: ``business`` and ``work_tracking``
  drop ``None`` / ``""`` / ``[]`` but keep empty dicts ``{}``, where this
  ``filtered`` also drops ``{}``. Switching them would change request payloads.
* ``json_object`` vs local ``_json_object``: ``business`` and ``event_meeting``
  add an ``allow_empty`` parameter and a richer error string;
  ``transform_utility`` raises on whitespace-only input where this helper returns
  ``{}``.
* ``dump_json``: ``transform_utility``'s local ``_dump_json`` is non-truncating,
  so it does not import this (truncating) version.

These are tracked as follow-ups in the slice 13 and 14 optimization reports.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig

if TYPE_CHECKING:  # import cost stays at zero; see the lazy-init note in CLAUDE.md
    import httpx

# Default truncation budget for tool output. Modules that legitimately need a
# larger budget (bulk row dumps, log payloads) keep an explicit module-local
# override and pass it through ``dump_json(..., max_chars=...)``.
DEFAULT_MAX_JSON_CHARS = 60_000


def dump_json(data: Any, *, max_chars: int = DEFAULT_MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def filtered(values: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (values or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def clamp_limit(value: int, *, default: int = 50, max_value: int = 200) -> int:
    """Clamp a user-supplied row/page limit into ``[1, max_value]``.

    The shared clamp body for the ``*_service_integrations`` corpus (slices
    13/14/15). Each module keeps a thin local ``_limit`` wrapper that binds its
    own ``default``/``max_value`` and delegates here, the same pattern
    ``_dump_json`` uses for its ``_MAX_JSON_CHARS`` budget. Non-int input falls
    back to ``default`` (the clamp deliberately swallows coercion errors,
    matching the prior per-module behavior). The base ``default``/``max_value``
    are ergonomic fallbacks only; every corpus wrapper passes both explicitly.
    """
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    from ..core.http_policy import validate_http_egress_url

    return validate_http_egress_url(value.strip().rstrip("/"), label="base URL", resolve_dns=False)


def request_with_policy(
    client: "httpx.Client", method: str, url: str, **request_kwargs: Any
) -> "httpx.Response":
    """Issue one integration request under the HTTP egress policy.

    The corpus-wide replacement for a bare ``client.request(method, url, ...)``.
    ``base_url()`` is a cheap PERSIST-shaped screen: it runs with
    ``resolve_dns=False``, so it rejects a literal private/loopback/metadata IP
    but lets a HOSTNAME through no matter where it resolves. This is the
    EGRESS-time half: ``httpx_request_with_policy`` re-evaluates the concrete
    target with DNS resolution on, and holds ``pinned_dns_resolution`` across
    the send so the address policy approved is the address the socket connects
    to. Without the pin the two lookups are independent and a short-TTL record
    can flip between them.

    ``follow_redirects=False`` is not a policy choice, it is the httpx default
    every caller here already had: the redirect legs exist for the callers that
    opt in. Chasing a 3xx would re-aim a request that already carries the
    credential, so preserving it matters twice.

    The two policy exceptions are translated to one ``RuntimeError`` so the 32
    modules and 36 call sites that use this need no new ``except`` clause of
    their own. Thirty of the 33 integration modules end their tool bodies in
    ``except Exception``; in the other three (``relationship_crm``,
    ``personal_device``, ``time_hr``, 81 tools between them) the RuntimeError
    reaches ``SafeToolNode``, which renders it as a tool result because
    ``handle_tool_errors=True``. Either way the agent gets a string, and either
    way that path was already live: these helpers raise ``RuntimeError`` on any
    4xx/5xx today.
    """
    from ..core.http_policy import (
        HTTPPolicyRedirectLimit,
        HTTPPolicyViolation,
        httpx_request_with_policy,
    )

    try:
        response, _redirect_chain, _decision = httpx_request_with_policy(
            method,
            url,
            client=client,
            follow_redirects=False,
            **request_kwargs,
        )
    except HTTPPolicyRedirectLimit as exc:
        raise RuntimeError(f"HTTP request blocked by egress policy: {exc}") from exc
    except HTTPPolicyViolation as exc:
        # Names the remedy, because the commonest way to meet this message is not
        # an attack: it is a self-hosted instance (Home Assistant, GitLab, Jira
        # Server, Jenkins, Nextcloud, Grafana), including several that ship an
        # internal address as their own built-in default. The capability change
        # is documented, but the runtime string is the only place the operator
        # actually looks.
        raise RuntimeError(
            f"HTTP request blocked by egress policy: {exc}. If this is your own "
            "self-hosted instance, add its host (or host:port) to "
            "HTTP_INTERNAL_ALLOWLIST."
        ) from exc
    return response


def signed_endpoint_url(
    *,
    from_vault: Optional[str],
    from_settings: Optional[str] = None,
    keys_from_vault: bool,
    label: str,
) -> Optional[str]:
    """Resolve an endpoint override for a request signed with separate keys.

    The two boto3 call sites (``aws``, ``file_storage``) reach neither
    ``base_url()`` nor ``request_with_policy``, because botocore has its own HTTP
    stack. So this is the only screen those addresses get, which is why it
    resolves DNS rather than running the cheap literal-IP check the parse-time
    screen runs.

    ``region`` at the same two sites is classified a destination too, and is
    deliberately left alone: botocore interpolates it into
    ``s3.{region}.amazonaws.com``, so a planted value cannot leave
    ``amazonaws.com`` and there is nowhere internal for it to go.

    ``keys_from_vault`` is the slice B join spelled locally. Slice B guarantees
    a record supplying an address holds SOME anchor field for the provider, not
    the specific one a given call site asks for. Here the anchors are
    ``secret_access_key``/``session_token`` while ``access_key_id`` is a
    non-proof name, so a record holding ``endpoint_url`` + ``session_token``
    clears slice B, both key lookups then miss the vault, and boto3 SigV4-signs
    the operator's env-configured key pair to an address it does not own. The
    general form of that gap is filed as E10-02-D; these two sites are where it
    is concrete enough, and cheap enough, to close now.

    The settings leg is deliberately NOT screened. That is not "operator
    configuration is safe by fiat": it is that here the provenance is free, so
    the control can apply exactly where the risk is. ``S3_ENDPOINT_URL=
    http://minio:9000`` is an ordinary compose deployment, and refusing it would
    cost real capability for very little. The qualifier matters: no native tool
    writes settings, but ``s3_endpoint_url`` IS in the ``PATCH /settings`` schema
    and ``nymeria_update_settings`` exposes that route over MCP, so on a
    deployment where an ADMIN's thread mounts Nymeria's own MCP server the value
    is reachable. That is the same line the workflow gate draws (an admin's own
    agent is not the threat the gate is for), and it is recorded in SECURITY.md
    rather than papered over. The 264 ``base_url()`` sites have no such choice
    either way: they cannot tell which leg produced the address, so they screen
    the concrete target unconditionally.
    """
    if not from_vault:
        return str(from_settings).strip() if from_settings else None
    if not keys_from_vault:
        from .native_credentials import CredentialDestinationRefused

        raise CredentialDestinationRefused(
            f"A saved credential supplies {label.lower()} but not the key pair signing the "
            "request, so it would steer calls made with a key it does not hold. Add the "
            f"access key id and secret access key to that same record, or remove the "
            f"{label.lower()} from it."
        )
    from ..core.http_policy import validate_http_egress_url

    return validate_http_egress_url(str(from_vault).strip(), label=label)


def require_joined_destination(
    *,
    destination_from_vault: Optional[str],
    secret_from_vault: Optional[str],
    secret: Optional[str],
    provider: str,
) -> None:
    """Refuse a request whose address came from the vault and whose secret did not.

    This is E10-02-D, the settings/env leg. Slice B
    (``native_credentials._destination_record_id``) guarantees that a record
    supplying an address holds SOME anchor field of the provider's. It does not
    guarantee the record holds the one a given call site asks for, and every
    config helper here spells its fallback ``_credential_value(...) or
    _settings_value(...)``. So a record holding ``base_url`` plus one anchor from
    a DIFFERENT anchor group clears slice B, the secret lookup then misses the
    vault, and the operator's environment-configured key rides to an address that
    record chose. 36 of the 171 destination-bearing specs declare two or more
    independent anchor groups and are shaped for it.

    The rule is the one cluster A already ships for LLM destinations
    (``core/llm_provider_utils.destination_redirects_away_from_config``): a
    caller-named destination does not get the server's credential. Ownership is
    judged by PROVENANCE, not presence.

    ACCEPTED CAPABILITY COST, and it is not zero. One legitimate shape is
    refused: a single record holding an address plus a complete credential for
    one auth branch, while a STALE environment variable still holds a credential
    for an EARLIER branch. The earlier branch wins the fixed precedence, resolves
    from settings, and is refused here. Measured on elasticsearch (record with
    ``base_url`` + ``username`` + ``password``, plus a leftover
    ``ELASTICSEARCH_API_KEY``), and reproduced on thirteen more providers. The
    refusal is correct in the sense that it IS the leak shape when the record is
    planted rather than the operator's, and it cannot be told apart from inside
    the vault. The message below therefore names the stale setting as a remedy;
    it is the one people will actually be hitting. The cost-free version is to
    order the auth branches by provenance so the vault-supplied credential's
    branch wins, which is task #65.

    Three cases deliberately pass:

    * ``destination_from_vault`` empty. The address is a setting or the vendor
      default, so nothing caller-supplied is steering anything.
    * ``secret_from_vault`` set. The secret came from the vault rather than from
      server configuration. Note what this is NOT: it is a provenance bit, not a
      record identity, so on its own it does not say the two came from the SAME
      record. What makes it mean that is slice B, which only lets the first
      record holding every anchor any candidate holds serve an address, judged by
      VALUE. While that was judged by NAME, a record could name an anchor with an
      empty value, take the address, and let a different record answer the
      secret, so this bit was true and the leak still happened THROUGH the
      control. ``native_credentials._destination_record_id`` and
      ``test_an_empty_anchor_field_is_not_possession`` carry the other half of
      this note. Do not weaken either without revisiting this line.
    * ``secret`` empty. Nothing resolved, so there is nothing to disclose; the
      caller's existing "no credential yet" branch returns its setup hint.

    That third case is why this must not be spelled as "raise when the vault
    supplied an address and the secret lookup missed". 26 specs spell
    ``_credential_value(A) or _credential_value(B) or _settings_value(...)``
    (mailchimp access_token or api_key, pagerduty access_token or api_token), and
    a legitimate first miss is indistinguishable from the leak AT THAT CALL. It
    is only distinguishable once the whole chain has resolved, which is why this
    takes the settled values rather than screening each lookup.

    ``signed_endpoint_url`` is the same rule spelled for the two boto3 sites, and
    is deliberately NOT folded in here: it refuses a vault address whenever the
    keys did not come from the vault, without regard to whether keys resolved at
    all. That is stricter than this, harmless there (botocore cannot sign
    without a key pair anyway), and merging the two would change its behaviour.

    A caller may REPORT this but must not RECOVER from it. The distinction
    matters because almost every tool body here wraps its work in
    ``except Exception`` and returns ``"[Error]: ..."``, which is fine: the
    request never leaves and the refusal reaches the agent as text. What is not
    fine is catching it and carrying on, because every fallback chain here ends
    in a hard-coded vendor host, so continuing would retarget the request at the
    vendor's public API carrying a self-hosted instance's token, which is the
    disclosure the control exists to prevent. An earlier wording of this
    paragraph said "callers must not catch this", which the shipped code already
    contradicted.
    ``tests/test_service_integration_egress.py`` holds the AST gate that fails
    the build when a helper resolves a vault destination and a settings-backed
    secret without calling this.
    """
    if not destination_from_vault or secret_from_vault or not secret:
        return
    from .native_credentials import CredentialDestinationRefused

    # Three remedies, and the order matters: the LAST one is the common case for
    # an operator rather than an attacker, and an earlier version of this message
    # named only the first two. It then told someone whose record already held a
    # complete credential to "add the credential to that same record", which they
    # had done, while the actual cause (a leftover environment variable feeding
    # an earlier auth branch) went unmentioned.
    raise CredentialDestinationRefused(
        f'A saved "{provider}" credential supplies the address for this request but the '
        f"{provider} credential about to be sent there came from server configuration, "
        "not from that record, so the request would go to an address one record chose "
        "authenticated by something else. Add that credential to the same record, or "
        "remove the address from the record and configure the address in settings, or, "
        f"if the record already holds a working {provider} credential of a different "
        "kind, clear the stale environment variable or setting holding the one named "
        "here so the record's own credential is the one that resolves."
    )


# ``\Z``, not ``$``. Python's ``$`` also matches immediately BEFORE a trailing
# newline, and ``.strip()`` runs before ``.rstrip("/")`` below, so a trailing
# slash shields the newline from the strip: ``"evil.com\n/"`` reached a
# validated result carrying a character this charset is meant to exclude. It
# was not exploitable (urlsplit deletes the newline and httpx refuses to build
# the URL at all), but the whole argument for a positive charset is that no
# character survives which the three parsers could read differently, and that
# one falsified it.
_HOST_LABELS = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*\Z"
)


def vendor_host(
    value: Optional[str], *, vendor_suffix: str, provider: str, field: str
) -> str:
    """Build a complete vendor host from a credential-supplied FRAGMENT.

    Returns the WHOLE host (``"acme.chargebee.com"``), not the bare label, and
    that is the load-bearing part of the signature rather than a convenience.
    Returning the label would leave the dangerous
    ``f"https://{x}.vendor.com"`` template at every call site, making safety a
    property of the PREVIOUS LINE and forcing any ratchet to recognise vendor
    domains from a list. Owning the suffix here means no URL template in the
    corpus contains a vendor domain at all, so the gate is one rule with no
    vendor knowledge in it: anything interpolated into an authority position
    must be a call to this function.

    Many integrations build their address as ``f"https://{fragment}.vendor.com"``
    where ``fragment`` is a tenant name from a vault record or a setting. That
    template is making a promise, that the request lands somewhere under
    ``vendor.com``, and a bare interpolation does not keep it: the fragment can
    terminate the authority and push the vendor suffix into the path, the query
    or the URL fragment, leaving an attacker's host as the netloc. Measured, on
    the exact shapes in this tree:

        f"https://{instance}.service-now.com/api/now"  instance="evil.com/x#"
            -> netloc "evil.com", the vendor suffix now a discarded fragment
        f"https://{subdomain}.zendesk.com/api/v2"      subdomain="evil.com?"
        f"https://{prefix}.api.mailchimp.com/3.0"      prefix="evil.com/"
        f"https://{app_name}.bubbleapps.io"            app_name="evil.com:443#"

    So the escape set is the AUTHORITY TERMINATORS ``/``, ``?``, ``#`` and ``:``,
    not ``#`` alone.

    It refuses on a positive charset rather than normalising with ``urlparse``,
    and the reason is stronger than "a blacklist might miss a fifth delimiter". A
    parser-based normaliser ACCEPTS AND SILENTLY REWRITES, after which urlparse,
    httpx and DNS no longer agree about what it produced. Measured through the
    normalising path: ``"evil.com\\ty"`` has the tab deleted and requests
    ``evil.comy``; ``"evil .com"`` is percent-encoded to ``evil%20.com``;
    ``"acme。evil.com"`` is UTS-46 mapped by httpx into a real dot, so one
    label becomes two; ``"a@evil.com"`` silently introduces userinfo. None of
    those escape the vendor, but in each the string that was validated is not the
    host the socket got. ``[A-Za-z0-9.-]`` admits no character those three
    parsers can disagree about, so it DELETES that differential class instead of
    surviving it.

    Dots ARE allowed, deliberately. ``instance="foo.bar"`` yields
    ``foo.bar.service-now.com``, which is a genuine subdomain of the vendor and
    therefore still inside the promise; refusing it would break legitimate
    multi-label tenants for no gain. By the same reasoning ``foo@evil.com`` and
    ``evil.com\\`` were measured NOT to be escapes and are simply rejected here
    as non-label characters rather than treated as a special case.

    A leading ``http://`` or ``https://`` and the vendor suffix are both stripped
    before validation, preserving the convenience the call sites already had (a
    user may paste a whole URL), and both are matched case-insensitively because
    DNS is. Stripping the scheme is safe rather than lenient: anything left over
    still has to be labels, so ``https://evil.com/x`` is refused for its slash
    and ``https://evil.com`` becomes ``evil.com.vendor.com``, a vendor subdomain.
    Three different tolerances for the same user paste used to exist across this
    corpus; this is now the only one.

    WHAT THIS DOES NOT DO, and it is the bigger half. Keeping the request inside
    the vendor's domain does not keep it inside the OPERATOR's tenant: a planted
    fragment still selects another customer of the same vendor. That is a
    provenance question, not a syntax one, and the answer is
    ``require_joined_destination``, which refuses when a record supplies the
    address and did not supply the secret. This function and that one are both
    needed and neither substitutes for the other.
    """
    from .native_credentials import CredentialDestinationRefused

    # The SUFFIX is checked too, and it is not defensive programming. At one
    # site it is DATA: erpnext composes its host from a vault-supplied
    # subdomain and a `cloud_domain` that defaults to "erpnext.com", so an
    # unchecked suffix would let the second half do exactly what this function
    # stops the first half doing. It also turns three silent own-goals into a
    # test failure the moment anyone writes them, since a caller passing "",
    # "okta.com" (no dot) or "@evil.com" would otherwise get back
    # "attacker.example.net", "evil-corpokta.com" and "acme@evil.com" from a
    # function whose whole promise is that the result stays under the vendor.
    if not vendor_suffix.startswith(".") or not _HOST_LABELS.match(vendor_suffix[1:]):
        raise CredentialDestinationRefused(
            f'Cannot build the "{provider}" address: the vendor domain '
            f"{vendor_suffix!r} is not a dot followed by plain host labels, so "
            "there is no domain to keep the request inside. If it came from "
            "configuration, correct it there."
        )

    text = (value or "").strip()
    for scheme in ("https://", "http://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme) :]
            break
    text = text.rstrip("/")
    if vendor_suffix and text.lower().endswith(vendor_suffix.lower()):
        text = text[: -len(vendor_suffix)]
    if not text or not _HOST_LABELS.match(text):
        # The received value is deliberately NOT echoed. At one site
        # (customer_engagement's mailchimp) the fragment can be derived from the
        # API key itself, ``api_key.rsplit("-", 1)[-1]``, so echoing a malformed
        # one would put a slice of the credential into a tool-visible string, in
        # the one corpus whose whole subject is credentials not going where they
        # should not.
        # "configured or saved", not "saved": every call site resolves this as
        # ``_credential_value(...) or _settings_value(...)``, so the offending
        # value is as likely to be the operator's own env var (SHOPIFY_SHOP,
        # ZENDESK_SUBDOMAIN, SERVICENOW_INSTANCE) as a vault record. The other
        # two users of this exception fire only on vault-supplied addresses, so
        # the class name carries a provenance it does not have here, and telling
        # an operator their "saved" value is wrong when they never saved one
        # sends them looking in the wrong place.
        raise CredentialDestinationRefused(
            f'The configured or saved "{provider}" {field} is not usable. It is '
            f"interpolated into {provider}'s own hostname, so a value carrying "
            '"/", "?", "#" or ":" would end the hostname early and move the '
            f'request off "{vendor_suffix.lstrip(".") or provider}" entirely. '
            f'Give the tenant label on its own ("acme"), the full host '
            f'("acme{vendor_suffix}"), or that same host with an "https://" '
            "prefix. All three are accepted."
            # The examples deliberately do not spell a whole URL as one
            # interpolated string. A placeholder sitting directly after a
            # scheme is exactly the shape the build gate looks for, and prose
            # is syntactically indistinguishable from a request target, so
            # writing it that way would have made the helper's own error
            # message the one thing its gate reported.
        )
    return f"{text}{vendor_suffix}"


def json_object(value: str, *, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def json_array_or_object(value: str, *, field_name: str) -> list[dict[str, Any]]:
    if not value or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    records = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{field_name} must be a JSON object or array of objects")
    return records


def parse_json(value: str, *, expected: type, label: str) -> Any:
    """Parse ``value`` as JSON, requiring it to be of type ``expected``.

    The generalized JSON-argument parser shared across the ``*_service_integrations``
    corpus (slices 13/14/15). Empty/whitespace input yields the empty ``expected``
    instance (``{}`` for ``dict``, ``[]`` for ``list``); ``expected`` is only ever
    ``dict`` or ``list`` at the call sites. Distinct from ``json_object`` /
    ``json_array_or_object`` (which carry their own message wording and a
    fixed-shape contract); those stay separate.
    """
    if not value.strip():
        return {} if expected is dict else []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def basic_auth(username: str, password: str = "") -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def settings_value(name: str) -> Optional[str]:
    from ..config import get_settings

    return getattr(get_settings(), name)


# Common credential field-name lookup tuples, shared by the
# ``*_service_integrations`` modules. ``credential_value()`` / ``setup_hint()``
# forward these to ``native_credentials``, which tries them IN ORDER and takes the
# first matching field (and renders them in order in the setup-hint text), so the
# ORDER of each tuple is behaviorally significant. A constant may only stand in for
# a call site whose literal tuple is byte-identical (same elements, same order).
# ``*_FIELDS`` holds the common field name(s); the matching ``*_ALIAS_FIELDS`` adds the
# camelCase / alternate spellings. Order-variants (e.g. a base-url tuple with ``"url"``
# before ``"api_url"``) and provider-specific unions are deliberately kept inline.
API_KEY_FIELDS = ("api_key", "value")
API_KEY_ALIAS_FIELDS = ("api_key", "apiKey", "token", "value")
BASE_URL_FIELDS = ("base_url", "url")
BASE_URL_ALIAS_FIELDS = ("base_url", "baseUrl", "api_url", "apiUrl", "url")
USERNAME_FIELDS = ("username", "user", "login")


def credential_value(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    config: Optional[RunnableConfig],
    provider_aliases: tuple[str, ...] = (),
) -> Optional[str]:
    from .native_credentials import get_native_credential_value

    credential = get_native_credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    return credential.value if credential else None


def setup_hint(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    env_var: str,
    display_name: str,
) -> str:
    from .native_credentials import native_credential_setup_hint

    return native_credential_setup_hint(
        provider=provider,
        field_names=field_names,
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )
