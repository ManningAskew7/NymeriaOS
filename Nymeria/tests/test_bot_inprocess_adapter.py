"""Direct unit coverage for the shared ``InProcessBotAPI`` webhook-bot adapter.

This adapter is the collapse of the seven previously-verbatim
``InProcess<Platform>API`` classes (optimization slice 11 F1). The seven per-bot
router tests only exercise the webhook plumbing (signature/handshake/503/403),
never the adapter body, so these tests are the primary coverage of the moved
logic: the injected ``error_cls`` (so a native bot's ``except BotAPIError`` keeps
matching), the per-platform ``origin_client_id`` stamped on sync events, and the
``display_name`` rendered in the best-effort debug breadcrumbs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from nymeria.api.routers import _bot_inprocess
from nymeria.api.routers._bot_inprocess import InProcessBotAPI
from nymeria.core.chat_bindings import BindCodeInvalid, BindingAlreadyExists


class _SentinelError(Exception):
    """Stand-in for a platform's own ``BotAPIError`` (same constructor shape)."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [chunk async for chunk in agen]


class _FakeUser:
    def __init__(self, uid: str = "u1", disabled: bool = False) -> None:
        self.id = uid
        self.email = "u@example.com"
        self.display_name = "U"
        self.role = "user"
        self.disabled = disabled


class _FakeAccountsRepo:
    def __init__(self) -> None:
        self.platforms: dict[tuple[str, str], str] = {}
        self.user: _FakeUser | None = _FakeUser()
        self.linked: list[tuple[str, str, str]] = []
        self.raise_user_not_found_on_link = False

    def resolve_platform(self, provider: str, platform_user_id: str):
        return self.platforms.get((provider, platform_user_id))

    def link_platform(self, provider: str, platform_user_id: str, user_id: str) -> None:
        if self.raise_user_not_found_on_link:
            from nymeria.core.accounts import UserNotFound

            raise UserNotFound(user_id)
        self.linked.append((provider, platform_user_id, user_id))
        self.platforms[(provider, platform_user_id)] = user_id

    def list_platforms_for_user(self, user_id: str):
        return [
            SimpleNamespace(provider=p, provider_user_id=pid, created_at="t0")
            for (p, pid), u in self.platforms.items()
            if u == user_id
        ]

    def get_user_by_id(self, user_id: str):
        return self.user


class _FakeBindingsRepo:
    def __init__(self) -> None:
        self.inspect_result = None
        self.inspect_raises = False
        self.claimed: list[tuple[str, str]] = []
        self.created = None
        self.create_raises_exists = False
        self.lookup_result = None
        self.deleted: list[int] = []
        self.global_bindings: list = []

    def inspect_bind_code(self, code: str, *, kind: str, provider: str):
        if self.inspect_raises:
            raise BindCodeInvalid("bad code")
        return self.inspect_result

    def claim_bind_code(self, code: str, *, kind: str, provider: str) -> None:
        self.claimed.append((code, kind))

    def create_thread_binding(self, *, thread_id, provider, platform_chat_id, user_id):
        if self.create_raises_exists:
            raise BindingAlreadyExists("already bound")
        self.created = SimpleNamespace(
            id=7, thread_id=thread_id, provider=provider,
            platform_chat_id=platform_chat_id, user_id=user_id,
        )
        return self.created

    def lookup_thread_binding_by_chat(self, provider: str, platform_chat_id: str):
        return self.lookup_result

    def delete_thread_binding(self, binding_id: int, *, user_id: str) -> bool:
        self.deleted.append(binding_id)
        return True

    def list_thread_bindings_global(self, *, provider: str):
        return self.global_bindings


class _FakeAgent:
    def __init__(self) -> None:
        self.accounts_repo = _FakeAccountsRepo()
        self.chat_bindings_repo = _FakeBindingsRepo()
        self.thread_metadata_manager = SimpleNamespace(
            auto_title=lambda user_id, thread_id, message: None,
            get_thread=lambda user_id, thread_id: {"platform": "x"},
        )
        self.settings = SimpleNamespace(llm_model="default-model")
        self._thread_locks = SimpleNamespace(get_lock_info=lambda thread_id: None)
        self._last_chat_tool_calls = 3
        self._stream_chunks: list = ["a", "b"]
        self._context_stats_raises = False

    async def astream(self, message, *, thread_id, user_id):
        for chunk in self._stream_chunks:
            yield chunk

    def get_context_stats(self, thread_id):
        if self._context_stats_raises:
            raise RuntimeError("boom")
        return {"tokens": 1}

    def _get_llm_config_for_thread(self, thread_id):
        return SimpleNamespace(model="thread-model")

    def chat(self, message, *, thread_id, user_id):
        return "the-response"

    def abort_with_cascade(self, thread_id):
        self.aborted = thread_id


