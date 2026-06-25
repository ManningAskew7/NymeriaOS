"""Direct unit tests for the module-level webhook-fire helpers extracted from
``fire_trigger`` (slice 23 F4).

``_dispatch_trigger_fire`` is the daemon-thread body that previously lived as a
nested ``_fire`` closure and had no direct coverage: every HTTP-level test in
``test_trigger_webhook_security.py`` runs with ``nymeria_service_token=None``,
so the body short-circuits before the ``POST /chat`` call. These tests exercise
the dispatch body (missing-token early return, success, queued, error) and the
secret-resolution branching (500/404/403/409/happy) directly.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerExecution,
)
from nymeria.triggers.trigger_api import (
    _dispatch_trigger_fire,
    _resolve_webhook_trigger,
)
from nymeria.triggers.sources.webhook_source import WebhookSource


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

def _webhook_trigger(trigger_id: str, name: str, secret: str) -> TriggerDefinition:
    return TriggerDefinition(
        id=trigger_id,
        name=name,
        source_type="webhook",
        source_config={"secret": secret} if secret else {},
        action=TriggerAction(type="agent_prompt", config={"prompt_template": "hi"}),
        thread_id=f"trigger-{trigger_id}",
        created_by="user",
    )


class _FakeManager:
    """Minimal TriggerManager stand-in for the two helpers under test."""

    def __init__(self, by_id: Optional[List[Tuple[str, TriggerDefinition]]] = None) -> None:
        self._by_id = by_id or []
        self.logged: List[Tuple[str, TriggerExecution]] = []

    def find_triggers_by_id(self, trigger_id: str) -> List[Tuple[str, TriggerDefinition]]:
        return [(owner, t) for owner, t in self._by_id if t.id == trigger_id]

    def log_execution(self, user_id: str, execution: TriggerExecution) -> None:
        self.logged.append((user_id, execution))


class _FakeResponse:
    def __init__(self, lines: List[str], raise_exc: Optional[Exception] = None) -> None:
        self._lines = lines
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    def iter_lines(self):
        yield from self._lines


class _FakeStreamCtx:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeClient:
    """Stands in for ``httpx.Client``; records the single ``stream`` call."""

    def __init__(self, response: _FakeResponse, recorder: dict) -> None:
        self._response = response
        self._recorder = recorder

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def stream(self, method: str, url: str, *, headers=None, json=None) -> _FakeStreamCtx:
        self._recorder.update(method=method, url=url, headers=headers, json=json)
        return _FakeStreamCtx(self._response)


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse, recorder: dict
) -> None:
    """Patch ``httpx.Client`` on the real module so the helper's function-local
    ``import httpx`` observes the fake (same ``sys.modules['httpx']`` object)."""

    def _factory(*_args: Any, **_kwargs: Any) -> _FakeClient:
        recorder["client_kwargs"] = _kwargs
        return _FakeClient(response, recorder)

    monkeypatch.setattr("httpx.Client", _factory)


def _settings(token: Optional[str] = "svc-token", api_port: int = 8000) -> Any:
    return SimpleNamespace(nymeria_service_token=token, api_port=api_port)


# ---------------------------------------------------------------------------
# _resolve_webhook_trigger
# ---------------------------------------------------------------------------

def test_resolve_webhook_trigger_returns_owner_and_trigger(monkeypatch):
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: WebhookSource())
    trigger = _webhook_trigger("wh1", "Webhook", "shared")
    manager: Any = _FakeManager([("owner", trigger)])

    owner, resolved = _resolve_webhook_trigger(manager, "wh1", "shared")

    assert owner == "owner"
    assert resolved is trigger


def test_resolve_webhook_trigger_raises_500_when_source_unavailable(monkeypatch):
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: None)
    manager: Any = _FakeManager([("owner", _webhook_trigger("wh1", "Webhook", "shared"))])

    with pytest.raises(HTTPException) as exc:
        _resolve_webhook_trigger(manager, "wh1", "shared")
    assert exc.value.status_code == 500
    assert exc.value.detail == "Webhook source unavailable"


def test_resolve_webhook_trigger_raises_404_when_no_candidate(monkeypatch):
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: WebhookSource())
    manager: Any = _FakeManager([])

    with pytest.raises(HTTPException) as exc:
        _resolve_webhook_trigger(manager, "missing", "shared")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Trigger not found"


def test_resolve_webhook_trigger_raises_403_on_wrong_secret(monkeypatch):
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: WebhookSource())
    manager: Any = _FakeManager([("owner", _webhook_trigger("wh1", "Webhook", "shared"))])

    with pytest.raises(HTTPException) as exc:
        _resolve_webhook_trigger(manager, "wh1", "wrong")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid or missing webhook secret"


def test_resolve_webhook_trigger_raises_409_when_ambiguous(monkeypatch):
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: WebhookSource())
    manager: Any = _FakeManager(
        [
            ("owner_a", _webhook_trigger("dup", "A", "shared")),
            ("owner_b", _webhook_trigger("dup", "B", "shared")),
        ]
    )

    with pytest.raises(HTTPException) as exc:
        _resolve_webhook_trigger(manager, "dup", "shared")
    assert exc.value.status_code == 409
    assert "Ambiguous" in exc.value.detail


def test_resolve_webhook_trigger_secretless_is_not_fireable(monkeypatch):
    # A webhook trigger with no configured secret can never match (the real
    # WebhookSource.validate_secret rejects empty config) -> 403, not 200.
    monkeypatch.setattr("nymeria.triggers.sources.get_source", lambda name: WebhookSource())
    manager: Any = _FakeManager([("owner", _webhook_trigger("wh1", "Webhook", ""))])

    with pytest.raises(HTTPException) as exc:
        _resolve_webhook_trigger(manager, "wh1", None)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# _dispatch_trigger_fire
# ---------------------------------------------------------------------------

def test_dispatch_returns_early_and_logs_nothing_without_service_token(caplog):
    manager: Any = _FakeManager()

    with caplog.at_level(logging.ERROR, logger="nymeria.triggers.trigger_api"):
        _dispatch_trigger_fire(
            settings=_settings(token=None),
            manager=manager,
            user_id="owner",
            thread_id="trigger-wh1",
            prompt="hello",
            trigger_id="wh1",
            trigger_name="Webhook",
            action_type="agent_prompt",
            event_summary="{}",
        )

    # Early return is BEFORE the try/finally, so no execution is logged.
    assert manager.logged == []
    assert "NYMERIA_SERVICE_TOKEN is not" in caplog.text


def test_dispatch_success_logs_execution_and_sends_expected_request(monkeypatch):
    manager: Any = _FakeManager()
    recorder: dict = {}
    # Non-prompt_queued SSE lines plus noise keep was_queued False -> "success".
    response = _FakeResponse(lines=[": keepalive", 'data: {"type": "token"}', ""])
    _patch_httpx_client(monkeypatch, response, recorder)

    _dispatch_trigger_fire(
        settings=_settings(),
        manager=manager,
        user_id="owner",
        thread_id="trigger-wh1",
        prompt="hello",
        trigger_id="wh1",
        trigger_name="Webhook",
        action_type="agent_prompt",
        event_summary="{'message': 'hi'}",
    )

    assert len(manager.logged) == 1
    logged_user, execution = manager.logged[0]
    assert logged_user == "owner"
    assert execution.status == "success"
    assert execution.duration_seconds >= 0.0
    assert execution.events_summary == "{'message': 'hi'}"
    assert execution.action_type == "agent_prompt"

    # Request shape contract.
    assert recorder["method"] == "POST"
    assert recorder["url"] == "http://localhost:8000/chat"
    assert recorder["headers"]["Authorization"] == "Bearer svc-token"
    assert recorder["headers"]["X-Nymeria-Act-As"] == "owner"
    body = recorder["json"]
    assert body["message"] == "hello"
    assert body["thread_id"] == "trigger-wh1"
    assert body["is_self_invoke"] is True
    assert body["source"] == "trigger"
    assert body["source_id"] == "wh1"


def test_dispatch_marks_queued_when_prompt_queued_event_seen(monkeypatch):
    manager: Any = _FakeManager()
    recorder: dict = {}
    response = _FakeResponse(lines=['data: {"type": "prompt_queued"}'])
    _patch_httpx_client(monkeypatch, response, recorder)

    _dispatch_trigger_fire(
        settings=_settings(),
        manager=manager,
        user_id="owner",
        thread_id="trigger-wh1",
        prompt="hello",
        trigger_id="wh1",
        trigger_name="Webhook",
        action_type="agent_prompt",
        event_summary="{}",
    )

    assert len(manager.logged) == 1
    assert manager.logged[0][1].status == "queued"


def test_dispatch_marks_error_and_still_logs_execution_on_failure(monkeypatch):
    manager: Any = _FakeManager()
    recorder: dict = {}
    response = _FakeResponse(lines=[], raise_exc=RuntimeError("backend exploded"))
    _patch_httpx_client(monkeypatch, response, recorder)

    _dispatch_trigger_fire(
        settings=_settings(),
        manager=manager,
        user_id="owner",
        thread_id="trigger-wh1",
        prompt="hello",
        trigger_id="wh1",
        trigger_name="Webhook",
        action_type="agent_prompt",
        event_summary="{}",
    )

    # finally: still records the execution even though the request raised.
    assert len(manager.logged) == 1
    execution = manager.logged[0][1]
    assert execution.status == "error"
    assert execution.error_message is not None
    assert "backend exploded" in execution.error_message


def test_dispatch_truncates_long_error_message_to_200_chars(monkeypatch):
    manager: Any = _FakeManager()
    recorder: dict = {}
    long_msg = "x" * 500
    response = _FakeResponse(lines=[], raise_exc=RuntimeError(long_msg))
    _patch_httpx_client(monkeypatch, response, recorder)

    _dispatch_trigger_fire(
        settings=_settings(),
        manager=manager,
        user_id="owner",
        thread_id="trigger-wh1",
        prompt="hello",
        trigger_id="wh1",
        trigger_name="Webhook",
        action_type="agent_prompt",
        event_summary="{}",
    )

    execution = manager.logged[0][1]
    assert execution.error_message is not None
    assert len(execution.error_message) == 200
