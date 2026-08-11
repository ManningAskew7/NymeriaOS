"""Tests for the LLM provider compatibility suite."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from nymeria.core.llm_provider_test_suite import (
    ProviderTestSuiteOptions,
    _resolve_credentials,
    run_provider_test_suite,
)

from test_api_settings_router import FakeAsyncClient, _auth, _client


class SequenceAsyncClient:
    """Small httpx.AsyncClient test double with queued responses."""

    responses: list[tuple[int, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    @classmethod
    def queue(cls, *responses: tuple[int, dict[str, Any]]) -> None:
        cls.responses = list(responses)
        cls.calls = []

    def _next_response(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self.responses:
            raise AssertionError(f"No queued response for {method} {url}")
        status, body = self.responses.pop(0)
        self.calls.append({
            "method": method,
            "url": url,
            "timeout": self.timeout,
            **kwargs,
        })
        request = httpx.Request(method, url)
        return httpx.Response(status, json=body, request=request)

    async def get(self, url: str, *, headers: dict[str, str]):
        return self._next_response("GET", url, headers=headers)

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
        return self._next_response("POST", url, headers=headers, json=json)


def _patch_suite_client(monkeypatch, factory: Callable[..., Any] = SequenceAsyncClient) -> None:
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.httpx.AsyncClient",
        factory,
    )


async def _run(options: ProviderTestSuiteOptions):
    return await run_provider_test_suite(options)


def test_provider_suite_selects_free_model_and_verifies_chat_and_tools(monkeypatch):
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue(
        (
            200,
            {
                "data": [
                    {
                        "id": "paid-model",
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    },
                    {
                        "id": "free-model:free",
                        "name": "Free Model",
                        "context_length": 8192,
                        "top_provider": {"max_completion_tokens": 1024},
                        "supported_parameters": ["tools", "temperature"],
                        "pricing": {"prompt": "0", "completion": "0"},
                    },
                ]
            },
        ),
        (
            200,
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        ),
        (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "nymeria_provider_ping",
                                        "arguments": "{\"ok\":true}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        ),
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="openrouter",
            api_key="sk-test-secret",
            api_mode="chat_completions",
        ),
    )

    assert report.ok is True
    assert report.model == "free-model:free"
    assert report.models_count == 2
    assert [step.status for step in report.steps] == [
        "passed",
        "passed",
        "passed",
        "passed",
        "passed",
    ]
    assert SequenceAsyncClient.calls[0]["url"] == "https://openrouter.ai/api/v1/models"
    assert SequenceAsyncClient.calls[1]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert SequenceAsyncClient.calls[2]["json"]["tool_choice"]["function"]["name"] == "nymeria_provider_ping"


def test_provider_suite_skips_billable_generation_without_explicit_allowance(monkeypatch):
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue(
        (
            200,
            {
                "data": [
                    {
                        "id": "paid-model",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    }
                ]
            },
        ),
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="openrouter",
            model="paid-model",
            api_key="sk-test-secret",
            allow_billable=False,
        ),
    )

    assert report.ok is False
    assert report.model == "paid-model"
    assert [step.name for step in report.steps] == [
        "configuration",
        "model_list",
        "model_selection",
        "chat_completion",
        "tool_call",
    ]
    assert report.steps[3].status == "skipped"
    assert len(SequenceAsyncClient.calls) == 1


def test_provider_suite_reports_unknown_provider_without_base_url(monkeypatch):
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue()

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="not-a-real-provider",
            api_key="sk-test-secret",
        ),
    )

    assert report.ok is False
    assert report.steps[0].error_type == "unknown_provider"
    assert SequenceAsyncClient.calls == []


def test_provider_suite_missing_api_key_fails_authentication(monkeypatch):
    # A hosted provider with no request key, no vault credential, and no
    # settings/env key must short-circuit on the authentication guard before any
    # network call. Locks the missing_api_key early-return after the F4 split.
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue()
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.get_llm_provider_credential",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.resolve_provider_api_key",
        lambda *a, **k: None,
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(provider="openrouter"),
    )

    assert report.ok is False
    assert report.credential_source == "none"
    assert [step.name for step in report.steps] == ["authentication"]
    assert report.steps[0].error_type == "missing_api_key"
    assert SequenceAsyncClient.calls == []


def test_provider_suite_no_selectable_model_fails(monkeypatch):
    # Custom (unregistered) endpoint, no requested model, empty /models, and no
    # spec default -> nothing to select. Locks the missing_model early-return and
    # exercises the custom-provider base-URL branch of _resolve_effective_base_url.
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue((200, {"data": []}))

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="custom-endpoint",
            base_url="https://custom.example/v1",
            api_key="sk-test-secret",
        ),
    )

    assert report.ok is False
    assert report.model is None
    assert report.models_count == 0
    assert [step.name for step in report.steps] == [
        "configuration",
        "model_list",
        "model_selection",
    ]
    assert report.steps[-1].error_type == "missing_model"
    assert len(SequenceAsyncClient.calls) == 1


def test_provider_suite_anthropic_base_url_falls_back_to_default(monkeypatch):
    # Anthropic resolves through the dedicated branch of
    # _resolve_effective_base_url; with no configured base URL it falls back to
    # the public endpoint. No live checks requested, so the run passes cleanly.
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue()
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.resolve_provider_base_url",
        lambda *a, **k: None,
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="anthropic",
            model="claude-test",
            api_key="sk-ant-test",
            run_model_list=False,
            run_chat_completion=False,
            run_tool_call=False,
        ),
    )

    assert report.ok is True
    assert report.effective_base_url == "https://api.anthropic.com"
    assert [step.name for step in report.steps] == [
        "configuration",
        "model_selection",
    ]
    assert SequenceAsyncClient.calls == []


def test_provider_suite_cliproxy_anthropic_chat_carries_billing_block(monkeypatch):
    # #161: the suite's anthropic chat probe used to send no OAuth billing
    # fingerprint, so premium Claude models 429'd here while /provider test
    # (which adds the block) passed: two surfaces disagreeing about the same
    # provider. The block must ride the wire payload, exactly once and first.
    from nymeria.vendor.react_agent.cliproxy import CLIPROXY_BILLING_SYSTEM_BLOCK

    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue(
        (200, {"content": [{"type": "text", "text": "ok"}]}),
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="anthropic",
            model="claude-opus-4-8",
            api_key="sk-ant-test",
            base_url="http://cli-proxy-api:8317",
            allow_billable=True,
            run_model_list=False,
            run_chat_completion=True,
            run_tool_call=False,
        ),
    )

    assert report.ok is True
    chat_call = SequenceAsyncClient.calls[0]
    assert chat_call["url"] == "http://cli-proxy-api:8317/v1/messages"
    assert chat_call["json"]["system"][0] == CLIPROXY_BILLING_SYSTEM_BLOCK
    import json as jsonlib

    assert jsonlib.dumps(chat_call["json"]).count("x-anthropic-billing-header") == 1
    # Full production identity, not just the body: the cloak-skip User-Agent
    # and the safe Anthropic-Beta override ride the probe headers too
    # (shared provider_probe_headers, so this surface cannot drift from
    # /provider test).
    assert chat_call["headers"]["User-Agent"].startswith("claude-cli/")
    assert "Anthropic-Beta" in chat_call["headers"]


def test_provider_suite_direct_anthropic_chat_has_no_billing_block(monkeypatch):
    # The fingerprint is CLIProxy-only: a direct api.anthropic.com probe must
    # not carry it.
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue(
        (200, {"content": [{"type": "text", "text": "ok"}]}),
    )

    import anyio

    report = anyio.run(
        _run,
        ProviderTestSuiteOptions(
            provider="anthropic",
            model="claude-opus-4-8",
            api_key="sk-ant-test",
            base_url="https://api.anthropic.com",
            allow_billable=True,
            run_model_list=False,
            run_chat_completion=True,
            run_tool_call=False,
        ),
    )

    assert report.ok is True
    chat_call = SequenceAsyncClient.calls[0]
    import json as jsonlib

    assert "x-anthropic-billing-header" not in jsonlib.dumps(chat_call["json"])
    # The CLIProxy-only Anthropic-Beta override must not reach a direct base.
    assert "Anthropic-Beta" not in chat_call["headers"]


def test_resolve_credentials_seeds_base_url_from_vault(monkeypatch):
    # No request key: the vault credential supplies the key, the credential
    # source, and (since no request base URL) the base URL.
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.get_llm_provider_credential",
        lambda *a, **k: SimpleNamespace(
            api_key="sk-vault",
            credential_id="cred-123",
            base_url="https://vault.example/v1",
        ),
    )

    api_key, base_url, source = _resolve_credentials(
        ProviderTestSuiteOptions(provider="openrouter"), "openrouter"
    )

    assert api_key == "sk-vault"
    assert base_url == "https://vault.example/v1"
    assert source == "vault:cred-123"


def test_resolve_credentials_keeps_request_base_url_over_vault(monkeypatch):
    # A request-supplied base URL is not overwritten by the vault credential.
    monkeypatch.setattr(
        "nymeria.core.llm_provider_test_suite.get_llm_provider_credential",
        lambda *a, **k: SimpleNamespace(
            api_key="sk-vault",
            credential_id="cred-123",
            base_url="https://vault.example/v1",
        ),
    )

    api_key, base_url, source = _resolve_credentials(
        ProviderTestSuiteOptions(
            provider="openrouter", base_url="https://req.example/v1"
        ),
        "openrouter",
    )

    assert api_key == "sk-vault"
    assert base_url == "https://req.example/v1"
    assert source == "vault:cred-123"


def test_provider_suite_endpoint_is_admin_only_and_does_not_call_provider(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = agent.accounts_repo.issue_token("alice")

    response = client.post(
        "/settings/llm/test-suite",
        headers=_auth(user_token),
        json={
            "llm_provider": "openrouter",
            "llm_model": "free-model:free",
            "api_key": "sk-test",
        },
    )

    assert response.status_code == 403
    assert FakeAsyncClient.calls == []


def test_provider_suite_endpoint_redacts_provider_errors(
    tmp_path: Path,
    monkeypatch,
):
    _patch_suite_client(monkeypatch)
    SequenceAsyncClient.queue(
        (401, {"error": {"message": "bad key sk-test-secret"}}),
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test-suite",
        headers=_auth(token),
        json={
            "llm_provider": "openrouter",
            "llm_model": "free-model:free",
            "api_key": "sk-test-secret",
            "run_chat_completion": False,
            "run_tool_call": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["steps"][1]["status"] == "failed"
    assert "sk-test-secret" not in body["steps"][1]["message"]
    assert "[redacted]" in body["steps"][1]["message"]
