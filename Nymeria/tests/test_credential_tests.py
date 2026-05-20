"""Tests for provider credential verification registry."""

from __future__ import annotations

import asyncio
from typing import Any

from nymeria.core.credential_tests import (
    CredentialTestResult,
    register_credential_tester,
    test_credential_fields as run_credential_test,
    unregister_credential_tester,
)


def test_no_tester_returns_not_verified_success():
    result = asyncio.run(
        run_credential_test(
            provider="not-real-provider-nymeria-test",
            kind="api_key",
            metadata={},
            secret_fields={"value": "secret"},
        )
    )
    assert result.ok is True
    assert result.verified is False
    assert result.code == "no_tester"
    assert "no verification probe" in result.message


def test_missing_secret_fails_before_provider_probe():
    result = asyncio.run(
        run_credential_test(
            provider="not-real-provider-nymeria-test",
            kind="api_key",
            metadata={},
            secret_fields={},
        )
    )
    assert result.ok is False
    assert result.verified is False
    assert result.code == "missing_secret"


def test_registered_tester_success_and_failure():
    async def fake_tester(
        provider: str,
        kind: str,
        metadata: dict[str, Any],
        secret_fields: dict[str, str],
        settings: Any,
    ) -> CredentialTestResult:
        _ = provider, kind, metadata, settings
        if secret_fields["value"] == "good":
            return CredentialTestResult(
                ok=True,
                message="ok",
                code="verified",
                verified=True,
            )
        return CredentialTestResult(
            ok=False,
            message="bad",
            code="rejected",
            verified=True,
        )

    register_credential_tester("fake-provider", fake_tester)
    try:
        ok = asyncio.run(
            run_credential_test(
                provider="fake-provider",
                kind="api_key",
                metadata={},
                secret_fields={"value": "good"},
            )
        )
        failed = asyncio.run(
            run_credential_test(
                provider="fake-provider",
                kind="api_key",
                metadata={},
                secret_fields={"value": "bad"},
            )
        )
    finally:
        unregister_credential_tester("fake-provider")

    assert ok.ok is True
    assert ok.verified is True
    assert ok.code == "verified"
    assert failed.ok is False
    assert failed.code == "rejected"


def test_probe_exception_redacts_submitted_secrets():
    async def exploding_tester(
        provider: str,
        kind: str,
        metadata: dict[str, Any],
        secret_fields: dict[str, str],
        settings: Any,
    ) -> CredentialTestResult:
        _ = provider, kind, metadata, settings
        raise RuntimeError(f"provider rejected {secret_fields['value']}")

    register_credential_tester("exploding-provider", exploding_tester)
    try:
        result = asyncio.run(
            run_credential_test(
                provider="exploding-provider",
                kind="api_key",
                metadata={},
                secret_fields={"value": "secret-value"},
            )
        )
    finally:
        unregister_credential_tester("exploding-provider")

    assert result.ok is False
    assert result.code == "probe_error"
    assert "secret-value" not in result.message
    assert "[redacted]" in result.message


def test_probe_timeout_returns_timeout_status():
    async def slow_tester(
        provider: str,
        kind: str,
        metadata: dict[str, Any],
        secret_fields: dict[str, str],
        settings: Any,
    ) -> CredentialTestResult:
        _ = provider, kind, metadata, secret_fields, settings
        await asyncio.sleep(0.2)
        return CredentialTestResult(ok=True, message="late", code="verified")

    register_credential_tester("slow-provider", slow_tester)
    try:
        result = asyncio.run(
            run_credential_test(
                provider="slow-provider",
                kind="api_key",
                metadata={},
                secret_fields={"value": "secret"},
                timeout_seconds=0.01,
            )
        )
    finally:
        unregister_credential_tester("slow-provider")

    assert result.ok is False
    assert result.code == "timeout"
    assert result.verified is True
