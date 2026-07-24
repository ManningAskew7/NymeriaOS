"""Interactive Textual wizard driven with real keypresses (Pilot).

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _API_PORT_STEP,
    _AUTH_STEP,
    _CONNECTION_STEP,
    _HOSTING_STEP,
    _PROVIDER_STEP,
    _SECURITY_STEP,
    _advance_to_provider,
    _no_models,
)


# --- interactive Textual wizard (Pilot) -------------------------------------


def test_wizard_pilot_forward_back_and_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            assert app.nav.current() == 0  # welcome (environment detection)
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            assert app.nav.current() == _HOSTING_STEP
            await pilot.press("enter")  # accept default hosting (local), advance
            await pilot.pause()
            assert state.hosting is HostingOption.LOCAL
            assert app.nav.current() == _API_PORT_STEP
            await pilot.press("escape")  # back to hosting (does not exit)
            await pilot.pause()
            assert app.nav.current() == _HOSTING_STEP
            await pilot.press("enter")  # hosting -> api port
            await pilot.pause()
            await pilot.press("enter")  # accept default port (8000) -> security
            await pilot.pause()
            # docker_stack (index 3) is skipped for a non-Docker host.
            assert app.nav.current() == _SECURITY_STEP
            await pilot.press("enter")  # accept default security -> auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
            await pilot.press("enter")  # accept default auth (API key) -> provider
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP
            await pilot.press("enter")  # Enter in picker -> focus moves to key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-xyz"
            await pilot.press("enter")  # key entered -> advance past provider
            await pilot.pause()
            assert state.provider == "anthropic"
            assert state.api_key == "sk-ant-xyz"
            # Anthropic has a fixed endpoint and no API-mode toggle, so the
            # connection step is skipped; the model step is next.
            assert _CONNECTION_STEP not in app.nav.applicable_indices()
            await pilot.press("enter")  # model step: accept default model, advance
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.model == "claude-sonnet-4-6"  # provider default model filled in


def test_wizard_pilot_tier_quickstart_gates_and_seeds(monkeypatch):
    """Enter on the chooser (Quickstart preselected) flips quick mode; the
    hosting pick then seeds the hosting-dependent keyless defaults, and the
    next applicable screen is the auth method (port/stack/security gated off).
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> tuple[WizardState, int]:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("enter")  # accept Quickstart (preselected)
            await pilot.pause()
            assert app.nav.current() == _HOSTING_STEP
            await pilot.press("enter")  # accept default hosting (local)
            await pilot.pause()
            return state, app.nav.current()  # type: ignore[bad-return]

    state, landed = asyncio.run(drive())
    assert state.quick is True
    assert state.extras["tier"] == "quickstart"
    # api_port, docker_stack, and security are quick-gated, so the LLM auth
    # choice is the next screen.
    assert landed == _AUTH_STEP
    # Hosting-dependent seeds applied for the local shape.
    assert state.extras["web_search"] == ["web_search_ddgs"]
    assert state.extras["tts"] == "kokoro"
    assert state.extras["stt"] == "faster-whisper"
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert state.rag_quickstarted is True


def test_hint_markup_accents_keys_and_preserves_text():
    from textual.content import Content

    from nymeria.setup.steps.base import ACCENT, hint_markup

    hint = "up/down move   space select   enter next   ctrl+q quit"
    markup = hint_markup(hint)
    assert f"[{ACCENT}]up/down[/]" in markup  # first word of each pair is the key
    assert f"[{ACCENT}]ctrl+q[/]" in markup
    # The styling is presentation-only: the plain text survives unchanged.
    assert Content.from_markup(markup).plain == hint


