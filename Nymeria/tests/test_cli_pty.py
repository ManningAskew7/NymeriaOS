"""Terminal-edge regression tests for the CLI/TUI.

T20 deliberately avoids adding a ``pexpect`` dependency. The stable coverage
here uses the stdlib ``pty`` module where available, plus prompt_toolkit
controller seams for interrupt and paste behavior.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import IO

import pytest

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import detect_terminal_capabilities
from nymeria.triggers.cli.input import ComposerController, ComposerSubmission

pty = pytest.importorskip("pty")


def runtime_config(**overrides):
    return replace(CLIRuntimeConfig(), **overrides)


def _open_slave_streams(slave_fd: int) -> tuple[IO[bytes], IO[bytes], IO[bytes]]:
    return (
        os.fdopen(os.dup(slave_fd), "rb", buffering=0),
        os.fdopen(os.dup(slave_fd), "wb", buffering=0),
        os.fdopen(os.dup(slave_fd), "wb", buffering=0),
    )


def test_real_pty_streams_are_detected_as_interactive_rich() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        stdin, stdout, stderr = _open_slave_streams(slave_fd)
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

    assert caps.stdin_isatty is True
    assert caps.stdout_isatty is True
    assert caps.stderr_isatty is True
    assert caps.renderer == "rich"
    assert caps.renderer_reason == "auto-interactive"
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
