"""Interactive-admission tests for the voice router (backlog #83).

The /voice/chat pipeline is STT -> agent turn -> TTS; the agent turn is an
interactive holder turn like any /chat message, so it draws against the
global interactive ceiling. These tests pin the shed shape (429 before the
agent runs), the busy-thread exemption, and slot release at turn end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nymeria.core import voice as voice_module
from nymeria.core.accounts import AccountsRepo
from nymeria.core.interactive_admission import (
    CAPACITY_DETAIL,
    get_interactive_turn_gate,
    reset_interactive_turn_gate_for_tests,
)


@pytest.fixture(autouse=True)
def _fresh_interactive_gate():
    """Isolate the process-global admission gate per test in this module."""
    reset_interactive_turn_gate_for_tests()
    yield
    reset_interactive_turn_gate_for_tests()


class _FakeSTT:
    async def transcribe(self, audio_bytes, *, filename=None, content_type=None):
        return "transcribed hello"


class _FakeTTS:
    async def synthesize(self, text: str, *, voice_note: bool = False):
        return b"fake-audio", "audio/mpeg"


@pytest.fixture(autouse=True)
def _fake_voice_services(monkeypatch: pytest.MonkeyPatch):
    # The route imports these function-locally from nymeria.core.voice, so
    # patching the module attributes covers every request.
    monkeypatch.setattr(voice_module, "get_stt_service", lambda settings: _FakeSTT())
    monkeypatch.setattr(voice_module, "get_tts_service", lambda settings: _FakeTTS())


class _FakeThreadLocks:
    def __init__(self) -> None:
        self.busy = False

    def is_thread_busy(self, thread_id: str) -> bool:
        return self.busy


class FakeVoiceAgent:
    def __init__(self, data_dir: Path) -> None:
        from nymeria.core.hook_manager import HookManager

        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.settings = SimpleNamespace(
            llm_provider="fallback-provider", llm_model="fallback-model"
        )
        self._thread_locks = _FakeThreadLocks()
        self.hook_manager = HookManager(data_dir)
        self.synced_tools = 0
        self.astream_calls: list[dict[str, Any]] = []

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1

    async def astream(self, message: str, **kwargs: Any):
        self.astream_calls.append({"message": message, **kwargs})
        yield {"type": "response", "content": "spoken reply"}


def _voice_client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(
        tmp_path,
        max_concurrent_interactive=1,
    )
    agent = FakeVoiceAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="alice"
    )
    return client, agent, token


def _post_voice_chat(client, api_client_builder, token, thread_id="voice-1"):
    return client.post(
        "/voice/chat",
        headers=api_client_builder.auth(token),
        files={"audio": ("clip.wav", b"riff-bytes", "audio/wav")},
        data={"thread_id": thread_id},
    )


def test_voice_chat_sheds_429_at_interactive_capacity(
    tmp_path: Path, api_client_builder
):
    client, agent, token = _voice_client(tmp_path, api_client_builder)
    held = get_interactive_turn_gate().try_acquire(1)
    assert held is not None

    response = _post_voice_chat(client, api_client_builder, token)

    # Shed AFTER transcription but BEFORE the agent turn starts: a plain
    # HTTP 429 with the documented detail and advisory Retry-After.
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "10"
    assert response.json() == {"detail": CAPACITY_DETAIL}
    assert agent.astream_calls == []
    held.release()


def test_voice_chat_releases_slot_at_turn_end(tmp_path: Path, api_client_builder):
    client, agent, token = _voice_client(tmp_path, api_client_builder)

    # Two sequential turns at limit 1: each must return its slot.
    for _ in range(2):
        response = _post_voice_chat(client, api_client_builder, token)
        assert response.status_code == 200
        assert response.content == b"fake-audio"

    assert len(agent.astream_calls) == 2
    assert get_interactive_turn_gate().active == 0


def test_voice_chat_busy_thread_bypasses_capacity(
    tmp_path: Path, api_client_builder
):
    # A prompt aimed at a busy thread queues onto the running holder turn
    # (no new concurrency), so it must pass through even at the ceiling.
    client, agent, token = _voice_client(tmp_path, api_client_builder)
    agent._thread_locks.busy = True
    held = get_interactive_turn_gate().try_acquire(1)
    assert held is not None

    response = _post_voice_chat(client, api_client_builder, token)

    assert response.status_code == 200
    assert len(agent.astream_calls) == 1
    held.release()
