"""Provider-specific credential verification for the credential vault.

Testers receive plaintext values only inside the API process and return
redacted, user-safe status objects. The agent sees only the resulting status
and message, never the submitted secret fields.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from ..config.llm_providers import is_known_llm_provider, resolve_provider_base_url
from .llm_provider_utils import http_error_detail, redact_secrets


@dataclass(frozen=True)
class CredentialTestResult:
    ok: bool
    message: str
    code: str
    verified: bool = True
    metadata: dict[str, Any] | None = None


CredentialTester = Callable[
    [str, str, dict[str, Any], dict[str, str], Any],
    Awaitable[CredentialTestResult],
]


_TESTERS: dict[str, CredentialTester] = {}
_DEFAULT_TIMEOUT_SECONDS = 10.0


def register_credential_tester(provider: str, tester: CredentialTester) -> None:
    _TESTERS[provider.strip().lower()] = tester


def unregister_credential_tester(provider: str) -> None:
    _TESTERS.pop(provider.strip().lower(), None)


def _first_secret(secret_fields: dict[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = secret_fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in secret_fields.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _base_url(metadata: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return None


async def test_credential_fields(
    *,
    provider: str,
    kind: str,
    metadata: dict[str, Any] | None,
    secret_fields: dict[str, str],
    settings: Any = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> CredentialTestResult:
    provider_norm = (provider or "").strip().lower()
    clean_metadata = metadata or {}
    if not any(isinstance(v, str) and v.strip() for v in secret_fields.values()):
        return CredentialTestResult(
            ok=False,
            message="No secret fields were provided.",
            code="missing_secret",
            verified=False,
        )

    tester = _TESTERS.get(provider_norm)
    if tester is None and is_known_llm_provider(provider_norm):
        tester = _test_openai_compatible_llm
    if tester is None:
        return CredentialTestResult(
            ok=True,
            message="This provider has no verification probe yet.",
            code="no_tester",
            verified=False,
        )

    try:
        return await asyncio.wait_for(
            tester(provider_norm, kind, clean_metadata, secret_fields, settings),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        return CredentialTestResult(
            ok=False,
            message="Credential verification timed out.",
            code="timeout",
            verified=True,
        )
    except Exception as exc:  # noqa: BLE001 - user-facing probe errors are expected.
        return CredentialTestResult(
            ok=False,
            message=redact_secrets(str(exc), *secret_fields.values())[:300],
            code="probe_error",
            verified=True,
        )


async def _get_json_probe(
    url: str,
    *,
    headers: dict[str, str],
    secrets: tuple[str | None, ...],
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> CredentialTestResult:
    # User-supplied base URLs flow into this probe, so screen every request
    # through the shared HTTP egress policy (private/loopback/metadata targets
    # blocked, redirects re-validated, DNS pinned). The policy helper is
    # synchronous, so run it off the event loop to keep the DNS pin intact.
    from .http_policy import (
        HTTPPolicyRedirectLimit,
        HTTPPolicyViolation,
        httpx_request_with_policy,
    )

    def _probe():
        resp, _chain, _decision = httpx_request_with_policy(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        return resp

    try:
        response = await asyncio.to_thread(_probe)
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as exc:
        return CredentialTestResult(
            ok=False,
            message=redact_secrets(str(exc), *secrets)[:300],
            code="blocked_url",
            verified=True,
        )
    if response.status_code < 400:
        return CredentialTestResult(
            ok=True,
            message="Provider verification succeeded.",
            code="verified",
            verified=True,
            metadata={"status_code": response.status_code},
        )
    return CredentialTestResult(
        ok=False,
        message=f"Provider returned HTTP {response.status_code}: {http_error_detail(response, *secrets)}",
        code="http_error",
        verified=True,
        metadata={"status_code": response.status_code},
    )


def _make_get_tester(
    *,
    name: str,
    secret_names: tuple[str, ...],
    header_builder: Callable[[Optional[str]], dict[str, str]],
    fixed_url: Optional[str] = None,
    path: Optional[str] = None,
    default_base_url: Optional[str] = None,
    settings_base_attr: Optional[str] = None,
) -> CredentialTester:
    """Build a GET-probe credential tester from a small table.

    The GitHub/Todoist/Anthropic/Tavily/Brave testers all extract one secret via
    ``_first_secret``, build headers from it, and hit a GET endpoint through the
    shared egress-screened ``_get_json_probe``. They differ only in the secret
    field-name order, the URL, and the header shape, so they are generated here
    rather than hand-written. The genuinely different POST/SearXNG/LLM testers
    stay explicit below.

    Two URL modes:

    - ``fixed_url`` set: a hard-coded host (no user-supplied base URL); the only
      redaction secret is the resolved value (matches ``_test_tavily`` /
      ``_test_brave``).
    - ``path`` + ``default_base_url`` set: resolve the base URL from metadata
      (then an optional ``settings`` attribute, then the default), append
      ``path``, and redact both the value and the base URL (matches
      ``_test_github`` / ``_test_todoist`` / ``_test_anthropic``). ``path`` must
      include its own leading ``/``.

    Exactly one of ``fixed_url`` or ``path`` must be set; path mode also needs a
    ``default_base_url``. These are validated at import (construction) time so a
    malformed table entry fails loudly rather than shipping a broken probe.
    """
    if (fixed_url is None) == (path is None):
        raise ValueError("_make_get_tester needs exactly one of fixed_url or path")
    if path is not None and default_base_url is None:
        raise ValueError("_make_get_tester path mode requires default_base_url")

    async def _tester(
        provider: str,
        kind: str,
        metadata: dict[str, Any],
        secret_fields: dict[str, str],
        settings: Any,
    ) -> CredentialTestResult:
        _ = provider, kind
        value = _first_secret(secret_fields, *secret_names)
        if fixed_url is not None:
            return await _get_json_probe(
                fixed_url,
                headers=header_builder(value),
                secrets=(value,),
            )
        configured = (
            getattr(settings, settings_base_attr, None)
            if settings_base_attr and settings is not None
            else None
        )
        base_url = _base_url(metadata, "base_url", "api_base_url") or configured or default_base_url
        return await _get_json_probe(
            f"{str(base_url).rstrip('/')}{path}",
            headers=header_builder(value),
            secrets=(value, str(base_url)),
        )

    _tester.__name__ = name
    _tester.__qualname__ = name
    return _tester


_test_github = _make_get_tester(
    name="_test_github",
    secret_names=("token", "api_key", "value"),
    path="/user",
    default_base_url="https://api.github.com",
    header_builder=lambda value: {
        "Authorization": f"Bearer {value}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
)


_test_todoist = _make_get_tester(
    name="_test_todoist",
    secret_names=("api_key", "token", "value"),
    path="/projects",
    default_base_url="https://api.todoist.com/api/v1",
    settings_base_attr="todoist_base_url",
    header_builder=lambda value: {"Authorization": f"Bearer {value}"},
)


_test_anthropic = _make_get_tester(
    name="_test_anthropic",
    secret_names=("api_key", "key", "value"),
    path="/models",
    default_base_url="https://api.anthropic.com/v1",
    header_builder=lambda value: {
        "x-api-key": value or "",
        "anthropic-version": "2023-06-01",
    },
)


_test_tavily = _make_get_tester(
    name="_test_tavily",
    secret_names=("api_key", "token", "value"),
    fixed_url="https://api.tavily.com/usage",
    header_builder=lambda value: {"Authorization": f"Bearer {value}"} if value else {},
)


# Brave's Web Search endpoint is a GET, so validate the key with a minimal query
# (count=1) against the fixed api.search.brave.com host. The token rides in the
# X-Subscription-Token header (not a Bearer). 401 means a bad key; a status below
# 400 means the key works.
_test_brave = _make_get_tester(
    name="_test_brave",
    secret_names=("api_key", "token", "value"),
    fixed_url="https://api.search.brave.com/res/v1/web/search?q=ping&count=1",
    header_builder=lambda value: {
        "X-Subscription-Token": value or "",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    },
)


# The generic openai-compatible fallback can never validate Perplexity: it
# probes {base}/models, and the registry base URL (correct for chat) has no
# /models at its root; the model list lives under /v1. GET /v1/models is
# auth-gated (measured 2026-08-24: 401 in ~0.3s for a missing OR bogus key,
# despite upstream docs suggesting no auth is needed), which makes it a free, fast,
# model-agnostic key check. Deliberately not a chat probe: a /v1/agent or
# sonar completion bills the key a per-request search fee on every Test
# click and a slow search can time the 10s budget out, marking a VALID key
# invalid. If Perplexity ever drops the auth gate on /v1/models this probe
# false-positives: re-verify during the #243 Agent API migration.
_test_perplexity = _make_get_tester(
    name="_test_perplexity",
    secret_names=("api_key", "token", "value"),
    fixed_url="https://api.perplexity.ai/v1/models",
    header_builder=lambda value: {
        "Authorization": f"Bearer {value or ''}",
        "Accept": "application/json",
    },
)


async def _test_exa(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    # Exa has no free GET auth-check endpoint, so validate the key with the
    # cheapest possible call: a minimal "instant" search (numResults=1, no
    # contents) against the fixed api.exa.ai host. 401 means a bad key; a status
    # below 400 means the key works. The host is a hard-coded constant (no
    # user-supplied URL), so SSRF
    # screening adds nothing; the POST still goes through policy_http_client
    # so env proxy mounts are neutralized (a bare
    # httpx.Client would silently route this key-bearing request through
    # HTTPS_PROXY/ALL_PROXY).
    _ = provider, kind, metadata, settings
    from .http_policy import policy_http_client

    api_key = _first_secret(secret_fields, "api_key", "token", "value")

    def _probe():
        with policy_http_client(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            return client.post(
                "https://api.exa.ai/search",
                headers={
                    "x-api-key": api_key or "",
                    "x-exa-integration": "nymeria",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"query": "ping", "type": "instant", "numResults": 1},
            )

    response = await asyncio.to_thread(_probe)
    if response.status_code < 400:
        return CredentialTestResult(
            ok=True,
            message="Provider verification succeeded.",
            code="verified",
            verified=True,
            metadata={"status_code": response.status_code},
        )
    return CredentialTestResult(
        ok=False,
        message=f"Provider returned HTTP {response.status_code}: {http_error_detail(response, api_key)}",
        code="http_error",
        verified=True,
        metadata={"status_code": response.status_code},
    )


async def _test_firecrawl(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    # Firecrawl has no free GET auth-check endpoint, so validate the key with the
    # cheapest representative call: a minimal /v2/search (limit=1, no
    # scrapeOptions) against the fixed api.firecrawl.dev host. 401 means a bad
    # key; a status below 400 means the key works. The host is a hard-coded
    # constant (no user-supplied URL), so SSRF
    # screening adds nothing; the POST still goes through policy_http_client
    # so env proxy mounts are neutralized (a bare
    # httpx.Client would silently route this key-bearing request through
    # HTTPS_PROXY/ALL_PROXY).
    _ = provider, kind, metadata, settings
    from .http_policy import policy_http_client

    api_key = _first_secret(secret_fields, "api_key", "token", "value")

    def _probe():
        with policy_http_client(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            return client.post(
                "https://api.firecrawl.dev/v2/search",
                headers={
                    "Authorization": f"Bearer {api_key or ''}",
                    "Content-Type": "application/json",
                },
                json={"query": "ping", "limit": 1},
            )

    response = await asyncio.to_thread(_probe)
    if response.status_code < 400:
        return CredentialTestResult(
            ok=True,
            message="Provider verification succeeded.",
            code="verified",
            verified=True,
            metadata={"status_code": response.status_code},
        )
    return CredentialTestResult(
        ok=False,
        message=f"Provider returned HTTP {response.status_code}: {http_error_detail(response, api_key)}",
        code="http_error",
        verified=True,
        metadata={"status_code": response.status_code},
    )


async def _test_searxng(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    # SearXNG runs as an internal sidecar (e.g. http://searxng:8080), so its base
    # URL is intentionally a private/loopback host. The shared SSRF egress policy
    # (httpx_request_with_policy) would block the very host we need to reach, so
    # address screening is skipped; the client still comes from
    # policy_http_client (no address validation, but env proxy mounts are
    # neutralized so the internal sidecar URL never leaks to an HTTP_PROXY). It also verifies that JSON
    # output is enabled (search.formats must include "json"); a non-JSON response
    # means JSON is off.
    _ = provider, kind
    from .http_policy import policy_http_client

    base_url = _first_secret(secret_fields, "base_url", "url", "value")
    if not base_url:
        configured = getattr(settings, "searxng_base_url", None) if settings is not None else None
        base_url = _base_url(metadata, "base_url", "url") or configured
    if not base_url:
        return CredentialTestResult(
            ok=False,
            message="No SearXNG base URL was provided.",
            code="missing_base_url",
            verified=True,
        )
    base = str(base_url).rstrip("/")

    def _probe():
        with policy_http_client(timeout=_DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
            return client.get(
                f"{base}/search",
                params={"q": "ping", "format": "json"},
                headers={"Accept": "application/json"},
            )

    response = await asyncio.to_thread(_probe)
    if response.status_code >= 400:
        return CredentialTestResult(
            ok=False,
            message=f"SearXNG returned HTTP {response.status_code}: {http_error_detail(response, base)}",
            code="http_error",
            verified=True,
            metadata={"status_code": response.status_code},
        )
    try:
        data = response.json()
    except Exception:
        return CredentialTestResult(
            ok=False,
            message="SearXNG responded without JSON; enable 'json' in the instance's search.formats.",
            code="json_disabled",
            verified=True,
        )
    if not isinstance(data, dict) or "results" not in data:
        return CredentialTestResult(
            ok=False,
            message="SearXNG response is missing a results field; check that JSON output is enabled.",
            code="bad_response",
            verified=True,
        )
    return CredentialTestResult(
        ok=True,
        message="Provider verification succeeded.",
        code="verified",
        verified=True,
        metadata={"status_code": response.status_code},
    )


async def _test_openai_compatible_llm(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    _ = kind
    api_key = _first_secret(secret_fields, "api_key", "key", "token", "value")
    configured_base = _base_url(metadata, "base_url", "api_base_url", "llm_base_url")
    base_url = resolve_provider_base_url(
        provider,
        configured_base_url=configured_base,
        settings=settings,
        include_default=True,
    )
    if not base_url:
        return CredentialTestResult(
            ok=False,
            message="No base URL is known for this provider.",
            code="missing_base_url",
            verified=True,
        )
    probe_url = f"{base_url.rstrip('/')}/models"
    return await _get_json_probe(
        probe_url,
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        secrets=(api_key, base_url),
    )


register_credential_tester("github", _test_github)
register_credential_tester("todoist", _test_todoist)
register_credential_tester("anthropic", _test_anthropic)
register_credential_tester("anthropic_direct", _test_anthropic)
register_credential_tester("tavily", _test_tavily)
register_credential_tester("exa", _test_exa)
register_credential_tester("firecrawl", _test_firecrawl)
register_credential_tester("brave", _test_brave)
register_credential_tester("searxng", _test_searxng)
# Aliases mirror tools/web.py's ProviderCredentialSpec for the same provider.
register_credential_tester("perplexity", _test_perplexity)
register_credential_tester("perplexity_api", _test_perplexity)
register_credential_tester("pplx", _test_perplexity)


__all__ = [
    "CredentialTestResult",
    "CredentialTester",
    "register_credential_tester",
    "test_credential_fields",
    "unregister_credential_tester",
]
