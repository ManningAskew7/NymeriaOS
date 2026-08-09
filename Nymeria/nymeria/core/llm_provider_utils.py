"""Shared utilities for LLM provider probing, destinations, and metadata.

Three groups live here, all of them things more than one layer needs and none
of them owning a provider's state: HTTP probe helpers (headers, error
extraction, metadata parsing), the CLIProxy base-URL derivations, and the
outbound-destination predicate that decides whether a caller-supplied base URL
points anywhere configuration would not have. The predicate sits in this
neutral module rather than beside either of its callers because both the
admin-facing settings route and the per-thread config resolver have to answer
the same question, and a copy in either one would drift from the other.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config.llm_providers import (
    cliproxy_base_url_for_provider,
    get_llm_provider_spec,
    is_google_native_provider,
    normalize_llm_provider,
    resolve_provider_base_url,
)
from ..config.local_llm import is_local_llm_base_url

logger = logging.getLogger(__name__)

# Anthropic Messages API version header value. Shared by the provider-test probe
# and the live model-listing probe so they advertise the same wire version.
ANTHROPIC_API_VERSION = "2023-06-01"

# OpenRouter courtesy attribution headers (their dashboard ranks apps by these).
# Canonical copy; reused by core/llm_provider_test_suite.py.
OPENROUTER_ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
    "X-Title": "Nymeria",
}


def cliproxy_base_url_with_v1(base_url: str) -> str:
    """Append the ``/v1`` suffix CLIProxy's OpenAI-compatible surface needs.

    CLIProxy serves its OpenAI-compatible API under ``/v1``; a base URL that
    points at the proxy root without it makes every ``/models`` and
    ``/chat/completions`` request 404. Non-CLIProxy URLs, and ones already
    ending in ``/v1``, are returned rstripped but otherwise unchanged.

    This is the OpenAI arm of :func:`cliproxy_base_url_for_provider` with a
    passthrough for the non-CLIProxy case, expressed that way rather than
    open-coded so that function stays the single derivation it claims to be.
    """
    return cliproxy_base_url_for_provider("openai", base_url) or base_url.rstrip("/")


_LOOPBACK_SPELLINGS = frozenset({"localhost", "127.0.0.1", "::1"})


def _canonical_loopback(host: str) -> str:
    """Collapse every spelling of the local machine onto one host token.

    Exactly three spellings, because exactly three are needed: ``localhost``,
    ``127.0.0.1`` and ``::1``. Deliberately NOT the rest of ``127.0.0.0/8``,
    even though it is all loopback by definition. Collapsing the whole block
    would be a strict widening of a credential-egress gate for no flow that
    exists: a deployment configured as ``localhost`` and a client offering
    ``127.0.0.1`` is the entire problem being solved, whereas admitting
    ``127.0.0.2`` lets anything able to bind a spare loopback address stand up
    a listener on a port configuration named and be handed the key. Not
    ``0.0.0.0`` either, which is a bind address rather than a destination.

    Names that merely RESOLVE to the loopback are not collapsed, because
    deciding that needs DNS and this predicate must not do lookups. Nor are
    the exotic-but-valid dialled forms (``127.1``, ``2130706433``,
    ``0177.0.0.1``, ``::ffff:127.0.0.1``): they are refused rather than
    admitted, which is the safe direction, and spelling any of them is a
    choice nobody makes by accident.

    Deliberately NOT ``config.local_llm.LOCAL_MODEL_HOSTS``, which is the same
    subject calibrated the opposite way. That set is broad on purpose (private
    LAN, Tailscale CGNAT, ``.docker.internal``) because it answers "may we skip
    the key, may we probe"; this one is narrow because it answers "is this the
    same machine configuration named", and a wrong yes hands over a credential.
    ``host.docker.internal`` is the case that proves they must stay apart: it
    belongs in that set and would be actively wrong here, since in the Docker
    shape it names the HOST rather than the container. Widening this to reuse
    that set would silently widen a credential-egress gate.
    """
    return "localhost" if host in _LOOPBACK_SPELLINGS else host


def base_url_destination_key(value: str | None) -> str | None:
    """``scheme://host[:port]`` for a base URL, or None if there isn't one.

    Compared rather than the raw string so a trailing slash, a ``/v1`` suffix,
    a path, a query and an explicit default port do not read as different
    destinations. What IS in the key is deliberate:

    - the scheme, because ``http://`` to a host configured as ``https://`` is
      a downgrade that puts the credential on the wire in clear text;
    - the port when it is not the scheme default, because a different port is
      a different server (on the reference deployment the CLIProxy sidecar and
      the API itself differ only by port).

    Userinfo is dropped by ``urlparse`` before ``hostname``, which is the safe
    direction: ``http://real.host@evil.example`` keys as ``evil.example``, the
    host an HTTP client would actually dial.

    Loopback spellings collapse to one token. That is host equality being
    CORRECT, not an exemption: ``127.0.0.1``, ``::1`` and ``localhost`` are the
    same machine by definition, so treating them as three destinations refused
    threads that named the very server configuration already pointed at (a
    deployment configured as ``localhost:8318`` while the client offers
    ``127.0.0.1``; Ollama registered at ``localhost`` while its own docs say
    ``127.0.0.1``). Loopback as such is still gated: naming a loopback port
    configuration does NOT name is still a redirection, which is what stops the
    field becoming credentialed port scanning.
    """
    clean = (value or "").strip()
    if not clean:
        return None
    try:
        parsed = urlparse(clean if "://" in clean else f"http://{clean}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    host = _canonical_loopback(host)
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = (parsed.scheme or "http").lower()
    # Re-bracket an IPv6 literal. `urlparse` strips the brackets off `.hostname`,
    # and this key is fed BACK through this function by
    # `destination_redirects_away_from_config`, which re-keys whatever the
    # calling subsystem hands it. Unbracketed, `http://2001:db8::1:8317` reparses
    # with a `.port` that raises, the key becomes None, EVERY configured entry is
    # dropped, and the gate then refuses the operator's own address. The key has
    # to be idempotent, not merely well-formed. Bracketing also keeps
    # `[2001:db8::1]:8317` distinct from `[2001:db8::1:8317]`, which are two
    # hosts an HTTP client dials differently but which collapsed to one key.
    if ":" in host:
        host = f"[{host}]"
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def configured_llm_destinations(
    provider: str | None,
    *,
    provider_route: Any = None,
    settings: Any = None,
) -> set[str]:
    """Where configuration would already send this LLM *provider*.

    The LLM-shaped half of the gate. It builds the set;
    :func:`destination_redirects_away_from_config` compares against it and
    knows nothing about providers, so the other consumption points in this
    audit cluster (the embedding client, and the voice ``tts_base_url`` /
    ``stt_base_url`` pair) can pass their own configured URLs instead of
    copying the predicate. A copy is what the shared home exists to prevent.

    Derived from what the operator has ALREADY set, deliberately not from a
    list anybody maintains. An allowlist was the first design and it earned
    nothing: the providers with no canonical host (Azure, Vertex, Bedrock,
    Databricks, Cloudflare Gateway) genuinely ARE naming a new destination when
    overridden per thread, so they should bring their own key rather than be
    listed, and every other provider is already covered by what configuration
    says. Nothing here exempts a destination for being local; see
    :func:`destination_redirects_away_from_config`, which explains why.

    The vendor's canonical host counts only when configuration names no
    destination FOR THIS PROVIDER. That condition is load-bearing rather than
    tidiness: on a proxied deployment the provider env keys hold PROXY-LOCAL
    secrets (``cpx-*``), so admitting ``api.openai.com`` because the registry
    knows it as OpenAI's address would mail a third-party-issued secret to
    OpenAI Inc. That is the same mistake ``anthropic_probe_base_url`` records
    in ``vendor/react_agent/providers.py``, and the reason its test asserts
    zero egress rather than zero result.

    Per provider, not globally, and that word is the whole difference between
    a control and an annoyance. ``LLM_BASE_URL`` SUPPRESSES the canonical host
    of the global provider only; letting it also suppress OpenRouter's would
    refuse a thread pointed at the operator's own documented backup provider,
    whose key is a real OpenRouter key going to OpenRouter. It is still ADMITTED
    for every provider, which is the deliberate asymmetry: the operator's own
    address is inside the trust boundary whoever is talking to it, and only the
    question "may the vendor's host be assumed too" is provider-specific.
    """
    allowed = {
        key
        for key in (
            base_url_destination_key(getattr(settings, field, None))
            for field in ("llm_base_url", "llm_background_base_url")
        )
        if key
    }
    # Addresses configuration names FOR THIS PROVIDER, as opposed to for the
    # deployment in general. Two spellings, one meaning, so they get one name:
    # a provider's own configured base URL, and the CLIProxy derivation, since
    # one proxy container fronts both the Anthropic and the OpenAI path and a
    # global base URL pointing at it therefore names an address for each of
    # them rather than only for whichever is global.
    #
    # The RESTRICTIVE direction is what this buys, and it is the whole reason
    # the CLIProxy arm exists: without it `api.anthropic.com` was admitted on
    # the reference deployment purely as the vendor's canonical host, and the
    # key that would have ridden there is the proxy-local `cpx-*` secret. That
    # is the third-party-key-to-the-vendor mis-send this function's docstring
    # says it prevents, and it showed as an asymmetry, since `api.openai.com`
    # WAS refused on the same box. It adds nothing in the permissive direction:
    # the derivation only ever adds or strips a `/v1` path and the key drops
    # paths, so its key already equals the `llm_base_url` key gathered above.
    provider_named = {
        key
        for key in (
            base_url_destination_key(
                _provider_base_url(provider, provider_route, settings, include_default=False)
            ),
            base_url_destination_key(
                cliproxy_base_url_for_provider(
                    provider, getattr(settings, "llm_base_url", None)
                )
            ),
        )
        if key
    }
    allowed |= provider_named

    # The third shape: a global base URL speaks for the global provider even
    # when nothing above resolved an address for it by name. Raw truthiness
    # rather than a parsed key, so a set-but-unparseable `LLM_BASE_URL` still
    # suppresses the vendor host instead of falling through to it.
    global_provider = normalize_llm_provider(getattr(settings, "llm_provider", None))
    named_by_global = bool(getattr(settings, "llm_base_url", None)) and (
        normalize_llm_provider(provider) == global_provider
    )
    if provider_named or named_by_global:
        return allowed

    candidates = [_provider_base_url(provider, provider_route, settings, include_default=True)]
    try:
        spec = get_llm_provider_spec(provider)
    except Exception:  # noqa: BLE001 - a registry miss must not fail the turn
        # Fail CLOSED but QUIETLY: no spec means no extra allowed destinations,
        # so an unknown provider refuses rather than admits. Letting this raise
        # would take the whole turn down, because the caller treats the gate as
        # a predicate rather than as something that can fail.
        logger.debug("Provider spec lookup failed for %s", provider, exc_info=True)
        spec = None
    if spec is not None:
        candidates += [
            spec.default_base_url,
            spec.openai_compat_base_url,
            spec.anthropic_messages_base_url,
        ]
    return allowed | {key for key in (base_url_destination_key(v) for v in candidates) if key}


def _provider_base_url(
    provider: str | None, provider_route: Any, settings: Any, *, include_default: bool
) -> str | None:
    try:
        return resolve_provider_base_url(
            provider,
            provider_route=provider_route,
            settings=settings,
            include_default=include_default,
        )
    except Exception:  # noqa: BLE001 - a registry miss must not fail the turn
        # None means "cannot say", and the caller reads that as "configuration
        # names nothing for this provider", which is the PERMISSIVE branch: it
        # goes on to admit the vendor canonical hosts. Bounded, because an
        # unknown provider has no spec either, so that arm contributes nothing
        # and the set collapses back to the operator's own configured URLs.
        logger.debug("Provider base URL lookup failed for %s", provider, exc_info=True)
        return None


def destination_redirects_away_from_config(
    candidate: str | None,
    *,
    configured: Iterable[str | None],
) -> bool:
    """True when *candidate* points somewhere configuration would not have.

    The whole gate in one predicate, and deliberately ignorant of what kind of
    destination it is judging: *configured* is whatever URLs the calling
    subsystem has already been told to use. LLM callers get that set from
    :func:`configured_llm_destinations`; the embedding and voice consumption
    points in the same audit cluster have their own settings fields and can
    pass those directly.

    Naming the host configuration already points at is not a redirection: it is
    what the per-thread field is FOR on the reference deployment, where the
    desktop client auto-fills the CLIProxy sidecar URL and relies on the
    server's key.

    Loopback and private-LAN destinations are deliberately NOT exempt, even
    though they cannot leave the building. The resolver probes any local base
    URL WITH the key attached before the main call, so exempting them would
    turn the field into credentialed internal port scanning: name
    ``http://127.0.0.1:<port>`` and whatever answers is handed the operator's
    key. The cost is that a thread aimed at a keyless LAN model server has to
    set a per-thread ``api_key`` as well (any value; the server ignores it),
    which is the same one-field hatch every other destination uses.

    Shape (b) of the three in ``SECURITY.md`` 2.5.
    """
    if not (candidate or "").strip():
        # Naming nothing is not a redirection. `""` is the spelling for
        # "explicit direct API" and has to stay usable.
        return False
    key = base_url_destination_key(candidate)
    if key is None:
        # Non-empty but unparseable. This MUST read as a redirection: treating
        # it as "no host" the way the empty case is treated would make
        # `http://[oops` a gate bypass, since the value still reaches the
        # client further down.
        return True
    return key not in {
        known for known in (base_url_destination_key(v) for v in configured) if known
    }


def provider_probe_headers(
    provider: str, api_key: str, *, has_custom_base_url: bool = False
) -> dict[str, str]:
    """Auth + identity headers for a direct provider HTTP probe (test or models).

    ``has_custom_base_url`` signals the request targets a non-default base URL
    (e.g. CLIProxy); for anthropic that adds the cloak-skip ``User-Agent``, so
    the provider-test probe and the model listing send the same identity to a
    custom endpoint. The value is irrelevant for OpenAI-compatible providers.
    """
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_API_VERSION}
        if has_custom_base_url:
            # Function-local import for the same reason as above.
            from ..vendor.react_agent.cliproxy import CLIPROXY_CLAUDE_USER_AGENT

            headers["User-Agent"] = CLIPROXY_CLAUDE_USER_AGENT
        return headers
    if is_google_native_provider(provider):
        # Native Gemini wire: the key rides x-goog-api-key (a Bearer header
        # is ignored by Google and by CLIProxy's /v1beta inbound alike).
        return {"x-goog-api-key": api_key}
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers.update(OPENROUTER_ATTRIBUTION_HEADERS)
    return headers


def cliproxy_failure_hint(base_url: str | None, message: str) -> str:
    """Plain-language hint for the CLIProxy model/auth failure shapes, or "".

    The raw messages are misread in practice (dogfood 2026-08-05): the
    proxy's 502 "unknown provider for model X" reads as a Nymeria config
    bug, the upstream's 404 "Requested entity was not found" says nothing
    actionable, and the proxy-local 503 ``auth_unavailable`` looks like a
    logged-out credential when it is a self-clearing backoff. Keyed on the
    SAME predicate the runtime uses to decide the route is a CLIProxy
    (``looks_like_cliproxy_url``), so a direct-API failure is never
    editorialized. ONE shared helper for /provider test AND the live-turn
    error path (backlog #148): the two surfaces must not drift. This
    appends copy to an already-failed request; it never classifies behavior
    off message text (the trap `docs/private/cliproxy.md` documents for the
    verify endpoint). Taxonomy: that doc's "Interpreting proxy auth errors".
    """
    if not base_url:
        return ""
    # Function-local vendored import, same reason as the cloak header above.
    from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

    if not looks_like_cliproxy_url(base_url):
        return ""
    lowered = message.casefold()
    if "unknown provider for model" in lowered:
        return (
            "This means no logged-in subscription on the proxy has"
            " registered that model id (its registry is exact-match,"
            " no prefixes). Pick from the live list: /provider cliproxy"
            " <target>, or /model."
        )
    if "requested entity was not found" in lowered:
        return (
            "The subscription's upstream does not serve this model id"
            " for your account, even though the proxy registered it."
            " Pick a different model: /provider cliproxy <target>, or"
            " /model."
        )
    if "auth_unavailable" in lowered or "no auth available" in lowered:
        # Two causes share this wire shape (taxonomy): the usual TEMPORARY
        # error backoff, and a revoked/disabled login (upstream 401 also
        # marks unavailable with a retry timer). The copy must not send the
        # revoked case into an endless wait.
        return (
            "Every proxy login that could serve this model is unavailable"
            " (auth_unavailable), so nothing was dispatched upstream."
            " Usually this is a temporary error backoff that clears on its"
            " own (re-login does not help there). If it persists across"
            " retries, the login is likely revoked or disabled, and then a"
            " re-login IS the fix. Check /provider status, or switch model:"
            " /model."
        )
    return ""


def redact_secrets(text: str, *secrets: str | None) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def http_error_detail(response: httpx.Response, *secrets: str | None) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return redact_secrets(text, *secrets)[:300] or response.reason_phrase

    message: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()
        if message is None:
            candidate = body.get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()

    return redact_secrets(message or response.reason_phrase, *secrets)[:300]


def base_url_allows_no_api_key(base_url: str | None) -> bool:
    return is_local_llm_base_url(base_url)


def first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def first_float(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _unambiguous_max_tokens_as_output(
    model: dict[str, Any], context_length: int | None
) -> int | None:
    """Read ``max_tokens`` as the OUTPUT cap, but only where it can mean nothing else.

    Anthropic's Models API spells the output ceiling ``max_tokens`` and the
    context window ``max_input_tokens``; it has no ``max_output_tokens`` field
    at all, so every Anthropic model registered from a live listing used to come
    back with a null output ceiling.

    The name cannot simply be added to the output key list, because it is not
    self-describing: some third-party catalogs use ``max_tokens`` for the
    CONTEXT window, and reading one of those as an output cap would hand a model
    a ceiling the size of its whole window. So accept it only on the shape where
    that misreading is not possible: a row that also carries a context window,
    with ``max_tokens`` strictly below it. A source that conflates the two axes
    repeats the same number (the context == output fingerprint recorded in
    docs/private/plans/model-capability-resolution.md), which this refuses along
    with rows that carry no context at all.

    Two residual ambiguities this does NOT resolve, both shared with the
    explicit output keys above rather than introduced here. A mixed-convention
    row (window bumped, stale ``max_tokens`` still holding the OLD window) still
    passes, since the stale value is genuinely smaller. And no field name
    distinguishes a hard ceiling from a default request budget: xai documents
    128000 as its ``max_completion_tokens`` DEFAULT, and a source echoing that
    into a listing reads here as a cap. Both need a provider that states which
    quantity it means; see the capability-resolution plan.
    """
    if not context_length or context_length <= 0:
        return None
    value = first_int(model, "max_tokens")
    if value is None or value >= context_length:
        return None
    return value


def extract_model_metadata(model: dict[str, Any]) -> dict[str, Any]:
    """Normalize common metadata fields returned by provider /models APIs."""
    architecture = model.get("architecture") or {}
    if not isinstance(architecture, dict):
        architecture = {}
    top_provider = model.get("top_provider") or {}
    if not isinstance(top_provider, dict):
        top_provider = {}
    defaults = model.get("default_parameters") or model.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}
    pricing = model.get("pricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}

    # Both axes are spelled differently by every dialect, and camelCase is not
    # cosmetic: Google's native model listing reports
    # inputTokenLimit/outputTokenLimit, so a snake_case-only reader sees a
    # listing with no limits at all rather than a wrong number. No caller feeds
    # that endpoint here TODAY (the wizard probes Gemini through its
    # OpenAI-compat shim), but ~130 providers reach this function with an
    # operator-supplied base URL, and CLIProxy's management plane serves the
    # same camelCase shape.
    context_length = (
        first_int(
            model,
            "context_length",
            "context_window",
            "context_size",
            "max_context_length",
            "max_context_tokens",
            "input_token_limit",
            "inputTokenLimit",
            "max_input_tokens",
        )
        or first_int(top_provider, "context_length", "max_context_tokens")
    )
    max_completion_tokens = (
        first_int(
            model,
            "max_completion_tokens",
            "max_output_tokens",
            "output_token_limit",
            "outputTokenLimit",
        )
        or first_int(top_provider, "max_completion_tokens", "max_output_tokens")
        # Last, and guarded: Anthropic's output ceiling is named max_tokens.
        or _unambiguous_max_tokens_as_output(model, context_length)
    )

    raw_supported = model.get("supported_parameters") or model.get("supported_params") or []
    if not isinstance(raw_supported, (list, tuple, set)):
        raw_supported = []
    supported_parameters = [str(param) for param in raw_supported if param]
    raw_modalities = (
        model.get("input_modalities")
        or architecture.get("input_modalities")
        or model.get("modalities")
        or []
    )
    if not isinstance(raw_modalities, (list, tuple, set)):
        raw_modalities = []
    input_modalities = [str(modality) for modality in raw_modalities if modality]

    return {
        "context_length": context_length,
        "max_completion_tokens": max_completion_tokens,
        "supported_parameters": supported_parameters,
        "input_modalities": input_modalities,
        "tokenizer": architecture.get("tokenizer") or model.get("tokenizer"),
        "default_temperature": first_float(defaults, "temperature"),
        "default_top_p": first_float(defaults, "top_p"),
        "default_frequency_penalty": first_float(defaults, "frequency_penalty"),
        "pricing_prompt": first_float(pricing, "prompt"),
        "pricing_completion": first_float(pricing, "completion"),
    }
