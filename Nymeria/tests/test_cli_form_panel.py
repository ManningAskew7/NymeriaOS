from __future__ import annotations

from nymeria.triggers.cli.commands.base import CommandResult
from nymeria.triggers.cli.rendering.form_panel import (
    FORM_PANEL_MAX_ROWS,
    _TEXT_FOOTER,
    _TEXT_FOOTER_SINGLE_TAB,
    FormField,
    FormOption,
    FormResult,
    FormSpec,
    FormTab,
    active_input_is_secret,
    build_result,
    draft_for,
    filter_options,
    form_panel_fragments,
    form_panel_height,
    init_state,
    input_field,
    move_selection,
    move_tab,
    sync_filter,
    toggle_current,
    visible_options,
)


async def _noop(_result: FormResult) -> CommandResult:  # pragma: no cover - stub
    return CommandResult.completed()


def _rendered(fragments) -> str:
    return "".join(fragment[1] for fragment in fragments)


def _radio_spec(option_ids: list[str], *, current: str | None = None) -> FormSpec:
    options = tuple(
        FormOption(id=name, label=name, meta="200,000 ctx", current=name == current)
        for name in option_ids
    )
    return FormSpec(
        title="Select model",
        tabs=(
            FormTab(
                label="Models",
                fields=(
                    FormField(kind="search", key="filter", placeholder="Filter…"),
                    FormField(kind="radio", key="model", options=options),
                ),
            ),
        ),
        on_confirm=_noop,
    )


def _checkbox_spec(option_ids: list[str], *, current: set[str] | None = None) -> FormSpec:
    chosen = current or set()
    options = tuple(
        FormOption(id=name, label=name, current=name in chosen) for name in option_ids
    )
    return FormSpec(
        title="Tools",
        tabs=(
            FormTab(
                label="Tools",
                fields=(FormField(kind="checkbox", key="tools", options=options),),
            ),
        ),
        on_confirm=_noop,
    )


def _multi_tab_spec() -> FormSpec:
    return FormSpec(
        title="Settings",
        tabs=(
            FormTab(
                label="Model",
                fields=(FormField(kind="radio", key="model", options=(
                    FormOption(id="a", label="a"),
                    FormOption(id="b", label="b"),
                )),),
            ),
            FormTab(
                label="Tools",
                fields=(FormField(kind="checkbox", key="tools", options=(
                    FormOption(id="x", label="x"),
                )),),
            ),
        ),
        on_confirm=_noop,
    )


def _text_spec(*, secret: bool = False) -> FormSpec:
    return FormSpec(
        title="API key",
        tabs=(
            FormTab(
                label="Key",
                fields=(
                    FormField(
                        kind="text",
                        key="api_key",
                        label="API key",
                        placeholder="sk-...",
                        secret=secret,
                    ),
                ),
            ),
        ),
        on_confirm=_noop,
    )


def test_text_only_tab_renders_input_line_and_text_footer() -> None:
    spec = _text_spec()
    state = init_state(spec)

    # header + input line + footer, no list rows.
    assert form_panel_height(spec, state) == 3
    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    # The row is an instruction pointing at the composer, never a text-box
    # look-alike (`label: placeholder` read as a second, unreachable input;
    # the label lives on the composer prompt, which IS the field).
    assert "API key:" not in rendered
    # Points UP: the panel renders under the composer, so the instruction
    # must aim at it (a "below" arrow pointed away, reported 2026-08-02).
    assert "↑ type or paste in the prompt above" in rendered
    assert "e.g. sk-..." in rendered  # placeholder demoted to an example
    assert "Enter submit" in rendered

    # A text step's value is NOT echoed here. The composer owns the caret and is
    # the single surface that shows it; the panel reports its shape, so the two
    # do not read as two separate input boxes.
    sync_filter(spec, state, "sk-test-123")
    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "sk-test-123" not in rendered
    assert "11 chars entered" in rendered


def test_busy_text_row_stops_advertising_enter() -> None:
    """While the step is in flight the row must not contradict the Working
    footer: Enter is a no-op until the result lands."""

    spec = _text_spec()
    state = init_state(spec)
    sync_filter(spec, state, "sk-test-123")
    state.busy = True

    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "Enter to submit" not in rendered
    assert "11 chars entered · submitted, working…" in rendered
    assert "Working…" in rendered  # the busy footer

    # A busy step with no typed value skips the empty count.
    state.filter_text = ""
    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "0 chars" not in rendered
    assert "submitted, working…" in rendered


