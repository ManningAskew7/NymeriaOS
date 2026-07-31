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
