"""Unit tests for the shared `/fast` temporary-model stash/restore protocol.

Covers `nymeria.triggers.cli.temporary_model`, the leaf both CLI shells delegate
their `_set_temporary_thread_model` / `_restore_temporary_thread_model` methods
to. The two shells previously carried byte-identical copies of this logic; these
tests lock the contract that the extraction must preserve.
"""

from __future__ import annotations

from typing import Any

from cli_fixtures import run
from nymeria.triggers.cli.commands import CommandContext
from nymeria.triggers.cli.temporary_model import (
    apply_temporary_model,
    restore_temporary_model,
)


class FakeThreadClient:
    """Minimal client exposing the three methods the protocol calls."""

    def __init__(
        self,
        *,
        thread_config: dict[str, Any] | None = None,
        default_model: str = "gpt-5.5",
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._thread_config = thread_config
        self._default_model = default_model

    async def get_thread_config(
        self, thread_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_thread_config", {"thread_id": thread_id, "user_id": user_id})
        )
        return dict(self._thread_config) if self._thread_config is not None else {}

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_settings", {"user_id": user_id}))
        return {"llm_model": self._default_model}

    async def update_thread_config(
        self, thread_id: str, *, user_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "update_thread_config",
                {"thread_id": thread_id, "user_id": user_id, **kwargs},
            )
        )
        return {"ok": True}


def _context(client: FakeThreadClient) -> CommandContext:
    return CommandContext(client=client, thread_id="thread-1", user_id="alice")


def _update_calls(client: FakeThreadClient) -> list[dict[str, Any]]:
    return [payload for name, payload in client.calls if name == "update_thread_config"]


def test_apply_stashes_existing_model_and_switches() -> None:
    client = FakeThreadClient(thread_config={"llm_config": {"model": "claude-opus-4-8"}})

    restore = run(apply_temporary_model(_context(client), "claude-haiku-4-5"))

    assert restore == {
        "llm_config_present": True,
        "model_present": True,
        "previous_model": "claude-opus-4-8",
        "effective_model": "claude-opus-4-8",
    }
    # The thread config was switched to the fast-turn model.
    assert _update_calls(client) == [
        {"thread_id": "thread-1", "user_id": "alice", "llm_config": {"model": "claude-haiku-4-5"}}
    ]


def test_apply_with_no_llm_config_falls_back_to_default_model() -> None:
    client = FakeThreadClient(thread_config={}, default_model="gpt-5.5")

    restore = run(apply_temporary_model(_context(client), "claude-haiku-4-5"))

    assert restore == {
        "llm_config_present": False,
        "model_present": False,
        "previous_model": None,
        "effective_model": "gpt-5.5",
    }


def test_apply_with_llm_config_but_no_model_uses_default_for_effective() -> None:
    # llm_config present (a Mapping) but without a "model" key.
    client = FakeThreadClient(
        thread_config={"llm_config": {"temperature": 0.5}}, default_model="gpt-5.5"
    )

    restore = run(apply_temporary_model(_context(client), "claude-haiku-4-5"))

    assert restore == {
        "llm_config_present": True,
        "model_present": False,
        "previous_model": None,
        "effective_model": "gpt-5.5",
    }


def test_restore_clears_config_when_none_was_present() -> None:
    client = FakeThreadClient()
    restore = {
        "llm_config_present": False,
        "model_present": False,
        "previous_model": None,
        "effective_model": "gpt-5.5",
    }

    run(restore_temporary_model(_context(client), restore))

    assert _update_calls(client) == [
        {"thread_id": "thread-1", "user_id": "alice", "clear_llm_config": True}
    ]


def test_restore_writes_back_previous_model() -> None:
    client = FakeThreadClient()
    restore = {
        "llm_config_present": True,
        "model_present": True,
        "previous_model": "claude-opus-4-8",
        "effective_model": "claude-opus-4-8",
    }

    run(restore_temporary_model(_context(client), restore))

    assert _update_calls(client) == [
        {"thread_id": "thread-1", "user_id": "alice", "llm_config": {"model": "claude-opus-4-8"}}
    ]


def test_restore_writes_none_model_when_config_present_without_model() -> None:
    client = FakeThreadClient()
    restore = {
        "llm_config_present": True,
        "model_present": False,
        "previous_model": None,
        "effective_model": "gpt-5.5",
    }

    run(restore_temporary_model(_context(client), restore))

    assert _update_calls(client) == [
        {"thread_id": "thread-1", "user_id": "alice", "llm_config": {"model": None}}
    ]


def test_apply_then_restore_round_trip_leaves_original_model() -> None:
    client = FakeThreadClient(thread_config={"llm_config": {"model": "claude-opus-4-8"}})
    context = _context(client)

    restore = run(apply_temporary_model(context, "claude-haiku-4-5"))
    run(restore_temporary_model(context, restore))

    # First update sets the fast model, second restores the original.
    assert _update_calls(client) == [
        {"thread_id": "thread-1", "user_id": "alice", "llm_config": {"model": "claude-haiku-4-5"}},
        {"thread_id": "thread-1", "user_id": "alice", "llm_config": {"model": "claude-opus-4-8"}},
    ]