def _make_adapter(agent=None, *, origin="testplat", display="TestPlatform", events=None):
    agent = agent or _FakeAgent()

    def publish(**kwargs):
        if events is not None:
            events.append(kwargs)

    api = InProcessBotAPI(
        agent=agent,
        require_thread_access_fn=lambda authed, thread_id: None,
        publish_sync_event_fn=publish,
        origin_client_id=origin,
        display_name=display,
        error_cls=_SentinelError,
    )
    return api, agent


# --- error_cls injection (the security-sensitive raises) --------------------

def test_claim_platform_link_invalid_code_raises_injected_error():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_raises = True
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_platform_link_code(code="c", provider="whatsapp", platform_user_id="p"))
    assert exc.value.status_code == 400
    assert "Invalid code" in exc.value.detail


def test_claim_platform_link_already_linked_to_other_user_409():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id=None)
    agent.accounts_repo.platforms[("whatsapp", "p")] = "someone-else"
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_platform_link_code(code="c", provider="whatsapp", platform_user_id="p"))
    assert exc.value.status_code == 409


def test_claim_platform_link_user_not_found_404():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id=None)
    agent.accounts_repo.raise_user_not_found_on_link = True
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_platform_link_code(code="c", provider="whatsapp", platform_user_id="p"))
    assert exc.value.status_code == 404


def test_claim_platform_link_linked_but_not_found_500():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id=None)
    # link succeeds but the subsequent re-find never returns the (provider, pid) pair
    agent.accounts_repo.list_platforms_for_user = lambda user_id: []
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_platform_link_code(code="c", provider="whatsapp", platform_user_id="p"))
    assert exc.value.status_code == 500


def test_claim_platform_link_success_returns_record():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id=None)
    result = _run(api.claim_platform_link_code(code="c", provider="whatsapp", platform_user_id="p"))
    assert result["user_id"] == "owner"
    assert result["provider_user_id"] == "p"
    assert agent.chat_bindings_repo.claimed == [("c", "platform_link")]


def test_claim_thread_bind_no_thread_id_500():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id=None)
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_thread_bind_code(
            code="c", provider="teams", platform_chat_id="chat",
            expected_provider_user_id="pu"))
    assert exc.value.status_code == 500


def test_claim_thread_bind_different_account_403():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id="t1")
    agent.accounts_repo.platforms[("teams", "pu")] = "different-owner"
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_thread_bind_code(
            code="c", provider="teams", platform_chat_id="chat",
            expected_provider_user_id="pu"))
    assert exc.value.status_code == 403


def test_claim_thread_bind_already_exists_409():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id="t1")
    agent.accounts_repo.platforms[("teams", "pu")] = "owner"
    agent.chat_bindings_repo.create_raises_exists = True
    with pytest.raises(_SentinelError) as exc:
        _run(api.claim_thread_bind_code(
            code="c", provider="teams", platform_chat_id="chat",
            expected_provider_user_id="pu"))
    assert exc.value.status_code == 409


def test_claim_thread_bind_success_publishes_origin_client_id(monkeypatch):
    events: list[dict] = []
    api, agent = _make_adapter(origin="teams", events=events)
    monkeypatch.setattr(_bot_inprocess, "_thread_list_platform", lambda agent, tid, meta: "teams")
    agent.chat_bindings_repo.inspect_result = SimpleNamespace(user_id="owner", thread_id="t1")
    agent.accounts_repo.platforms[("teams", "pu")] = "owner"
    result = _run(api.claim_thread_bind_code(
        code="c", provider="teams", platform_chat_id="chat",
        expected_provider_user_id="pu"))
    assert result["binding_id"] == 7
    # _publish_platform_sync fired with the injected origin_client_id
    assert events and all(e["origin_client_id"] == "teams" for e in events)


def test_unbind_cross_user_raises_injected_403():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.lookup_result = SimpleNamespace(
        id=9, thread_id="t1", user_id="real-owner")
    with pytest.raises(_SentinelError) as exc:
        _run(api.unbind_chatapp_by_chat(
            provider="line", platform_chat_id="chat", user_id="intruder"))
    assert exc.value.status_code == 403
    assert agent.chat_bindings_repo.deleted == []  # never deleted on the 403 path


def test_unbind_missing_binding_returns_unbound_false():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.lookup_result = None
    result = _run(api.unbind_chatapp_by_chat(provider="line", platform_chat_id="chat"))
    assert result == {"unbound": False}


