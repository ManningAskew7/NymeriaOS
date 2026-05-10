from __future__ import annotations

import re
from dataclasses import replace

from cli_fixtures import (
    CapturedRenderOutput,
    strip_delays,
    thinking_silence_events,
    tool_call_result_events,
)

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import detect_terminal_capabilities
from nymeria.triggers.cli.rendering.plain import PlainRenderer

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class FakeStream:
    def __init__(self, *, isatty: bool = True, encoding: str | None = "utf-8"):
        self._isatty = isatty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._isatty


def runtime_config(**overrides):
    return replace(CLIRuntimeConfig(), **overrides)


def test_plain_renderer_streams_response_to_stdout_and_progress_to_stderr() -> None:
    output = CapturedRenderOutput()
    renderer = PlainRenderer(stdout=output.stdout, stderr=output.stderr, width=100)
    renderer.start_turn("use a tool", thread_id="thread-1", now=0.0)

    renderer.render_events(tool_call_result_events(), now=1.0)

    assert output.stdout_text == "I found the notes.\n"
    assert "Waiting... search_memory" in output.stderr_text
    assert "Processing results..." in output.stderr_text
    assert "> search_memory project status -> Found 2 matching notes." in (
        output.stderr_text
    )
    assert not ANSI_RE.search(output.stdout_text)
    assert not ANSI_RE.search(output.stderr_text)


def test_plain_renderer_hides_thinking_content_and_spinner_frames() -> None:
    output = CapturedRenderOutput()
    renderer = PlainRenderer(stdout=output.stdout, stderr=output.stderr, width=80)
    renderer.start_turn("think quietly", thread_id="thread-1", now=0.0)

    renderer.render_events(strip_delays(thinking_silence_events()), now=1.0)

    assert output.stdout_text == "Ready.\n"
    assert "Thinking..." in output.stderr_text
    assert "checking context" not in output.stderr_text
    assert "⠋" not in output.stderr_text
    assert "| Thinking" not in output.stderr_text
    assert "/ Thinking" not in output.stderr_text
    assert "- Thinking" not in output.stderr_text


def test_plain_renderer_capability_policy_covers_non_tty_and_dumb_terminal() -> None:
    non_tty = detect_terminal_capabilities(
        runtime_config(),
        stdin=FakeStream(),
        stdout=FakeStream(isatty=False),
        stderr=FakeStream(),
        environ={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
    )
    dumb = detect_terminal_capabilities(
        runtime_config(),
        stdin=FakeStream(),
        stdout=FakeStream(),
        stderr=FakeStream(),
        environ={"TERM": "dumb", "LANG": "en_US.UTF-8"},
    )

    assert non_tty.renderer == "plain"
    assert non_tty.renderer_reason == "stdout-not-tty"
    assert dumb.renderer == "plain"
    assert dumb.renderer_reason == "dumb-terminal"


def test_plain_renderer_render_state_snapshot_contains_no_ansi() -> None:
    output = CapturedRenderOutput()
    renderer = PlainRenderer(stdout=output.stdout, stderr=output.stderr, width=100)
    renderer.start_turn("use a tool", thread_id="thread-1", now=0.0)
    renderer.render_events(tool_call_result_events(), now=1.0)
    snapshot = renderer.state
    output.clear()

    PlainRenderer(
        state=snapshot,
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    ).render_state()

    assert "I found the notes." in output.stdout_text
    assert "> search_memory project status -> Found 2 matching notes." in (
        output.stderr_text
    )
    assert not ANSI_RE.search(output.stdout_text)
    assert not ANSI_RE.search(output.stderr_text)
