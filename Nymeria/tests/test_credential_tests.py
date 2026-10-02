"""Tests for provider credential verification registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import nymeria.core.credential_tests as credential_tests
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


def test_probe_blocks_internal_base_url():
    # A user-supplied metadata.base_url pointing at an internal/loopback target
    # must be rejected by the HTTP egress policy before the probe is sent.
    result = asyncio.run(
        run_credential_test(
            provider="github",
            kind="api_key",
            metadata={"base_url": "http://127.0.0.1:8000"},
            secret_fields={"token": "ghp_example"},
        )
    )
    assert result.ok is False
    assert result.code == "blocked_url"


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


# ---------------------------------------------------------------------------
# F12: GET-probe tester factory (_make_get_tester)
#
# The github/todoist/anthropic/tavily/brave testers are generated from a table.
# These tests pin the exact (url, headers, secrets) each one passes to the shared
# _get_json_probe, so the factory stays byte-for-byte behavior-preserving.
# ---------------------------------------------------------------------------


def _capture_probe(monkeypatch):
    """Patch the shared probe to record its call args and return a captured dict."""
    captured: dict[str, Any] = {}

    async def fake_probe(url, *, headers, secrets, timeout=credential_tests._DEFAULT_TIMEOUT_SECONDS):
        _ = timeout
        captured.clear()
        captured.update(url=url, headers=headers, secrets=secrets)
        return CredentialTestResult(ok=True, message="ok", code="verified")

    monkeypatch.setattr(credential_tests, "_get_json_probe", fake_probe)
    return captured


def _run_tester(tester, *, metadata=None, fields, settings=None):
    asyncio.run(tester("prov", "api_key", metadata or {}, fields, settings))


def test_github_tester_probe_shape(monkeypatch):
    captured = _capture_probe(monkeypatch)
    _run_tester(credential_tests._test_github, fields={"token": "ght"})
    assert captured["url"] == "https://api.github.com/user"
    assert captured["headers"] == {
        "Authorization": "Bearer ght",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    assert captured["secrets"] == ("ght", "https://api.github.com")


def test_github_tester_metadata_base_url_is_rstripped(monkeypatch):
    captured = _capture_probe(monkeypatch)
    _run_tester(
        credential_tests._test_github,
        metadata={"base_url": "https://gh.example.com/"},
        fields={"api_key": "k2"},
    )
    # _base_url strips the trailing slash before the path is appended, and the
    # same stripped value is what gets redacted.
    assert captured["url"] == "https://gh.example.com/user"
    assert captured["secrets"] == ("k2", "https://gh.example.com")


def test_todoist_tester_default_and_field_order(monkeypatch):
    captured = _capture_probe(monkeypatch)
    # todoist prefers "api_key" over "token"; github prefers "token" first.
    _run_tester(credential_tests._test_todoist, fields={"api_key": "td", "token": "ignored"})
    assert captured["url"] == "https://api.todoist.com/api/v1/projects"
    assert captured["headers"] == {"Authorization": "Bearer td"}
    assert captured["secrets"] == ("td", "https://api.todoist.com/api/v1")


def test_todoist_tester_settings_fallback_keeps_raw_base_in_redaction(monkeypatch):
    captured = _capture_probe(monkeypatch)
    _run_tester(
        credential_tests._test_todoist,
        fields={"token": "td2"},
        settings=SimpleNamespace(todoist_base_url="https://td.example.com/"),
    )
    # The settings base URL is not run through _base_url, so the trailing slash is
    # only stripped for the request URL, while the redaction tuple keeps the raw
    # value (matching the pre-factor behavior exactly).
    assert captured["url"] == "https://td.example.com/projects"
    assert captured["secrets"] == ("td2", "https://td.example.com/")


def test_anthropic_tester_probe_shape_and_empty_key_header(monkeypatch):
    captured = _capture_probe(monkeypatch)
    # No recognized secret field name -> _first_secret falls back to any value.
    _run_tester(credential_tests._test_anthropic, fields={"key": "ak"})
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"] == {
        "x-api-key": "ak",
        "anthropic-version": "2023-06-01",
    }
    assert captured["secrets"] == ("ak", "https://api.anthropic.com/v1")


def test_tavily_tester_fixed_host_and_conditional_header(monkeypatch):
    captured = _capture_probe(monkeypatch)
    _run_tester(credential_tests._test_tavily, fields={"api_key": "tv"})
    assert captured["url"] == "https://api.tavily.com/usage"
    assert captured["headers"] == {"Authorization": "Bearer tv"}
    assert captured["secrets"] == ("tv",)


def test_tavily_tester_blank_secret_drops_header(monkeypatch):
    captured = _capture_probe(monkeypatch)
    # A blank/whitespace value resolves to None, so the Authorization header is
    # omitted entirely and only None is redacted.
    _run_tester(credential_tests._test_tavily, fields={"value": "   "})
    assert captured["headers"] == {}
    assert captured["secrets"] == (None,)


def test_brave_tester_fixed_url_and_subscription_header(monkeypatch):
    captured = _capture_probe(monkeypatch)
    _run_tester(credential_tests._test_brave, fields={"api_key": "bv"})
    assert captured["url"] == "https://api.search.brave.com/res/v1/web/search?q=ping&count=1"
    assert captured["headers"] == {
        "X-Subscription-Token": "bv",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    assert captured["secrets"] == ("bv",)


def test_brave_tester_blank_secret_keeps_empty_header(monkeypatch):
    captured = _capture_probe(monkeypatch)
    # Unlike Tavily's drop-if-empty Authorization header, Brave/Anthropic build
    # the header unconditionally via `value or ""`, so a blank secret still sends
    # the header key with an empty value.
    _run_tester(credential_tests._test_brave, fields={"value": "   "})
    assert captured["headers"]["X-Subscription-Token"] == ""
    assert captured["secrets"] == (None,)


def test_generated_testers_preserve_names_and_registrations():
    # The factory restores each function's __name__/__qualname__ so tracebacks and
    # introspection stay meaningful, and the public registry resolves to them.
    for name in ("_test_github", "_test_todoist", "_test_anthropic", "_test_tavily", "_test_brave"):
        tester = getattr(credential_tests, name)
        assert tester.__name__ == name
        assert tester.__qualname__ == name
    assert credential_tests._TESTERS["github"] is credential_tests._test_github
    assert credential_tests._TESTERS["todoist"] is credential_tests._test_todoist
    assert credential_tests._TESTERS["anthropic"] is credential_tests._test_anthropic
    assert credential_tests._TESTERS["anthropic_direct"] is credential_tests._test_anthropic
    assert credential_tests._TESTERS["tavily"] is credential_tests._test_tavily
    assert credential_tests._TESTERS["brave"] is credential_tests._test_brave


def test_configurable_tester_default_base_url_used_when_no_metadata(monkeypatch):
    # Direct factory unit test: with no metadata and no settings attr, the default
    # base URL wins, and the path is appended after an rstrip.
    captured = _capture_probe(monkeypatch)
    tester = credential_tests._make_get_tester(
        name="_test_factory_probe",
        secret_names=("api_key",),
        path="/ping",
        default_base_url="https://factory.example.com/",
        header_builder=lambda value: {"Authorization": f"Bearer {value}"},
    )
    assert tester.__name__ == "_test_factory_probe"
    _run_tester(tester, fields={"api_key": "fk"})
    assert captured["url"] == "https://factory.example.com/ping"
    assert captured["secrets"] == ("fk", "https://factory.example.com/")


def test_factory_rejects_invalid_mode_combinations():
    import pytest

    builder = lambda value: {"Authorization": f"Bearer {value}"}  # noqa: E731
    # Neither mode selected.
    with pytest.raises(ValueError):
        credential_tests._make_get_tester(name="x", secret_names=("api_key",), header_builder=builder)
    # Both modes selected.
    with pytest.raises(ValueError):
        credential_tests._make_get_tester(
            name="x",
            secret_names=("api_key",),
            header_builder=builder,
            fixed_url="https://h.example.com/probe",
            path="/probe",
            default_base_url="https://h.example.com",
        )
    # Path mode without a default base URL.
    with pytest.raises(ValueError):
        credential_tests._make_get_tester(
            name="x", secret_names=("api_key",), header_builder=builder, path="/probe"
        )


# ---------------------------------------------------------------------------
# Perplexity tester (regression: perplexity must NOT fall through to the
# generic openai-compatible /models fallback: the registry base URL, correct
# for chat, has no /models at its root, so that probe 404s for every key,
# valid or not; the model list lives under /v1 and is auth-gated)
# ---------------------------------------------------------------------------


class _FakePerplexityResponse:
    def __init__(self, status_code: int, text: str = "", json_valid: bool = True):
        self.status_code = status_code
        self.text = text
        self.reason_phrase = "Unauthorized" if status_code == 401 else "OK"
        self._json_valid = json_valid

    def json(self):
        if not self._json_valid:
            raise ValueError("not json")
        return {"error": {"message": self.text}}


def test_perplexity_dispatch_probes_v1_models_for_all_aliases(monkeypatch):
    # Dispatch through the public entry point per alias, so this asserts the
    # observable outcome (the probe aimed at the real auth-gated endpoint)
    # rather than registry internals. Red if any alias falls through to the
    # generic /models fallback.
    captured = _capture_probe(monkeypatch)
    for name in ("perplexity", "perplexity_api", "pplx"):
        result = asyncio.run(
            run_credential_test(
                provider=name,
                kind="api_key",
                metadata={},
                secret_fields={"api_key": "pplx-test-123"},
            )
        )
        assert result.ok is True, name
        assert result.code == "verified", name
        assert captured["url"] == "https://api.perplexity.ai/v1/models", name
        assert captured["headers"]["Authorization"] == "Bearer pplx-test-123", name
        assert "pplx-test-123" in captured["secrets"], name


def test_perplexity_bad_key_is_http_error_and_redacted(monkeypatch):
    from nymeria.core import http_policy

    def fake_request(method, url, **kwargs):
        _ = method, url, kwargs
        return _FakePerplexityResponse(401, text="Unauthorized key pplx-test-123"), [], None

    monkeypatch.setattr(http_policy, "httpx_request_with_policy", fake_request)
    result = asyncio.run(
        run_credential_test(
            provider="perplexity",
            kind="api_key",
            metadata={},
            secret_fields={"api_key": "pplx-test-123"},
        )
    )
    assert result.ok is False
    assert result.code == "http_error"
    assert "401" in result.message
    assert "pplx-test-123" not in result.message


def test_perplexity_non_json_error_body_is_redacted(monkeypatch):
    # A gateway/HTML error body takes http_error_detail's text branch, the
    # one most likely to echo unredacted input.
    from nymeria.core import http_policy

    def fake_request(method, url, **kwargs):
        _ = method, url, kwargs
        return (
            _FakePerplexityResponse(
                502, text="<html>upstream error key pplx-test-123</html>", json_valid=False
            ),
            [],
            None,
        )

    monkeypatch.setattr(http_policy, "httpx_request_with_policy", fake_request)
    result = asyncio.run(
        run_credential_test(
            provider="perplexity",
            kind="api_key",
            metadata={},
            secret_fields={"api_key": "pplx-test-123"},
        )
    )
    assert result.ok is False
    assert result.code == "http_error"
    assert "pplx-test-123" not in result.message


# ---------------------------------------------------------------------------
# Proxy-mount neutralization: probes that carry a third-party key must not
# route through HTTPS_PROXY/ALL_PROXY from the process env (policy_http_client
# neutralizes the scheme mounts; a bare httpx.Client silently honors them).
# ---------------------------------------------------------------------------


def _capture_policy_client(monkeypatch):
    import httpx

    from nymeria.core import http_policy

    calls: dict[str, Any] = {}

    def _bare_client_forbidden(*args, **kwargs):
        raise AssertionError("bare httpx.Client constructed; probes must use policy_http_client")

    monkeypatch.setattr(httpx, "Client", _bare_client_forbidden)

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            calls["url"] = url
            calls["headers"] = headers or {}
            calls["json"] = json or {}
            return _FakePerplexityResponse(200, text="ok")

        def get(self, url, params=None, headers=None):
            calls["url"] = url
            calls["params"] = params or {}
            calls["headers"] = headers or {}
            return _FakePerplexityResponse(200, text="ok")

    def fake_factory(**kwargs):
        calls["factory_kwargs"] = kwargs
        return _FakeClient()

    monkeypatch.setattr(http_policy, "policy_http_client", fake_factory)
    return calls


def test_exa_probe_uses_policy_client(monkeypatch):
    calls = _capture_policy_client(monkeypatch)
    _run_tester(credential_tests._test_exa, fields={"api_key": "exa-key"})
    assert calls["url"] == "https://api.exa.ai/search"
    assert calls["headers"]["x-api-key"] == "exa-key"
    assert calls["factory_kwargs"].get("timeout") == credential_tests._DEFAULT_TIMEOUT_SECONDS


def test_firecrawl_probe_uses_policy_client(monkeypatch):
    calls = _capture_policy_client(monkeypatch)
    _run_tester(credential_tests._test_firecrawl, fields={"api_key": "fc-key"})
    assert calls["url"] == "https://api.firecrawl.dev/v2/search"
    assert calls["headers"]["Authorization"] == "Bearer fc-key"
    assert calls["factory_kwargs"].get("timeout") == credential_tests._DEFAULT_TIMEOUT_SECONDS


def test_searxng_probe_uses_policy_client(monkeypatch):
    # The sidecar base URL is deliberately private, so address screening is
    # skipped, but the client must still come from policy_http_client: a bare
    # httpx.Client would hand the internal URL to an env HTTP_PROXY.
    calls = _capture_policy_client(monkeypatch)
    _run_tester(
        credential_tests._test_searxng,
        fields={"base_url": "http://searxng:8080"},
    )
    assert calls["url"] == "http://searxng:8080/search"
    assert calls["factory_kwargs"].get("timeout") == credential_tests._DEFAULT_TIMEOUT_SECONDS
    assert calls["factory_kwargs"].get("follow_redirects") is True


# ---------------------------------------------------------------------------
# #244: a record stored under a declared spec alias is probed by its provider's
# tester. The rule is the vault lookup's (`native_credentials.provider_candidates`:
# the canonical name, its dash/underscore twins, and the declared aliases
# verbatim), case-folded the way dispatch always was. A spelling the runtime
# cannot match (a dash variant of an ALIAS) keeps `no_tester`, so the Test
# button never verifies a record every tool would refuse to use (decision
# "runtime-exact"). Widening `provider_candidates` later flips those cases on
# purpose; the ratchet below follows it automatically.
# ---------------------------------------------------------------------------


def _recording_tester(calls: list[str], label: str):
    async def tester(provider, kind, metadata, secret_fields, settings):
        _ = kind, metadata, secret_fields, settings
        calls.append(provider)
        return CredentialTestResult(ok=True, message=label, code=f"fake:{label}")

    return tester


def _dispatch(name: str, secret: str = "it244-secret") -> CredentialTestResult:
    return asyncio.run(
        run_credential_test(
            provider=name,
            kind="api_key",
            metadata={},
            secret_fields={"api_key": secret},
        )
    )


def test_declared_alias_runs_its_providers_real_tester(monkeypatch):
    # The item's own example, through the real Todoist tester (only the network
    # probe is faked), so the URL and header are what a real Test click sends.
    # Red before the fix: `todoist_api` matched nothing and returned no_tester
    # without probing.
    captured = _capture_probe(monkeypatch)

    result = _dispatch("todoist_api", secret="td-it244")

    assert result.code == "verified"
    assert result.verified is True
    assert captured["url"] == "https://api.todoist.com/api/v1/projects"
    assert captured["headers"] == {"Authorization": "Bearer td-it244"}


def test_every_runtime_spelling_of_a_tested_provider_reaches_its_tester(monkeypatch):
    # The ratchet: every name the vault lookup accepts for a provider with a
    # registered tester dispatches to THAT tester, which receives the canonical
    # provider name. Driven from the live spec registry, so a newly declared
    # alias is covered without editing this test.
    from nymeria.tools.credential_registry import iter_provider_specs
    from nymeria.tools.native_credentials import provider_candidates

    tested = [
        spec for spec in iter_provider_specs() if spec.provider in credential_tests._TESTERS
    ]
    # Guard against a vacuous pass (an empty registry or renamed testers).
    assert {"todoist", "tavily", "perplexity", "searxng"} <= {s.provider for s in tested}

    checked = 0
    for spec in tested:
        calls: list[str] = []
        monkeypatch.setitem(
            credential_tests._TESTERS, spec.provider, _recording_tester(calls, spec.provider)
        )
        for name in sorted(provider_candidates(spec.provider, spec.aliases)):
            calls.clear()
            result = _dispatch(name)
            assert result.code == f"fake:{spec.provider}", name
            assert calls == [spec.provider], name
            checked += 1
    # 8 providers, 22 spellings today; the floor only rises as aliases are added.
    assert checked >= 22


def test_alias_dispatch_strips_and_folds_case(monkeypatch):
    # The normalization dispatch always applied (strip, lower) happens BEFORE
    # alias resolution, so a padded, upper-cased alias still resolves.
    calls: list[str] = []
    monkeypatch.setitem(credential_tests._TESTERS, "todoist", _recording_tester(calls, "todoist"))

    result = _dispatch(" Todoist_API ")

    assert result.code == "fake:todoist"
    assert calls == ["todoist"]


def test_exact_registrations_still_win(monkeypatch):
    # Names with their own registration keep it: anthropic_direct has no spec
    # at all; outlook is registered directly. And an exact registration
    # outranks alias resolution: a dedicated tester registered under an alias
    # name is the one that runs, not the canonical provider's.
    captured = _capture_probe(monkeypatch)
    result = _dispatch("anthropic_direct", secret="ant-it244")
    assert result.code == "verified"
    assert captured["url"] == "https://api.anthropic.com/v1/models"

    outlook_calls: list[str] = []
    monkeypatch.setitem(
        credential_tests._TESTERS, "outlook", _recording_tester(outlook_calls, "outlook")
    )
    assert _dispatch("outlook").code == "fake:outlook"
    assert outlook_calls == ["outlook"]

    canonical_calls: list[str] = []
    alias_calls: list[str] = []
    monkeypatch.setitem(
        credential_tests._TESTERS, "todoist", _recording_tester(canonical_calls, "todoist")
    )
    monkeypatch.setitem(
        credential_tests._TESTERS, "todoist_api", _recording_tester(alias_calls, "alias")
    )
    assert _dispatch("todoist_api").code == "fake:alias"
    assert alias_calls == ["todoist_api"]
    assert canonical_calls == []


def test_llm_fallback_is_unchanged_and_keeps_the_stored_name(monkeypatch):
    # A known LLM provider with no spec tester still gets the generic
    # openai-compatible probe at its registry base URL.
    from nymeria.config.llm_providers import is_known_llm_provider, resolve_provider_base_url
    from nymeria.tools.credential_registry import get_provider_spec

    assert is_known_llm_provider("groq") and get_provider_spec("groq") is None
    base = resolve_provider_base_url("groq", include_default=True)
    assert base
    captured = _capture_probe(monkeypatch)

    result = _dispatch("groq", secret="gsk-it244")

    assert result.code == "verified"
    assert captured["url"] == f"{base.rstrip('/')}/models"
    assert captured["headers"] == {"Authorization": "Bearer gsk-it244"}

    # A spec provider WITHOUT a tester that is also an LLM id (openai) must
    # still fall through to the LLM probe, not stop at "spec found".
    assert get_provider_spec("openai") is not None
    assert "openai" not in credential_tests._TESTERS
    openai_base = resolve_provider_base_url("openai", include_default=True)
    assert openai_base
    assert _dispatch("openai", secret="sk-it244").code == "verified"
    assert captured["url"] == f"{openai_base.rstrip('/')}/models"


def test_specs_without_a_tester_and_unknown_names_stay_unprobed(monkeypatch):
    # Alias resolution only finds testers that exist. Notion has a spec (and
    # aliases) but no tester; an unknown name has neither.
    captured = _capture_probe(monkeypatch)
    for name in ("notion", "notion_api", "not-real-provider-it244"):
        result = _dispatch(name)
        assert (result.ok, result.verified, result.code) == (True, False, "no_tester"), name
    assert captured == {}


def test_spellings_the_runtime_cannot_match_stay_no_tester(monkeypatch):
    # Decision "runtime-exact": `todoist-api` and `perplexity-api` are dash
    # variants of ALIASES. `get_provider_spec` resolves them, but the vault
    # lookup (`provider_candidates`) never matches a record stored under them,
    # so every Todoist/Perplexity tool reports no credential. The Test button
    # must not call such a record verified.
    captured = _capture_probe(monkeypatch)
    for name in ("todoist-api", "perplexity-api", "tavily-api", "searx-ng"):
        result = _dispatch(name)
        assert result.code == "no_tester", name
    assert captured == {}


def test_alias_of_an_llm_named_spec_without_a_tester_is_not_llm_probed(monkeypatch):
    # When the canonical provider has no tester, the LLM fallback still keys
    # on the STORED name, never the canonical one. The aws spec shows why:
    # `aws` is also an LLM provider id, but its records (and those of its `s3`
    # alias) are S3 access keys, which an openai-compatible `/models` probe
    # would send as a Bearer token to an LLM host.
    captured = _capture_probe(monkeypatch)
    for name in ("s3", "aws_s3", "openai_api"):
        assert _dispatch(name).code == "no_tester", name
    assert captured == {}


def test_perplexity_aliases_resolve_through_the_spec_not_registrations():
    # The per-alias registrations the Perplexity fix added are gone, so the
    # all-aliases dispatch test above now passes only through spec resolution.
    # anthropic_direct (no spec) keeps its own.
    assert "perplexity" in credential_tests._TESTERS
    assert "perplexity_api" not in credential_tests._TESTERS
    assert "pplx" not in credential_tests._TESTERS
    assert credential_tests._TESTERS["anthropic_direct"] is credential_tests._test_anthropic