def test_code_markup_styles_backtick_spans_and_escapes_markup():
    """Wizard prose (notes, choice descriptions, errors) is plain text with
    optional `code` spans: backticks render as accent-colored text instead of
    showing through literally, and stray brackets are escaped, never parsed."""
    from textual.content import Content

    from nymeria.setup.steps.base import ACCENT, code_markup

    markup = code_markup("Finish setup and show `nymeria slim` to run yourself.")
    assert f"[{ACCENT}]nymeria slim[/]" in markup
    plain = Content.from_markup(markup).plain
    assert plain == "Finish setup and show nymeria slim to run yourself."
    assert "`" not in plain

    # Brackets in prose are data, not markup (e.g. a user-typed model id).
    hostile = code_markup("model [bold red]x[/] stays literal")
    assert Content.from_markup(hostile).plain == "model [bold red]x[/] stays literal"

    # No backticks, no brackets: identity, so plain prose is untouched.
    assert code_markup("Plain text.") == "Plain text."


def test_wizard_pilot_provider_note_follows_highlight_and_clears(monkeypatch):
    """The note under the provider list shows the highlighted provider's
    registry note and clears when the filter has no matches (no stale prose
    for a provider that is no longer shown)."""
    from textual.widgets import Static

    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)
            spec = get_llm_provider_spec("aihubmix")
            assert spec is not None and spec.notes_for_user
            await pilot.press(*"aihubmix")  # filter to a provider with a note
            await pilot.pause()
            note = str(app.screen.query_one("#provider-note", Static).render())
            assert "Reasoning round-trips" in note
            await pilot.press(*"zzz")  # no matches
            await pilot.pause()
            note = str(app.screen.query_one("#provider-note", Static).render())
            assert note == ""

    asyncio.run(drive())


def test_show_error_toggles_error_row_visibility():
    """The error Static is hidden while empty (an empty Static still reserves
    a row, which matters on small terminals) and shown with a message."""
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            scr = app.screen
            error = scr.query_one("#wizard-error", Static)
            assert error.display is False
            scr.show_error("boom")  # type: ignore[missing-attribute]
            await pilot.pause()
            assert error.display is True
            assert "boom" in str(error.render())
            scr.show_error("")  # type: ignore[missing-attribute]
            await pilot.pause()
            assert error.display is False

    asyncio.run(drive())


def test_wizard_pilot_provider_step_fits_without_scrolling(monkeypatch):
    """At a standard terminal size the picker list is capped (fit_list), so
    the API key field stays fully on screen. Regression test for the dead-gap
    bug where the list's uncapped measured height pushed the field below the
    fold even though the rendered list was short."""
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test(size=(110, 30)) as pilot:
            await _advance_to_provider(pilot)
            key = app.screen.query_one("#api-key", Input)
            assert key.region.height > 0  # rendered at all
            assert key.region.y + key.region.height <= 30  # fully on screen

    asyncio.run(drive())


def test_wizard_pilot_arrow_keys_move_focus_and_description_space_selects():
    """Arrow keys move focus (and the per-option description) WITHOUT changing the
    selection; Space selects the focused option. The selection dot only moves on
    Space (or Enter), not on arrow navigation.
    """
    from textual.widgets import Static

    from nymeria.onboarding import HOSTING_CHOICES, HOSTING_ORDER
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    def desc_for(index: int) -> str:
        # The panel renders descriptions through code_markup (escape + accent
        # `code` spans), so the expected text goes through the same pipe; the
        # rendered Static stringifies to the parsed plain text.
        from textual.content import Content

        from nymeria.setup.steps.base import code_markup

        raw = HOSTING_CHOICES[HOSTING_ORDER[index]].description
        return Content.from_markup(code_markup(raw)).plain

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            scr = app.screen
            panel = scr.query_one("#choice-desc", Static)
            buttons = list(scr.query(CircleRadioButton))

            # Default selection + focus + description all sit on index 0 (LOCAL).
            assert buttons[0].value is True
            assert scr.focused is buttons[0]
            assert str(panel.render()) == desc_for(0)

            await pilot.press("down")
            await pilot.pause()
            # Focus + description moved, but the selection did NOT.
            assert scr.focused is buttons[1]
            assert str(panel.render()) == desc_for(1)
            assert buttons[0].value is True and buttons[1].value is False

            await pilot.press("space")
            await pilot.pause()
            # Space selects the focused option; the old default is cleared.
            assert buttons[1].value is True and buttons[0].value is False
            assert not app.completed  # selecting does not advance

    asyncio.run(drive())