def test_search_field_keeps_its_inline_filter_text() -> None:
    """The dedupe is text-field only: a filter sits above the list it filters."""

    spec = _radio_spec(["sonnet-4-6", "opus-4-8"])
    state = init_state(spec)
    sync_filter(spec, state, "sonnet")

    rendered = _rendered(form_panel_fragments(spec, state, width=48))
    assert "Filter: sonnet" in rendered


def test_secret_text_reports_length_and_never_the_value_or_a_bullet_run() -> None:
    spec = _text_spec(secret=True)
    state = init_state(spec)
    sync_filter(spec, state, "sk-secret-value")

    rendered = _rendered(form_panel_fragments(spec, state, width=40))
    assert "sk-secret-value" not in rendered
    # Deliberately no mask run either: a truncated bullet stream is identical
    # whether 40 or 400 characters landed, which is what made a long paste
    # impossible to verify. Masking is the composer's job (see the getter below,
    # which drives its PasswordProcessor).
    assert "•" not in rendered
    assert "15 chars entered" in rendered

    # The composer-masking getter tracks the active tab's field.
    assert active_input_is_secret(spec, state) is True
    assert active_input_is_secret(_text_spec(secret=False), state) is False
    assert active_input_is_secret(None, None) is False


def test_input_field_prefers_first_typed_input_and_result_carries_text() -> None:
    spec = _text_spec(secret=True)
    state = init_state(spec)
    field = input_field(spec.tabs[0])
    assert field is not None and field.kind == "text"

    sync_filter(spec, state, "the-value")
    result = build_result(spec, state)
    assert result.filter_text == "the-value"
    assert result.radio_value is None


def test_init_state_parks_on_the_active_tab() -> None:
    # Step-rail forms flag the tab to open on; init_state honors the FIRST
    # active flag and defaults to tab 0 when none is set.
    spec = FormSpec(
        title="Setup",
        tabs=(
            FormTab(label="Key", fields=(FormField(kind="radio", key="k", options=(
                FormOption(id="keep", label="keep"),
            )),)),
            FormTab(label="Model", active=True, fields=(FormField(kind="radio", key="m", options=(
                FormOption(id="a", label="a"),
            )),)),
        ),
        on_confirm=_noop,
    )
    state = init_state(spec)
    assert state.active_tab == 1
    assert build_result(spec, state).tab_label == "Model"

    plain = init_state(_multi_tab_spec())
    assert plain.active_tab == 0


def test_height_is_zero_when_empty() -> None:
    assert form_panel_height(None, None) == 0


def test_height_single_tab_radio_with_search() -> None:
    spec = _radio_spec(["one", "two", "three"])
    state = init_state(spec)
    # header(1) + search(1) + 3 rows + footer(1) = 6; no tab bar for one tab.
    assert form_panel_height(spec, state) == 6


def test_height_caps_with_overflow_indicator() -> None:
    spec = _radio_spec([f"m{index:02d}" for index in range(15)])
    state = init_state(spec)
    # header(1) + search(1) + 10 visible + 1 overflow + footer(1)
    assert form_panel_height(spec, state) == 14
    assert FORM_PANEL_MAX_ROWS == 10


def test_tab_bar_only_renders_with_multiple_tabs() -> None:
    single = _radio_spec(["one"])
    single_state = init_state(single)
    single_text = _rendered(form_panel_fragments(single, single_state, width=60))
    assert "Models" not in single_text.split("\n")[0]  # header line is the title

    multi = _multi_tab_spec()
    multi_state = init_state(multi)
    multi_text = _rendered(form_panel_fragments(multi, multi_state, width=60))
    assert "Model" in multi_text and "Tools" in multi_text


def test_filter_options_is_case_insensitive_substring() -> None:
    field = FormField(
        kind="radio",
        key="model",
        options=(
            FormOption(id="claude-opus-4-8", label="claude-opus-4-8"),
            FormOption(id="gpt-5.5", label="gpt-5.5"),
            FormOption(id="claude-sonnet-4-6", label="claude-sonnet-4-6"),
        ),
    )
    matches = filter_options(field, "CLAUDE")
    assert [option.id for option in matches] == [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    ]
    assert filter_options(field, "") == list(field.options)


def test_init_state_parks_radio_cursor_on_current() -> None:
    spec = _radio_spec(["one", "two", "three"], current="three")
    state = init_state(spec)
    assert state.selected_index["model"] == 2
    assert build_result(spec, state).radio_value == "three"


