from __future__ import annotations

import re
from dataclasses import replace

from cli_fixtures import CapturedRenderOutput, FakeAgentClient, FakeTerminalCapabilities

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import detect_terminal_capabilities
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
    _transcript_render_width,
)
from nymeria.triggers.cli.rendering.plain import PlainRenderer
from nymeria.triggers.cli.rendering.transcript import max_line_width
from nymeria.triggers.cli.state import reduce_stream_event, start_turn

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class FakeStream:
    def __init__(self, *, isatty: bool = True, encoding: str | None = "utf-8"):
        self._isatty = isatty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._isatty


def runtime_config(**overrides):
    return replace(CLIRuntimeConfig(), **overrides)


def detect(*, config=None, env=None, stdin=None, stdout=None, stderr=None):
    return detect_terminal_capabilities(
        config or runtime_config(),
        stdin=stdin or FakeStream(),
        stdout=stdout or FakeStream(),
        stderr=stderr or FakeStream(),
        environ=env or {"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
    )


def test_rendering_fallback_matrix_covers_non_tty_no_color_and_dumb_terminal() -> None:
    non_tty = detect(stdout=FakeStream(isatty=False))
    no_color = detect(env={"TERM": "xterm-256color", "NO_COLOR": "1"})
    dumb = detect(env={"TERM": "dumb", "LANG": "en_US.UTF-8"})

    assert non_tty.renderer == "plain"
    assert non_tty.renderer_reason == "stdout-not-tty"
    assert non_tty.color_enabled is False
    assert non_tty.alt_screen_enabled is False

    assert no_color.renderer == "full"
    assert no_color.color_enabled is False
    assert no_color.animation_enabled is False

    assert dumb.renderer == "plain"
    assert dumb.renderer_reason == "dumb-terminal"
    assert dumb.color_enabled is False
    assert dumb.animation_enabled is False


def test_plain_stream_contract_keeps_status_off_stdout_and_removes_ansi() -> None:
    output = CapturedRenderOutput()
    renderer = PlainRenderer(stdout=output.stdout, stderr=output.stderr, width=100)
    renderer.start_turn("handle a queued tool call", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {
                "type": "queued",
                "holder": "worker-1",
                "message": "waiting for thread lock",
                "thread_id": "thread-1",
            },
            {
                "type": "thinking",
                "content": "private chain of thought",
                "thread_id": "thread-1",
            },
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "filesystem_read",
                "args": {"path": "/opt/NymeriaOS/Nymeria/docs/cli.md"},
                "thread_id": "thread-1",
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "filesystem_read",
                "result": "\x1b[31mread complete\x1b[0m",
                "thread_id": "thread-1",
            },
            {
                "type": "response",
                "content": "Final answer.\n",
                "thread_id": "thread-1",
            },
            {"type": "done", "thread_id": "thread-1", "tool_call_count": 1},
        ],
        now=1.0,
    )

    assert output.stdout_text == "Final answer.\n"
    assert "Waiting..." not in output.stdout_text
    assert "Thinking..." not in output.stdout_text
    assert "Processing results..." not in output.stdout_text
    assert "Waiting..." in output.stderr_text
    assert "Thinking..." in output.stderr_text
    assert "Processing results..." in output.stderr_text
    assert "filesystem_read" in output.stderr_text
    assert "private chain of thought" not in output.stderr_text
    assert "[31m" not in output.stderr_text
    assert "[0m" not in output.stderr_text
    assert not ANSI_RE.search(output.stdout_text)
    assert not ANSI_RE.search(output.stderr_text)


def make_shell(*, width: int = 80) -> FullScreenPromptToolkitShell:
    return FullScreenPromptToolkitShell(
        client=FakeAgentClient(),
        capabilities=FakeTerminalCapabilities(width=width),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="provider/" + ("model-" * 16),
            thread_label="A long thread title " + ("with many words " * 8),
        ),
    )


def test_full_screen_resize_recomputes_transcript_and_status_widths() -> None:
    shell = make_shell(width=40)
    long_response = (
        "This is a long response with several words that should wrap cleanly "
        "when terminal dimensions change."
    )

    shell.state = start_turn(shell.state, "resize please", now=0.0)
    shell.state = reduce_stream_event(
        shell.state,
        {"type": "response", "content": long_response, "thread_id": "thread-1"},
        now=1.0,
    )
    shell.state = reduce_stream_event(
        shell.state,
        {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
        now=1.1,
    )

    shell._refresh_transcript()
    narrow_status = shell._status_text()

    assert max_line_width(shell.transcript.text) <= _transcript_render_width(
        shell.capabilities
    )
    assert len(narrow_status) <= 40
    assert narrow_status.endswith("...")

    shell.capabilities = shell.capabilities.with_overrides(width=120)
    shell._refresh_transcript()

    assert max_line_width(shell.transcript.text) <= _transcript_render_width(
        shell.capabilities
    )
    assert len(shell._status_text()) <= 120
    assert "thread A long thread title" in shell._status_text()


def test_full_screen_transcript_width_accounts_for_frame_and_scrollbar() -> None:
    shell = make_shell(width=100)
    shell.state = start_turn(shell.state, "hey", now=0.0)
    shell.state = reduce_stream_event(
        shell.state,
        {"type": "response", "content": "hello", "thread_id": "thread-1"},
        now=1.0,
    )

    shell._refresh_transcript()

    assert _transcript_render_width(shell.capabilities) == 96
    assert max_line_width(shell.transcript.text) <= 96
