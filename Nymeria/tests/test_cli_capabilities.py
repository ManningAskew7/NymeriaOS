from __future__ import annotations

from dataclasses import replace

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import (
    detect_terminal_capabilities,
    inside_terminal_multiplexer,
    probe_synchronized_output,
    resolve_atomic_repaint_support,
    synchronized_output_override,
)


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


def test_interactive_auto_renderer_prefers_rich():
    caps = detect()

    # Rich is the default and actively maintained renderer; auto resolves to it
    # on an interactive terminal.
    assert caps.renderer == "rich"
    assert caps.renderer_reason == "auto-interactive"
    assert caps.animation_enabled is True


def test_non_tty_stdout_forces_plain_renderer():
    caps = detect(stdout=FakeStream(isatty=False))

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "stdout-not-tty"
    assert caps.prefers_plain_renderer is True
    assert caps.animation_enabled is False
    assert caps.color_enabled is False


def test_non_tty_stdin_forces_plain_renderer():
    caps = detect(stdin=FakeStream(isatty=False))

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "stdin-not-tty"


def test_term_dumb_forces_plain_and_disables_interactive_features():
    caps = detect(env={"TERM": "dumb", "LANG": "en_US.UTF-8"})

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "dumb-terminal"
    assert caps.color_enabled is False
    assert caps.animation_enabled is False


def test_unset_term_is_treated_as_unsafe_for_interactive_rendering():
    caps = detect(env={"LANG": "en_US.UTF-8"})

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "dumb-terminal"
    assert caps.is_interactive is False
    assert caps.rich_allowed is False


def test_ci_forces_plain_even_with_tty_streams():
    caps = detect(env={"TERM": "xterm-256color", "CI": "1", "LANG": "en_US.UTF-8"})

    assert caps.ci is True
    assert caps.renderer == "plain"
    assert caps.renderer_reason == "ci"
    assert caps.is_interactive is False


