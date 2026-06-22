"""Terminal-edge regression tests for the CLI/TUI.

T20 deliberately avoids adding a ``pexpect`` dependency. The stable coverage
here uses the stdlib ``pty`` module where available, plus prompt_toolkit
controller seams for interrupt and paste behavior.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import IO

import pytest

from cli_fixtures import DelayedEvent, FakeAgentClient, FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import detect_terminal_capabilities
from nymeria.triggers.cli.input import ComposerController, ComposerSubmission
from nymeria.triggers.cli.rendering.full_screen_legacy import (
    LegacyFullScreenPromptToolkitShell,
    LegacyFullScreenShellConfig,
)

pty = pytest.importorskip("pty")


def runtime_config(**overrides):
    return replace(CLIRuntimeConfig(), **overrides)


def _open_slave_streams(slave_fd: int) -> tuple[IO[bytes], IO[bytes], IO[bytes]]:
    return (
        os.fdopen(os.dup(slave_fd), "rb", buffering=0),
        os.fdopen(os.dup(slave_fd), "wb", buffering=0),
        os.fdopen(os.dup(slave_fd), "wb", buffering=0),
    )


def test_real_pty_streams_are_detected_as_full_screen_capable() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        stdin, stdout, stderr = _open_slave_streams(slave_fd)
        with stdin, stdout, stderr:
            # The legacy full-screen renderer is opt-in (auto now resolves to
            # rich); request it explicitly to assert a real PTY is full-capable.
            caps = detect_terminal_capabilities(
                runtime_config(renderer="full"),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                environ={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
            )
    finally:
        os.close(master_fd)
        os.close(slave_fd)

    assert caps.stdin_isatty is True
    assert caps.stdout_isatty is True
    assert caps.stderr_isatty is True
    assert caps.renderer == "full"
    assert caps.alt_screen_enabled is True
    assert caps.animation_enabled is True


def test_pipe_stdout_forces_plain_even_when_stdin_is_a_real_pty() -> None:
    master_fd, slave_fd = pty.openpty()
    read_fd, write_fd = os.pipe()
    try:
        stdin = os.fdopen(os.dup(slave_fd), "rb", buffering=0)
        stdout = os.fdopen(write_fd, "wb", buffering=0)
        stderr = os.fdopen(os.dup(slave_fd), "wb", buffering=0)
        with stdin, stdout, stderr:
            caps = detect_terminal_capabilities(
                runtime_config(),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                environ={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
            )
    finally:
        os.close(master_fd)
        os.close(slave_fd)
        os.close(read_fd)

    assert caps.stdin_isatty is True
    assert caps.stdout_isatty is False
    assert caps.renderer == "plain"
    assert caps.renderer_reason == "stdout-not-tty"
    assert caps.alt_screen_enabled is False


def make_shell(client: FakeAgentClient) -> LegacyFullScreenPromptToolkitShell:
    return LegacyFullScreenPromptToolkitShell(
        client=client,
        capabilities=FakeTerminalCapabilities(width=100),
        config=LegacyFullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="test-model",
            thread_label="Fixture thread",
        ),
    )


def test_ctrl_c_composer_path_requests_stop_once_and_preserves_draft() -> None:
    async def exercise() -> tuple[LegacyFullScreenPromptToolkitShell, FakeAgentClient]:
        client = FakeAgentClient(
            streams={
                "slow": [
                    DelayedEvent(
                        60.0,
                        {
                            "type": "response",
                            "content": "late",
                            "thread_id": "thread-1",
                        },
                    )
                ]
            },
            real_sleep=True,
        )
        shell = make_shell(client)

        assert shell._handle_composer_submission(ComposerSubmission("slow")) is True
        await asyncio.sleep(0)
        assert shell._busy is True

        shell.composer.buffer.text = "draft while stopping"
        assert shell.composer_controller.stop_or_clear(shell.composer.buffer) is True
        for _ in range(5):
            await asyncio.sleep(0)
            if client.stop_count:
                break

        assert client.stop_count == 1
        assert shell.composer.buffer.text == "draft while stopping"

        await shell.shutdown_active_turn(reason="test-cleanup")
        return shell, client

    shell, client = run(exercise())

    assert client.stop_count == 1
    assert shell.state.turn_status == "cancelling"


def test_bracketed_multiline_paste_submits_one_buffer() -> None:
    submissions: list[ComposerSubmission] = []
    controller = ComposerController(on_submit=submissions.append)
    buffer = controller.text_area.buffer
    buffer.text = "\x1b[200~first line\r\nsecond line\x1b[201~"
    buffer.cursor_position = len(buffer.text)

    assert controller.handle_enter(buffer) is True

    assert len(submissions) == 1
    assert submissions[0].message == "first line\nsecond line"
    assert "\x1b[200~" not in submissions[0].raw_text
    assert "\x1b[201~" not in submissions[0].raw_text
    assert buffer.text == ""