def test_multi_select_checkboxes_render_as_brackets():
    """Multi-select rows draw real [x] / [ ] checkboxes, not the stock
    half-block button whose X is always present and only changes color."""
    from textual.widgets import SelectionList

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.nav import Step
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import Choice, MultiSelectStep

    def build(wizard, number, total):
        return MultiSelectStep(
            wizard,
            number,
            total,
            step_id="multi",
            title="Pick things",
            choices=[Choice("a", "Alpha"), Choice("b", "Beta")],
            get_initial=lambda _state: ["a"],
            store=lambda _state, _values: None,
        )

    step = Step(id="multi", applies=lambda _state: True, build=build)

    async def drive() -> None:
        app = SetupWizardApp(WizardState(), steps=[step])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            picker = app.screen.query_one(SelectionList)

            def row(index: int) -> str:
                return "".join(seg.text for seg in picker.render_line(index))

            assert row(0).startswith("[x] Alpha")  # initial selection
            assert row(1).startswith("[ ] Beta")
            await pilot.press("space")  # toggle the highlighted first row off
            await pilot.pause()
            assert row(0).startswith("[ ] Alpha")

    asyncio.run(drive())


def test_wizard_radio_renders_bare_circles_without_box():
    """The radio indicator is a bare circle (outline when off, filled when on),
    never the stock ``BUTTON_LEFT/RIGHT`` half-block box. Regression for removing
    the blue box around the dial: the box came from those side glyphs, so their
    absence is what proves it is gone.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            buttons = list(app.screen.query(CircleRadioButton))
            assert buttons  # the hosting step is a single-select radio screen
            for button in buttons:
                glyph = button._button.plain
                assert "▐" not in glyph and "▌" not in glyph  # no box sides
                assert glyph.strip() in {"○", "●"}  # outline / filled circle
            on = [b for b in buttons if b.value]
            assert len(on) == 1  # exactly the selected (highlighted) row
            assert on[0]._button.plain.strip() == "●"
            assert all(
                b._button.plain.strip() == "○" for b in buttons if not b.value
            )

    asyncio.run(drive())


def test_wizard_pilot_security_step_unleashed_only():
    """Secure and Standard render disabled (greyed out, unfocusable); the dot
    and focus land on Unleashed, arrows never reach a disabled row, and Enter
    stores UNLEASHED.
    """
    from nymeria.onboarding import SecurityProfile
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept default hosting (local) -> api port
            await pilot.pause()
            await pilot.press("enter")  # accept default port (8000) -> security
            await pilot.pause()
            assert app.nav.current() == _SECURITY_STEP
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            assert [b.disabled for b in buttons] == [True, True, False]
            assert buttons[2].value is True  # dot pre-selected on Unleashed
            assert scr.focused is buttons[2]
            for key in ("up", "down"):
                await pilot.press(key)
                await pilot.pause()
                assert scr.focused is not buttons[0]
                assert scr.focused is not buttons[1]
            await pilot.press("enter")  # lock Unleashed, advance to auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
        return state

    state = asyncio.run(drive())
    assert state.security_profile is SecurityProfile.UNLEASHED


def test_wizard_radio_focused_label_is_bold_and_bright_no_bar():
    """The highlight is the FOCUSED option, marked by bold + brighter text alone,
    with no background bar (the filled white circle pins the selection).
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton

    def luma(color) -> int:
        return color.r + color.g + color.b

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            buttons = list(app.screen.query(CircleRadioButton))
            focused = app.screen.focused
            assert focused in buttons  # the highlight is the focused option
            others = [b for b in buttons if b is not focused]
            assert others

            # No row paints a background: there is no selection bar.
            assert all(b.styles.background.a == 0 for b in buttons)

            foc_label = focused.get_visual_style("toggle--label")  # type: ignore[missing-attribute]
            assert foc_label.bold is True  # the focused row is bold...
            # ...and brighter than every unfocused label, which stay un-bold.
            foc_luma = luma(foc_label.foreground)
            for other in others:
                other_label = other.get_visual_style("toggle--label")
                assert other_label.bold is not True
                assert foc_luma >= luma(other_label.foreground)

    asyncio.run(drive())


