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


async def _test_github(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    _ = provider, kind, settings
    token = _first_secret(secret_fields, "token", "api_key", "value")
    base_url = _base_url(metadata, "base_url", "api_base_url") or "https://api.github.com"
    return await _get_json_probe(
        f"{base_url}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        secrets=(token, base_url),
    )


async def _test_todoist(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    _ = provider, kind
    token = _first_secret(secret_fields, "api_key", "token", "value")
    configured = getattr(settings, "todoist_base_url", None) if settings is not None else None
    base_url = _base_url(metadata, "base_url", "api_base_url") or configured or "https://api.todoist.com/api/v1"
    return await _get_json_probe(
        f"{str(base_url).rstrip('/')}/projects",
        headers={"Authorization": f"Bearer {token}"},
        secrets=(token, str(base_url)),
    )


async def _test_anthropic(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    _ = provider, kind, settings
    api_key = _first_secret(secret_fields, "api_key", "key", "value")
    base_url = _base_url(metadata, "base_url", "api_base_url") or "https://api.anthropic.com/v1"
    return await _get_json_probe(
        f"{base_url}/models",
        headers={
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        },
        secrets=(api_key, base_url),
    )


async def _test_tavily(
    provider: str,
    kind: str,
    metadata: dict[str, Any],
    secret_fields: dict[str, str],
    settings: Any,
) -> CredentialTestResult:
    _ = provider, kind, metadata, settings
    api_key = _first_secret(secret_fields, "api_key", "token", "value")
    return await _get_json_probe(
        "https://api.tavily.com/usage",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        secrets=(api_key,),
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


__all__ = [
    "CredentialTestResult",
    "CredentialTester",
    "register_credential_tester",
    "test_credential_fields",
    "unregister_credential_tester",
]
