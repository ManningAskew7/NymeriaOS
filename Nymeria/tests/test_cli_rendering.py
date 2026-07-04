from __future__ import annotations

import re
from dataclasses import replace

from cli_fixtures import CapturedRenderOutput

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

    # NO_COLOR disables color/animation but keeps the default interactive
    # renderer (rich); it does not force the plain fallback.
    assert no_color.renderer == "rich"
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