def test_wizard_pilot_voice_default_preserved_and_enter_locks_focus():
    """The TTS step's default is "Off" (first row) on a fresh run. Entering
    keeps that default selected and focused; Enter without moving records it.
    Arrowing moves focus (not selection); Enter then locks the focused option in.
    """
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.base import CircleRadioButton
    from nymeria.setup.steps.placeholders import make_tts_step
    from nymeria.setup.voice_catalog import TTS_CHOICES

    # The catalog order drives the rows: "none" first, then the providers.
    assert TTS_CHOICES[0].value == "none"
    assert TTS_CHOICES[1].value == "kokoro"
    none_index = 0

    async def drive_enter_only() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            panel = scr.query_one("#choice-desc", Static)
            # Selection, focus, and description all sit on the stored default.
            assert buttons[none_index].value is True
            assert scr.focused is buttons[none_index]
            assert str(panel.render()) == TTS_CHOICES[0].description
            await pilot.press("enter")  # commit without moving
            await pilot.pause()
        return dict(app.state.extras)

    async def drive_down_then_enter() -> dict:
        app = SetupWizardApp(WizardState(), steps=[make_tts_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            buttons = list(scr.query(CircleRadioButton))
            await pilot.press("down")  # focus moves to the next row (kokoro)
            await pilot.pause()
            assert scr.focused is buttons[1]  # focus moved...
            assert buttons[none_index].value is True  # ...but selection did not
            await pilot.press("enter")  # Enter locks the focused option in + advances
            await pilot.pause()
        return dict(app.state.extras)

    assert asyncio.run(drive_enter_only()) == {"tts": "none"}
    assert asyncio.run(drive_down_then_enter()) == {"tts": "kokoro"}


def test_wizard_pilot_selects_non_default_provider(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", _no_models)

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            await pilot.press(*"openrouter")  # filter the provider list
            await pilot.pause()
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-or-test"  # model left blank
            await pilot.press("enter")  # advance past provider -> connection step
            await pilot.pause()
            # OpenRouter supports API mode, so the connection step applies.
            assert _CONNECTION_STEP in app.nav.applicable_indices()
            await pilot.press("enter")  # connection: accept defaults, advance
            await pilot.pause()
            await pilot.press("enter")  # model step: accept default model, advance
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.provider == "openrouter"
    # Blank model falls back to the chosen provider's registry default.
    assert app.state.model == "anthropic/claude-sonnet-4.5"


def test_wizard_pilot_model_step_lists_and_selects(monkeypatch):
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.providers import ModelChoice
    from nymeria.setup.state import WizardState

    async def fake_fetch(spec, *, api_key, base_url=None, timeout=8.0):
        return [
            ModelChoice(id="claude-sonnet-4-6", context_length=200000),
            ModelChoice(id="claude-opus-4-8", context_length=200000),
            ModelChoice(id="claude-haiku-4-5", context_length=200000),
        ]

    monkeypatch.setattr("nymeria.setup.steps.model.fetch_models_for_spec", fake_fetch)

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state)
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            await pilot.press("enter")  # picker -> focus key
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-xyz"
            await pilot.press("enter")  # advance to model step (connection skipped)
            await pilot.pause()
            await pilot.pause()  # let the fetch worker populate the list
            await pilot.press(*"opus")  # filter to the one matching model
            await pilot.pause()
            await pilot.press("enter")  # commit the highlighted model, advance
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.model == "claude-opus-4-8"


def test_wizard_pilot_blank_key_requires_explicit_skip():
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            await pilot.press("enter")  # Enter in picker -> focus key field
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP  # picker enter does not advance
            await pilot.press("enter")  # blank key -> error, does not skip
            await pilot.pause()
            assert app.nav.current() == _PROVIDER_STEP
            assert app.state.provider is None
            await pilot.press("ctrl+s")  # explicit skip
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.provider is None
    assert app.nav.current() != _PROVIDER_STEP  # advanced past the provider step


def test_wizard_pilot_ctrl_s_skips_without_recording():
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            await pilot.press("ctrl+s")  # skip hosting without choosing
            await pilot.pause()
            await pilot.press("ctrl+s")  # skip the api port without typing
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.hosting is None
    assert app.state.api_port is None
    # Hosting unset means a non-Docker host, so docker_stack is skipped and the
    # security profile step is next.
    assert app.nav.current() == _SECURITY_STEP


def test_wizard_pilot_web_search_multiselect_seeds_real_backend():
    """The web search step is a real multi-select over the built web_search_*
    tool names (not abstract categories), and a pick is recorded as a concrete
    tool name that the init flow would seed.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_web_search_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_web_search_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")  # toggle the first (highlighted) backend
            await pilot.pause()
            await pilot.press("enter")  # advance, storing the selection
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["web_search"] == ["web_search_perplexity"]
    assert seeded_tool_names(state) == ["web_search_perplexity"]


def test_wizard_pilot_fetch_url_multiselect_defaults_nymeria_on():
    """The web fetch step defaults the keyless built-in fetcher (fetch_url_nymeria)
    checked, so accepting the default records it with no toggling. It needs no key
    (it distills pages with the configured LLM), making it a safe default.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_fetch_url_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_fetch_url_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # accept the default (nymeria fetch pre-checked)
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["fetch_url"] == ["fetch_url_nymeria"]
    assert seeded_tool_names(state) == ["fetch_url_nymeria"]


def test_wizard_pilot_image_gen_multiselect_seeds_real_backend():
    """The image generation step is a real multi-select over the built
    image_gen_* provider tool names (parallel to web search / web fetch), and a
    pick is recorded as a concrete tool name the init flow would seed.
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_image_gen_step, seeded_tool_names

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_image_gen_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")  # toggle the first (highlighted) provider
            await pilot.pause()
            await pilot.press("enter")  # advance, storing the selection
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.extras["image_gen"] == ["image_gen_openai"]
    assert seeded_tool_names(state) == ["image_gen_openai"]


def test_wizard_pilot_backend_keys_step_collects_key():
    """The backend-keys step renders one input per selected backend and writes
    the value into optional_env under the canonical env var.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.backend_keys import make_backend_keys_step

    async def drive() -> WizardState:
        state = WizardState(extras={"web_search": ["web_search_tavily"]})
        app = SetupWizardApp(state, steps=[make_backend_keys_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#backend-key-tavily_api_key", Input).value = "tav-secret"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.optional_env["TAVILY_API_KEY"] == "tav-secret"


def test_wizard_pilot_skill_kits_multiselect_defaults_on_and_records_list():
    """The skill-kits step is a real multi-select: the curated default kit set is
    checked by default, the offered set is discovered live from bundled kits, and
    the chosen kit names are recorded as a list in extras.
    """
    from nymeria.setup import family_catalog
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.placeholders import make_skill_kits_step, seeded_global_skills

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_skill_kits_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # accept the default-checked set
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    # The recorded picks are exactly the curated default-on kits (order follows the
    # discovered list, so compare order-independently).
    assert set(state.extras["skill_kits"]) == set(family_catalog.default_checked_skill_kits())
    # The offered set is discovered live, so it is a superset of the defaults.
    offered = {c.value for c in family_catalog.skill_kit_choices()}
    assert set(family_catalog.default_checked_skill_kits()) <= offered
    assert seeded_global_skills(state) == state.extras["skill_kits"]


def test_wizard_pilot_embedder_enter_jumps_to_empty_key_then_advances():
    """Enter on a cloud model with no key does not advance: it locks the model in,
    focuses the empty key field, and shows an error. Filling it and pressing Enter
    advances (retrieval keeps its Hybrid default, which Enter skips).
    """
    from textual.widgets import Input, Static

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive():
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            await pilot.press("enter")  # default premium-cohere needs a key
            await pilot.pause()
            key_input = scr.query_one("#rag-key", Input)
            assert key_input.has_focus  # jumped to the missing field
            assert not app.completed  # did not advance
            assert str(scr.query_one("#wizard-error", Static).render())
            key_input.value = "test-key"
            await pilot.press("enter")  # key filled -> nothing else required -> advance
            await pilot.pause()
        return state, app

    state, app = asyncio.run(drive())
    assert app.completed
    assert state.embedder == "premium-cohere"
    assert state.optional_env["EMBEDDING_API_KEY"] == "test-key"
    assert state.rag_retrieval_mode == "hybrid"  # default kept (Enter skipped it)


def test_wizard_pilot_embedder_local_hides_key_and_enter_advances():
    """Selecting the local (keyless) embedder hides the key field; Enter then needs
    no key and advances.
    """
    from textual.widgets import Input, RadioButton

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive():
        from nymeria.setup.rag_catalog import EMBEDDERS

        granite_idx = next(
            i for i, o in enumerate(EMBEDDERS) if o.id == "local-granite"
        )
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            models = list(scr.query_one("#model-group").query(RadioButton))
            for _ in range(granite_idx):  # arrow down to the local (keyless) option
                await pilot.press("down")
            await pilot.pause()
            assert scr.focused is models[granite_idx]  # local-granite
            await pilot.press("space")  # select local
            await pilot.pause()
            assert not scr.query_one("#rag-key", Input).display  # key hidden
            await pilot.press("enter")  # local needs no key -> advance
            await pilot.pause()
        return state, app

    state, app = asyncio.run(drive())
    assert app.completed
    assert state.embedder == "local-granite"
    assert "EMBEDDING_API_KEY" not in state.optional_env
    assert state.rag_retrieval_mode == "hybrid"


def test_wizard_pilot_embedder_arrows_cross_fields_both_ways():
    """The core 'change your mind' fix: arrows move from the model list into the
    key field and the retrieval group, and back UP from the key field to the model
    list (focus is never trapped in one control).
    """
    from textual.widgets import Input, RadioButton

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_embedder_step

    async def drive() -> None:
        app = SetupWizardApp(WizardState(), steps=[make_embedder_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = app.screen
            models = list(scr.query_one("#model-group").query(RadioButton))
            retrieval = list(scr.query_one("#retrieval-group").query(RadioButton))
            key_input = scr.query_one("#rag-key", Input)
            assert scr.focused is models[0]
            # Down through every model option lands on the key field.
            for _ in range(len(models)):
                await pilot.press("down")
                await pilot.pause()
            assert scr.focused is key_input
            await pilot.press("down")  # into the retrieval group
            await pilot.pause()
            assert scr.focused is retrieval[0]
            # Back up: retrieval -> key -> the last model option.
            await pilot.press("up")
            await pilot.pause()
            assert scr.focused is key_input
            await pilot.press("up")
            await pilot.pause()
            assert scr.focused is models[-1]

    asyncio.run(drive())


def test_wizard_pilot_reranker_none_advances_without_key():
    """The default 'No reranker' option needs no key, so Enter advances directly."""
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.rag import make_reranker_step

    async def drive() -> WizardState:
        state = WizardState(embedder="local-granite")
        app = SetupWizardApp(state, steps=[make_reranker_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # "No reranker" -> advance (no key prompt)
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.reranker == "none"


def test_wizard_pilot_auth_skip_pins_api_key_even_on_oauth_row():
    """Ctrl+S on the auth step must not leave a deferred OAuth value in state
    (which would silently disable the provider/connection/model steps). It coerces
    to the only wired method.
    """
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.auth import make_auth_method_step

    async def drive() -> WizardState:
        state = WizardState()
        app = SetupWizardApp(state, steps=[make_auth_method_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")  # highlight a deferred CLIProxy OAuth row
            await pilot.pause()
            await pilot.press("ctrl+s")  # skip must pin API key, not keep OAuth
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.auth_method is ProviderAuthMethod.API_KEY


def test_wizard_pilot_provider_switch_clears_stale_connection_state():
    """Switching providers clears api_mode/base_url, so a back-nav that skips the
    now-inapplicable connection step cannot leak the old provider's connection
    details into the written config.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.provider import make_provider_step

    async def drive() -> WizardState:
        # Stand in for a prior OpenRouter connection-step result, then switch.
        state = WizardState(
            provider="openrouter", api_mode="responses", base_url="http://x/v1"
        )
        app = SetupWizardApp(state, steps=[make_provider_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press(*"anthropic")  # filter to a different provider
            await pilot.pause()
            await pilot.press("enter")  # picker enter -> focus key field
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "sk-ant-x"
            await pilot.press("enter")  # advance, committing the provider switch
            await pilot.pause()
        return state

    state = asyncio.run(drive())
    assert state.provider == "anthropic"
    assert state.api_mode == ""  # cleared on the switch
    assert state.base_url == ""  # cleared on the switch


def test_wizard_pilot_auth_step_enters_cliproxy_branch():
    """Selecting subscription OAuth advances into the CLIProxy branch (the
    disclaimer step), and the API-key provider trio drops out of the flow.
    """
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    async def drive() -> SetupWizardApp:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await pilot.press("enter")  # welcome -> tier chooser
            await pilot.pause()
            await pilot.press("down")  # focus Full setup
            await pilot.press("enter")  # pick Full setup -> hosting
            await pilot.pause()
            await pilot.press("enter")  # accept hosting -> api port
            await pilot.pause()
            await pilot.press("enter")  # accept default port -> security profile
            await pilot.pause()
            await pilot.press("enter")  # accept security profile -> auth method
            await pilot.pause()
            assert app.nav.current() == _AUTH_STEP
            await pilot.press("down")  # focus Subscription OAuth via CLIProxy
            await pilot.pause()
            await pilot.press("enter")  # accepted -> the disclaimer step
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert app.state.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
    # Index 7 is cliproxy_disclaimer, the first step of the branch.
    assert app.nav.current() == 7


def test_wizard_pilot_provider_picker_up_arrow_focus_flow():
    """In the provider picker, down enters the list and up at the first row hands
    focus back to the search box (instead of wrapping the highlight to the
    bottom); up from a lower row stays in the list and moves up by one.
    """
    from textual.widgets import Input

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.widgets import PickerOptionList

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # focus the provider picker search
            search = app.screen.query_one("#provider-search", Input)
            option_list = app.screen.query_one(PickerOptionList)
            first = option_list._first_selectable_index()
            assert app.focused is search

            await pilot.press("down")  # into the list, highlight on the first row
            await pilot.pause()
            assert app.focused is option_list
            assert option_list.highlighted == first

            await pilot.press("down")  # move down one selectable row
            await pilot.pause()
            assert option_list.highlighted == first + 1  # type: ignore[unsupported-operation]

            await pilot.press("up")  # back to the first row, still in the list
            await pilot.pause()
            assert app.focused is option_list
            assert option_list.highlighted == first

            await pilot.press("up")  # at the first row -> focus returns to search
            await pilot.pause()
            assert app.focused is search

    asyncio.run(drive())


def test_wizard_pilot_provider_picker_first_row_reveals_top_header():
    """Returning to the first selectable row scrolls the list fully to the top so
    the leading group header is revealed, rather than staying clipped above the
    viewport (a disabled header is never highlighted, so the stock scroll never
    returns to it on its own).
    """
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.widgets import PickerOptionList

    async def drive() -> None:
        app = SetupWizardApp(WizardState())
        async with app.run_test() as pilot:
            await _advance_to_provider(pilot)  # land on the provider picker
            option_list = app.screen.query_one(PickerOptionList)
            # The registry yields more providers than fit, so the list scrolls.
            assert option_list.option_count > 14
            option_list.focus()

            await pilot.press("end")  # jump to the last row: list scrolls down
            await pilot.pause()
            assert option_list.scroll_offset.y > 0

            await pilot.press("home")  # back to the first row: header revealed
            await pilot.pause()
            assert option_list.highlighted == option_list._first_selectable_index()
            assert option_list.scroll_offset.y == 0

    asyncio.run(drive())


def test_finalize_writes_config_without_provider_when_skipped(tmp_path):
    from rich.console import Console

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    state = WizardState(provider=None, root=root, skip_llm_test=True)

    rc = finalize_mod.finalize(
        state, console=Console(), non_interactive=False, overwrite_confirmed=True
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "LLM_PROVIDER" not in config
    assert "DATABASE_BACKEND=sqlite" in config
    assert (root / "data" / "accounts.db").exists()


def test_welcome_env_report_fits_narrow_terminal():
    """The welcome panel wraps long detection notes instead of overflowing.

    Regression for backlog #101 log entry 1: with `#env-report { width: auto }`
    and no max-width cap, a long environment note (e.g. the "Nymeria containers
    are already running here (...)" warning) drove the panel wider than the
    terminal, truncating the note and running the border off the right edge.
    """
    from _setup_wizard_helpers import _env_report  # type: ignore[import-not-found]

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState

    long_note = (
        "Nymeria containers are already running here (nymeria-pypi, "
        "nymeria-caddy, nymeria-api, nymeria-worker): this wizard configures a "
        "separate instance and will not touch them."
    )
    state = WizardState()
    state.env_report = _env_report(notes=[long_note])

    async def drive() -> None:
        app = SetupWizardApp(state)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            report = app.screen.query_one("#env-report")
            # The panel (border included) must fit the 80-column terminal.
            assert report.region.right <= 80
            assert report.outer_size.width <= 80

    asyncio.run(drive())


def test_scroll_hint_clears_at_bottom_of_scrolled_step():
    """The 'more below' cue must clear once the body is scrolled to the end.

    Regression for backlog #101 log entry 9: the cue was recomputed only on
    Resize and error-row toggles, never on scrolling itself, so the review
    step kept saying 'more below' with the scrollbar pinned at the bottom.
    """
    from textual.widgets import Static

    from _setup_wizard_helpers import _env_report  # type: ignore[import-not-found]

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from textual.containers import VerticalScroll

    state = WizardState()
    # Enough notes to force the welcome body to scroll at a short height.
    state.env_report = _env_report(
        notes=[f"Detection note number {i} with enough words to fill a row." for i in range(12)]
    )

    async def drive() -> None:
        app = SetupWizardApp(state)
        async with app.run_test(size=(80, 16)) as pilot:
            await pilot.pause()
            body = app.screen.query_one("#wizard-body", VerticalScroll)
            hint = app.screen.query_one("#wizard-scroll-hint", Static)
            assert body.max_scroll_y > 0, "test needs an overflowing body"
            assert hint.display and "more below" in str(hint.content)

            body.scroll_end(animate=False)
            await pilot.pause()
            assert "more below" not in str(hint.content)
            assert "more above" in str(hint.content)
    asyncio.run(drive())