def test_move_selection_wraps() -> None:
    spec = _radio_spec(["one", "two", "three"], current="three")
    state = init_state(spec)
    assert move_selection(spec, state, 1) is True
    assert build_result(spec, state).radio_value == "one"


def test_server_and_client_text_footers_agree() -> None:
    """These two spellings are duplicated on purpose and must not drift.

    The backend ships a `footer_hint` with each form spec
    (`core.command_forms.chain_footer`), and this renderer carries its own
    fallback for specs that omit one. They cannot share a constant: the thin
    CLI must never import `nymeria.core` (one module-level import there cost
    14.9s of a 16.4s launch), so this test is the only thing holding them
    together.
    """

    from nymeria.core.command_forms import chain_footer

    text_tab = {"fields": [{"kind": "text"}]}
    assert chain_footer(text_tab, 3) == _TEXT_FOOTER
    assert chain_footer(text_tab, 1) == _TEXT_FOOTER_SINGLE_TAB


def test_move_tab_switches_and_is_noop_for_single_tab() -> None:
    multi = _multi_tab_spec()
    multi_state = init_state(multi)
    assert move_tab(multi, multi_state, 1, typed="") == ""
    assert build_result(multi, multi_state).tab_label == "Tools"

    single = _radio_spec(["one", "two"])
    single_state = init_state(single)
    assert move_tab(single, single_state, 1, typed="") is None


def test_move_tab_stashes_and_restores_each_steps_typed_value() -> None:
    """Navigating the rail must never destroy a typed value.

    Regression: the runtime used to clear the composer on every step change, so
    a single stray Left/Right threw away a pasted OAuth callback (~330 chars,
    single-use, unrecoverable). Each step now keeps its own draft, exchanged
    through the call itself so no caller can get the ordering wrong.
    """

    spec = _multi_tab_spec()
    state = init_state(spec)

    # The incoming step starts empty rather than inheriting the neighbour's text.
    assert move_tab(spec, state, 1, typed="first-step-value") == ""
    assert state.filter_text == ""

    assert move_tab(spec, state, -1, typed="second-step-value") == (
        "first-step-value"
    )
    assert state.filter_text == "first-step-value"
    assert draft_for(state) == "first-step-value"

    assert move_tab(spec, state, 1, typed="first-step-value") == (
        "second-step-value"
    )
    assert state.filter_text == "second-step-value"


def test_move_tab_takes_the_live_value_over_the_last_rendered_one() -> None:
    """The composer is the source of truth, not filter_text.

    filter_text only tracks the composer as of the last render, so a value typed
    and immediately arrowed away from would otherwise stash stale text.
    """

    spec = _multi_tab_spec()
    state = init_state(spec)
    sync_filter(spec, state, "stale-from-last-render")

    move_tab(spec, state, 1, typed="what-the-user-actually-typed")
    assert move_tab(spec, state, -1, typed="") == "what-the-user-actually-typed"


def test_toggle_current_flips_checkbox_membership() -> None:
    spec = _checkbox_spec(["read", "write", "exec"], current={"read"})
    state = init_state(spec)
    # cursor starts at index 0 ("read"), which is pre-checked -> toggling removes it.
    assert toggle_current(spec, state) is True
    assert build_result(spec, state).checkbox_values == ()
    assert move_selection(spec, state, 1) is True  # -> "write"
    assert toggle_current(spec, state) is True
    assert build_result(spec, state).checkbox_values == ("write",)


def test_toggle_current_noop_for_radio() -> None:
    spec = _radio_spec(["one", "two"])
    state = init_state(spec)
    assert toggle_current(spec, state) is False


def test_sync_filter_strips_newlines() -> None:
    # A pasted model id with a trailing newline (or a Ctrl+J in the buffer) must
    # not survive into the single-line filter, or the search row would render an
    # extra line that form_panel_height does not count (footer clip).
    spec = _radio_spec(["gpt-5.5", "claude-opus-4-8"])
    state = init_state(spec)
    sync_filter(spec, state, "gpt-5.5\n")
    assert state.filter_text == "gpt-5.5"
    assert [option.id for option in visible_options(spec, state)] == ["gpt-5.5"]
    # header(1) + search(1) + 1 matching row + footer(1); no stray extra row.
    assert form_panel_height(spec, state) == 4


def test_sync_filter_clamps_selection() -> None:
    spec = _radio_spec(["alpha", "beta", "gamma"])
    state = init_state(spec)
    move_selection(spec, state, 2)  # cursor on "gamma" (index 2)
    sync_filter(spec, state, "alpha")  # only one match now
    result = build_result(spec, state)
    assert result.radio_value == "alpha"


