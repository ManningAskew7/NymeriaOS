"""Tests for the LLM provider compatibility suite."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from nymeria.core.llm_provider_test_suite import (
    ProviderTestSuiteOptions,
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