def test_authenticated_user_missing_id_raises_injected_404():
    api, _ = _make_adapter()
    with pytest.raises(_SentinelError) as exc:
        api._authenticated_user(None)
    assert exc.value.status_code == 404


def test_authenticated_user_disabled_raises_injected_404():
    api, agent = _make_adapter()
    agent.accounts_repo.user = _FakeUser(disabled=True)
    with pytest.raises(_SentinelError) as exc:
        api._authenticated_user("u1")
    assert exc.value.status_code == 404


# --- origin_client_id + display_name on the streaming path ------------------

def test_chat_stream_stamps_origin_client_id_and_yields_done():
    events: list[dict] = []
    api, agent = _make_adapter(origin="googlechat", events=events)
    agent.thread_metadata_manager.auto_title = lambda user_id, thread_id, message: "A Title"
    chunks = _run(_collect(api.chat_stream("hi", "t1", "u1")))
    # the user message_added event is stamped first
    assert events[0]["event_type"] == "message_added"
    assert events[0]["origin_client_id"] == "googlechat"
    # auto-title thread_updated also stamped
    title_events = [e for e in events if e["event_type"] == "thread_updated"]
    assert title_events and title_events[0]["origin_client_id"] == "googlechat"
    # the stream forwards agent chunks then a terminal done dict
    assert chunks[:-1] == ["a", "b"]
    done = chunks[-1]
    assert done["type"] == "done"
    assert done["model"] == "thread-model"
    assert done["title"] == "A Title"


def test_chat_stream_done_metadata_failure_logs_display_name(caplog):
    api, agent = _make_adapter(display="Google Chat")
    agent._context_stats_raises = True
    with caplog.at_level(logging.DEBUG, logger="nymeria.api.routers._bot_inprocess"):
        _run(_collect(api.chat_stream("hi", "t1", "u1")))
    assert any("Google Chat done metadata failed" in r.message for r in caplog.records)


def test_chat_stream_auto_title_failure_logs_display_name(caplog):
    api, agent = _make_adapter(display="Webex")

    def boom(user_id, thread_id, message):
        raise RuntimeError("nope")

    agent.thread_metadata_manager.auto_title = boom
    with caplog.at_level(logging.DEBUG, logger="nymeria.api.routers._bot_inprocess"):
        _run(_collect(api.chat_stream("hi", "t1", "u1")))
    assert any("Webex auto-title failed" in r.message for r in caplog.records)


def test_publish_platform_sync_failure_logs_display_name(caplog):
    api, agent = _make_adapter(display="LINE")

    def boom(user_id, thread_id):
        raise RuntimeError("nope")

    agent.thread_metadata_manager.get_thread = boom
    with caplog.at_level(logging.DEBUG, logger="nymeria.api.routers._bot_inprocess"):
        api._publish_platform_sync("t1", "u1")
    assert any("LINE platform sync publish failed" in r.message for r in caplog.records)


def test_publish_platform_sync_uses_thread_list_platform(monkeypatch):
    events: list[dict] = []
    api, agent = _make_adapter(origin="webex", events=events)
    monkeypatch.setattr(_bot_inprocess, "_thread_list_platform", lambda agent, tid, meta: "resolved")
    api._publish_platform_sync("t1", "u1")
    assert events[0]["data"] == {"platform": "resolved"}
    assert events[0]["origin_client_id"] == "webex"


# --- the trivial delegators / happy paths -----------------------------------

def test_stop_idle_when_no_lock():
    api, agent = _make_adapter()
    result = _run(api.stop("t1", "u1"))
    assert result == {"status": "idle", "thread_id": "t1"}


def test_stop_stopping_when_locked():
    api, agent = _make_adapter()
    agent._thread_locks.get_lock_info = lambda thread_id: {"held": True}
    result = _run(api.stop("t1", "u1"))
    assert result == {"status": "stopping", "thread_id": "t1"}
    assert agent.aborted == "t1"


def test_chat_returns_response_and_tool_count():
    api, agent = _make_adapter()
    result = _run(api.chat("hi", "t1", "u1"))
    assert result == {"response": "the-response", "thread_id": "t1", "tool_call_count": 3}


def test_list_chatapp_bindings_maps_fields():
    api, agent = _make_adapter()
    agent.chat_bindings_repo.global_bindings = [
        SimpleNamespace(
            id=1, thread_id="t1", provider="line", platform_chat_id="c1",
            user_id="u1", created_at="t0", user_telegram_bot_id=None,
        )
    ]
    result = _run(api.list_chatapp_bindings("line"))
    assert result == [{
        "id": 1, "thread_id": "t1", "provider": "line", "platform_chat_id": "c1",
        "user_id": "u1", "created_at": "t0", "user_telegram_bot_id": None,
    }]