def test_no_color_disables_color_and_animation_by_default():
    caps = detect(env={"TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "en_US.UTF-8"})

    assert caps.no_color is True
    assert caps.supports_color is False
    assert caps.color_enabled is False
    assert caps.color_depth == 0
    assert caps.animation_enabled is False


def test_force_color_overrides_no_color_in_interactive_renderer():
    caps = detect(
        env={
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "FORCE_COLOR": "1",
            "LANG": "en_US.UTF-8",
        }
    )

    assert caps.force_color is True
    assert caps.supports_color is True
    assert caps.color_enabled is True
    assert caps.color_depth == 16


def test_color_always_overrides_no_color_when_renderer_permits_color():
    caps = detect(
        config=runtime_config(color="always"),
        env={"TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "en_US.UTF-8"},
    )

    assert caps.color_mode == "always"
    assert caps.supports_color is True
    assert caps.color_enabled is True


def test_plain_renderer_keeps_ansi_color_disabled_even_when_forced():
    caps = detect(
        config=runtime_config(renderer="plain", color="always"),
        env={"TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "en_US.UTF-8"},
    )

    assert caps.renderer == "plain"
    assert caps.supports_color is True
    assert caps.color_enabled is False


def test_color_never_takes_precedence_over_force_color():
    caps = detect(
        config=runtime_config(color="never"),
        env={"TERM": "xterm-256color", "FORCE_COLOR": "3", "LANG": "en_US.UTF-8"},
    )

    assert caps.supports_color is False
    assert caps.color_enabled is False
    assert caps.color_depth == 0


def test_ascii_override_disables_unicode_glyphs():
    caps = detect(config=runtime_config(ascii_only=True))

    assert caps.supports_unicode is True
    assert caps.unicode_enabled is False


def test_non_utf8_locale_disables_unicode_glyphs():
    caps = detect(
        env={"TERM": "xterm-256color", "LANG": "C"},
        stdout=FakeStream(encoding="ascii"),
    )

    assert caps.supports_unicode is False
    assert caps.unicode_enabled is False


def test_no_animation_disables_animation_only():
    caps = detect(config=runtime_config(animation=False))

    assert caps.supports_animation is True
    assert caps.animation_enabled is False


def test_renderer_explicit_rich_is_respected_when_terminal_is_interactive():
    caps = detect(config=runtime_config(renderer="rich"))

    assert caps.requested_renderer == "rich"
    assert caps.renderer == "rich"
    assert caps.renderer_reason == "rich-requested"
    assert caps.rich_allowed is True


def test_removed_full_renderer_value_degrades_to_rich():
    # The legacy full-screen shell was removed (2026-07-04). A stale "full"
    # value from an old config or caller coerces to auto and resolves to rich
    # instead of crashing.
    caps = detect(config=runtime_config(renderer="full"))

    assert caps.requested_renderer == "auto"
    assert caps.renderer == "rich"
    assert caps.renderer_reason == "auto-interactive"


def test_color_depth_detects_truecolor_and_terminal_size_from_env():
    caps = detect(
        env={
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": "120",
            "LINES": "40",
            "LANG": "en_US.UTF-8",
        }
    )

    assert caps.color_depth == 24
    assert caps.width == 120
    assert caps.height == 40


def test_nymeria_cli_animation_env_disables_animation():
    caps = detect(
        env={
            "TERM": "xterm-256color",
            "NYMERIA_CLI_ANIMATION": "off",
            "LANG": "en_US.UTF-8",
        }
    )

    assert caps.supports_animation is False
    assert caps.animation_enabled is False


def _probe_against_pty(
    *replies: bytes,
    timeout: float = 1.0,
    gap: float = 0.0,
) -> tuple[bool | None, bytes]:
    """Run the real DECRQM probe against a pty answering with ``replies``.

    Returns the verdict AND whatever the probe left unread in the terminal's
    input queue, because leftovers are not a detail here: prompt_toolkit
    would read them as keystrokes and type them into the first prompt.
    ``gap`` splits the replies in time, the shape a real terminal produces
    when the mode reply and the DA1 fence arrive in separate packets.
    """

    import os
    import select
    import threading
    import time as time_module
    import tty

    master, slave = os.openpty()
    # cbreak up front so the leftover drain below reads byte-wise: the probe
    # restores whatever mode it found, and a canonical-mode tty would hand us
    # nothing until a newline arrives.
    tty.setcbreak(slave)

    class PtyStream:
        """Minimal stdin/stdout stand-in over one pty slave fd."""

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return slave

        def write(self, text: str) -> int:
            return os.write(slave, text.encode("utf-8"))

        def flush(self) -> None:
            return None

    stream = PtyStream()

    def responder() -> None:
        deadline = time_module.monotonic() + timeout + 1.0
        seen = b""
        while time_module.monotonic() < deadline:
            if not select.select([master], [], [], 0.02)[0]:
                continue
            seen += os.read(master, 1024)
            if b"$p" not in seen:
                continue
            for index, reply in enumerate(replies):
                if index and gap:
                    time_module.sleep(gap)
                os.write(master, reply)
            return

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()
    try:
        verdict = probe_synchronized_output(
            stdin=stream,
            stdout=stream,
            timeout=timeout,
        )
        leftover = b""
        # The first wait has to outlast ``gap``: a fence left queued by a
        # probe that stopped at the verdict arrives late by construction, and
        # a drain window shorter than the split would call the leak clean.
        wait = max(0.3, gap * 3)
        while select.select([slave], [], [], wait)[0]:
            chunk = os.read(slave, 1024)
            if not chunk:
                break
            leftover += chunk
            wait = 0.02
        return verdict, leftover
    finally:
        thread.join(timeout=2)
        os.close(slave)
        os.close(master)


def test_probe_synchronized_output_reads_decrpm_support():
    # DECRPM value 2 = mode recognized and currently reset: supported.
    assert _probe_against_pty(b"\x1b[?2026;2$y\x1b[?62;1;6c") == (True, b"")
    # 1 (set) and 3 (permanently set) also mean the terminal implements it.
    assert _probe_against_pty(b"\x1b[?2026;1$y\x1b[?62;1;6c") == (True, b"")


def test_probe_synchronized_output_reads_decrpm_rejection():
    # 0 = not recognized, 4 = permanently reset (cannot be enabled).
    assert _probe_against_pty(b"\x1b[?2026;0$y\x1b[?62;1;6c") == (False, b"")
    assert _probe_against_pty(b"\x1b[?2026;4$y\x1b[?62;1;6c") == (False, b"")


def test_probe_synchronized_output_uses_device_attributes_fence():
    # A terminal that ignores the mode query still answers DA1; that reply
    # is the definitive "not implemented" signal, without waiting out the
    # whole timeout.
    assert _probe_against_pty(b"\x1b[?62;1;6c") == (False, b"")


def test_probe_synchronized_output_drains_a_fence_that_arrives_late():
    # The shape that matters: a real terminal answers in two packets. The
    # read must run to the FENCE, not stop at the verdict, or the DA1 bytes
    # stay queued and prompt_toolkit types `Escape` + `[?62;1;6c` into the
    # user's first prompt.
    assert _probe_against_pty(
        b"\x1b[?2026;2$y",
        b"\x1b[?62;1;6c",
        gap=0.06,
    ) == (True, b"")


def test_probe_synchronized_output_keeps_a_verdict_when_the_fence_never_lands():
    # A definitive mode reply stays definitive if the fence goes missing, and
    # the budget bounds the wait rather than hanging startup. (The deadline
    # path also flushes, but that only clears what is queued at that instant:
    # a reply arriving later still reaches prompt_toolkit, which is why the
    # budget, not the flush, is the defense.)
    assert _probe_against_pty(b"\x1b[?2026;2$y", timeout=0.1) == (True, b"")


def test_probe_synchronized_output_undetermined_without_reply():
    # Silent terminal: undetermined, which callers treat as unsupported.
    assert _probe_against_pty(timeout=0.05) == (None, b"")


def test_probe_synchronized_output_requires_a_tty():
    assert (
        probe_synchronized_output(
            stdin=FakeStream(isatty=False),
            stdout=FakeStream(isatty=True),
        )
        is None
    )


def test_synchronized_output_override_reads_env():
    assert synchronized_output_override({}) is None
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "auto"}) is None
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "on"}) is True
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "1"}) is True
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "off"}) is False
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "false"}) is False
    # A typo must fall back to auto, never to a silent "on" that would enable
    # float repaints on a terminal that flashes.
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "of"}) is None
    assert synchronized_output_override({"NYMERIA_CLI_SYNC_OUTPUT": "enabled"}) is None