def test_fragments_render_markers_caret_and_pad_to_width() -> None:
    spec = _radio_spec(["one", "two", "three"], current="two")
    state = init_state(spec)  # cursor on "two"
    fragments = form_panel_fragments(spec, state, width=50)
    rendered = _rendered(fragments)
    assert "Select model" in rendered  # header/title
    assert "◉" in rendered  # current marker
    assert "○" in rendered  # non-current markers
    assert "›" in rendered  # caret on the selected row

    # Every rendered line pads to (but does not exceed) the panel width.
    line_widths: list[int] = []
    accumulator = 0
    for fragment in fragments:
        text = fragment[1]
        if "\n" in text:
            line_widths.append(accumulator)
            accumulator = 0
        else:
            accumulator += len(text)
    line_widths.append(accumulator)
    assert all(0 < width <= 50 for width in line_widths)


def test_fragments_checkbox_markers_reflect_toggle() -> None:
    spec = _checkbox_spec(["read", "write"], current={"read"})
    state = init_state(spec)
    rendered = _rendered(form_panel_fragments(spec, state, width=40))
    assert "[x] read" in rendered
    assert "[ ] write" in rendered


def test_fragments_show_overflow_indicator() -> None:
    spec = _radio_spec([f"m{index:02d}" for index in range(15)])
    state = init_state(spec)
    rendered = _rendered(form_panel_fragments(spec, state, width=40))
    assert "↓ 5 more below" in rendered
    assert "above" not in rendered


def test_fragments_overflow_counts_both_directions() -> None:
    # The window anchors the cursor at its last row once scrolling starts,
    # so rows hide on BOTH sides; the indicator must name each side with
    # its own count instead of blaming every hidden row on "below".
    spec = _radio_spec([f"m{index:02d}" for index in range(15)])
    state = init_state(spec)
    for _ in range(11):  # cursor to index 11: window shows rows 2-11
        move_selection(spec, state, 1)
    rendered = _rendered(form_panel_fragments(spec, state, width=40))
    assert "↑ 2 more above" in rendered
    assert "↓ 3 more below" in rendered

    for _ in range(3):  # cursor to the last row: everything hidden is above
        move_selection(spec, state, 1)
    bottom = _rendered(form_panel_fragments(spec, state, width=40))
    assert "↑ 5 more above" in bottom
    assert "below" not in bottom


# ── fieldless action tabs (#139) ─────────────────────────────────────────────


def _action_spec(*, description: str = "Send a probe request", tabs: int = 1) -> FormSpec:
    action = FormTab(label="Test", fields=(), description=description)
    extra = tuple(
        FormTab(
            label=f"Other{index}",
            fields=(
                FormField(
                    kind="radio",
                    key=f"value{index}",
                    options=(FormOption(id="a", label="a"),),
                ),
            ),
        )
        for index in range(tabs - 1)
    )
    return FormSpec(title="Provider: Anthropic", tabs=(action, *extra), on_confirm=_noop)


def test_action_tab_renders_description_row_and_run_footer() -> None:
    spec = _action_spec()
    state = init_state(spec)

    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "▸ Send a probe request" in rendered
    assert "Enter run · Esc close" in rendered

    multi = _action_spec(tabs=2)
    multi_state = init_state(multi)
    multi_rendered = _rendered(form_panel_fragments(multi, multi_state, width=60))
    assert "←→ step · Enter run · Esc close" in multi_rendered


def test_action_tab_without_description_shows_run_instruction() -> None:
    spec = _action_spec(description="")
    state = init_state(spec)
    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "▸ Press Enter to run" in rendered


def test_action_tab_height_counts_its_description_row() -> None:
    spec = _action_spec()
    state = init_state(spec)
    # header + description row + footer
    assert form_panel_height(spec, state) == 3


def test_action_tab_busy_row_reports_in_flight() -> None:
    spec = _action_spec()
    state = init_state(spec)
    state.busy = True
    rendered = _rendered(form_panel_fragments(spec, state, width=60))
    assert "submitted, working…" in rendered
    assert "Working… · Esc dismiss" in rendered


def test_action_tab_build_result_is_valueless() -> None:
    spec = _action_spec()
    state = init_state(spec)
    result = build_result(spec, state)
    assert result.tab_label == "Test"
    assert result.radio_value is None
    assert result.checkbox_values == ()
