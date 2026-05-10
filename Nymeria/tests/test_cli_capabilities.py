from __future__ import annotations

from dataclasses import replace

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.capabilities import detect_terminal_capabilities


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


def test_interactive_auto_renderer_prefers_full_screen():
    caps = detect()

    assert caps.renderer == "full"
    assert caps.renderer_reason == "auto-interactive"
    assert caps.full_screen_allowed is True
    assert caps.alt_screen_enabled is True
    assert caps.animation_enabled is True
    assert caps.mouse_enabled is True


def test_non_tty_stdout_forces_plain_renderer():
    caps = detect(stdout=FakeStream(isatty=False))

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "stdout-not-tty"
    assert caps.prefers_plain_renderer is True
    assert caps.full_screen_allowed is False
    assert caps.alt_screen_enabled is False
    assert caps.animation_enabled is False
    assert caps.color_enabled is False


def test_non_tty_stdin_forces_plain_renderer():
    caps = detect(stdin=FakeStream(isatty=False))

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "stdin-not-tty"
    assert caps.alt_screen_enabled is False


def test_term_dumb_forces_plain_and_disables_interactive_features():
    caps = detect(env={"TERM": "dumb", "LANG": "en_US.UTF-8"})

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "dumb-terminal"
    assert caps.color_enabled is False
    assert caps.alt_screen_enabled is False
    assert caps.animation_enabled is False
    assert caps.mouse_enabled is False


def test_unset_term_is_treated_as_unsafe_for_interactive_rendering():
    caps = detect(env={"LANG": "en_US.UTF-8"})

    assert caps.renderer == "plain"
    assert caps.renderer_reason == "dumb-terminal"
    assert caps.is_interactive is False
    assert caps.rich_allowed is False
    assert caps.full_screen_allowed is False


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


def test_no_alt_screen_disables_alt_screen_without_disabling_full_renderer():
    caps = detect(config=runtime_config(alt_screen=False))

    assert caps.renderer == "full"
    assert caps.supports_alt_screen is True
    assert caps.alt_screen_enabled is False


def test_no_animation_disables_animation_only():
    caps = detect(config=runtime_config(animation=False))

    assert caps.supports_animation is True
    assert caps.animation_enabled is False
    assert caps.alt_screen_enabled is True


def test_renderer_explicit_rich_is_respected_when_terminal_is_interactive():
    caps = detect(config=runtime_config(renderer="rich"))

    assert caps.requested_renderer == "rich"
    assert caps.renderer == "rich"
    assert caps.renderer_reason == "rich-requested"
    assert caps.alt_screen_enabled is False
    assert caps.rich_allowed is True


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
