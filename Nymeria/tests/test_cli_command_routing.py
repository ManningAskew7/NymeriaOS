"""Unit tests for the shared CLI command-routing helpers.

These helpers were previously copy-pasted into both the Rich REPL (``app.py``)
and the legacy full-screen shell; they now live in one module so the two shells
classify commands identically.
"""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.triggers.cli.command_routing import (
    chat_stream_command_from_result,
    is_chat_stream_command,
    queued_notice,
)


def test_queued_notice_singular_and_plural():
    assert queued_notice(1) == "Queued message (1)"
    assert queued_notice(0) == "Queued messages (0)"
    assert queued_notice(3) == "Queued messages (3)"


def test_chat_stream_command_from_result_extracts_and_strips():
    result = SimpleNamespace(payload={"chat_stream_command": "  /ask hello  "})
    assert chat_stream_command_from_result(result) == "/ask hello"


def test_chat_stream_command_from_result_handles_missing_payload():
    assert chat_stream_command_from_result(SimpleNamespace(payload=None)) == ""
    assert chat_stream_command_from_result(SimpleNamespace()) == ""
    assert chat_stream_command_from_result(SimpleNamespace(payload={})) == ""


class _Registry:
    def __init__(self, execution_kind, *, raises=False):
        self._execution_kind = execution_kind
        self._raises = raises

    def resolve(self, raw_input):
        if self._raises:
            raise ValueError("no such command")
        command = SimpleNamespace(metadata={"execution_kind": self._execution_kind})
        return SimpleNamespace(command=command)


def test_is_chat_stream_command_true_for_chat_stream_kind():
    assert is_chat_stream_command(_Registry("chat_stream"), "/ask") is True


def test_is_chat_stream_command_false_for_other_kinds():
    assert is_chat_stream_command(_Registry("sync"), "/threads") is False
    assert is_chat_stream_command(_Registry(None), "/threads") is False


def test_is_chat_stream_command_false_when_resolution_raises():
    assert is_chat_stream_command(_Registry("chat_stream", raises=True), "/x") is False


def test_is_chat_stream_command_false_when_command_missing():
    registry = SimpleNamespace(resolve=lambda raw: SimpleNamespace(command=None))
    assert is_chat_stream_command(registry, "/x") is False
