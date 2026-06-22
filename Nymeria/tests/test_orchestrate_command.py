"""Tests for /orchestrate slash command registration.

The actual chat-stream execution lives in api/routers/chat.py and is exercised
via the Docker stack integration check; these tests verify only that the
command service exposes the right registry shape and refuses to dispatch the
command directly (it is handled by the chat endpoint).
"""

from __future__ import annotations


from cli_fixtures import run
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
)


class _FakeApi:
    """No-op API stand-in; not invoked for chat-stream commands."""


def test_orchestrate_root_is_chat_stream_and_lists_correctly() -> None:
    service = CommandService()
    cmds = service.list_commands(actor="user", surface="desktop", is_admin=False)
    orch = next(c for c in cmds if c.id == "orchestrate")
    assert orch.execution_kind == "chat_stream"
    assert orch.requires_thread is True
    assert orch.mutates_state is True
    assert orch.category == "Orchestration"


def test_orchestrate_subcommands_registered() -> None:
    service = CommandService()
    paths = {tuple(p): cid for p, cid in service._path_index.items()}
    assert ("orchestrate",) in paths
    assert ("orchestrate", "clear") in paths
    assert ("orchestrate", "status") in paths


def test_orchestrate_clear_alias_resolves() -> None:
    service = CommandService()
    aliases = service._aliases
    assert ("orchestrate_clear",) in aliases
    assert ("orchestrate_status",) in aliases


def test_orchestrate_is_not_executed_by_command_service() -> None:
    """All /orchestrate variants are chat_stream — the command service rejects them."""
    service = CommandService()
    for raw in ("/orchestrate make pizza", "/orchestrate clear", "/orchestrate status"):
        result = run(
            service.execute(
                CommandContext(
                    user_id="alice",
                    thread_id="thread-1",
                    actor="user",
                    surface="desktop",
                    is_admin=True,
                ),
                raw,
                api=_FakeApi(),
            )
        )
        assert result.success is False, raw
        assert "handled outside the command service" in result.markdown, raw


def test_orchestrate_requires_thread() -> None:
    """Without an active thread, /orchestrate variants are rejected pre-dispatch."""
    service = CommandService()
    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id=None,
                actor="user",
                surface="desktop",
                is_admin=True,
            ),
            "/orchestrate test",
            api=_FakeApi(),
        )
    )
    assert result.success is False
    assert "requires an active thread" in result.markdown.lower()