def test_resolve_atomic_repaint_support_skips_probe_when_forced():
    # The override must not touch the terminal at all: a non-tty stdin would
    # otherwise short-circuit to unsupported.
    assert (
        resolve_atomic_repaint_support(
            stdin=FakeStream(isatty=False),
            stdout=FakeStream(isatty=False),
            environ={"NYMERIA_CLI_SYNC_OUTPUT": "on"},
        )
        is True
    )
    assert (
        resolve_atomic_repaint_support(
            stdin=FakeStream(isatty=False),
            stdout=FakeStream(isatty=False),
            environ={"NYMERIA_CLI_SYNC_OUTPUT": "off", "TMUX": "/tmp/tmux-1000/default"},
        )
        is False
    )


def test_inside_terminal_multiplexer_detects_tmux_and_screen():
    assert inside_terminal_multiplexer({}) is False
    assert inside_terminal_multiplexer({"TERM": "xterm-256color"}) is False
    assert inside_terminal_multiplexer({"TMUX": "/tmp/tmux-1000/default,123,0"}) is True
    assert inside_terminal_multiplexer({"STY": "1234.pts-0.host"}) is True
    assert inside_terminal_multiplexer({"ZELLIJ": "0"}) is True
    assert inside_terminal_multiplexer({"ZELLIJ_SESSION_NAME": "main"}) is True
    # TERM alone is not evidence: emulators that are not multiplexers ship
    # these entries, and trusting them would skip the probe (and a real DEC
    # 2026 answer) for a terminal that does implement it.
    assert inside_terminal_multiplexer({"TERM": "tmux-256color"}) is False
    assert inside_terminal_multiplexer({"TERM": "screen.xterm-256color"}) is False


def test_resolve_atomic_repaint_support_trusts_multiplexer_batching():
    # Measured 2026-07-28: tmux 3.4 answers DA1 but no DECRPM for mode 2026,
    # so the probe alone would say "no". A multiplexer renders from its own
    # buffer and flushes on its own redraw cycle, which coalesces a burst of
    # writes into one outer-terminal update, so it never reaches the probe.
    assert (
        resolve_atomic_repaint_support(
            stdin=FakeStream(isatty=False),
            stdout=FakeStream(isatty=False),
            environ={"TERM": "tmux-256color", "TMUX": "/tmp/tmux-1000/default"},
        )
        is True
    )
    # A bare terminal that cannot confirm resolves False (conservative).
    assert (
        resolve_atomic_repaint_support(
            stdin=FakeStream(isatty=False),
            stdout=FakeStream(isatty=False),
            environ={"TERM": "xterm-256color"},
        )
        is False
    )
