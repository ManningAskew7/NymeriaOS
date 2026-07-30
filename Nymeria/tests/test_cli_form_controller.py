from __future__ import annotations

from types import SimpleNamespace

from nymeria.triggers.cli.input import ComposerController, ComposerSubmission


def test_form_hooks_are_gated_by_active_state() -> None:
    active = False
    moves: list[int] = []
    tabs: list[int] = []
    toggles: list[bool] = []
    submits: list[bool] = []
    cancels: list[bool] = []
    checkbox = False

    controller = ComposerController(
        form_is_active=lambda: active,
        form_tab_enabled=lambda: active,
        # Up/Down are gated on the active step HAVING a list, so this test's
        # radio-field scenario has to say so.
        form_has_navigable_list=lambda: active,
        active_field_is_checkbox=lambda: checkbox,
        on_form_move=moves.append,
        on_form_tab=tabs.append,
        on_form_toggle=lambda: toggles.append(True),
        on_form_submit=lambda: submits.append(True),
        on_form_cancel=lambda: cancels.append(True),
    )

    # Inactive: every form action is inert.
    assert controller.form_move_enabled() is False
    assert controller.form_tab_nav_enabled() is False
    assert controller.form_submit_enabled() is False
    assert controller.form_cancel_enabled() is False
    assert controller.move_form_selection(1) is False
    assert controller.move_form_tab(1) is False
    assert controller.submit_form_selection() is False
    assert controller.cancel_form() is False
    assert (moves, tabs, submits, cancels) == ([], [], [], [])

    # Active (radio field): nav/submit/cancel fire, but Space-toggle stays off.
    active = True
    assert controller.form_move_enabled() is True
    assert controller.form_tab_nav_enabled() is True
    assert controller.form_toggle_enabled() is False
    assert controller.toggle_form_option() is False
    assert controller.move_form_selection(-1) is True
    assert controller.move_form_tab(1) is True
    assert controller.submit_form_selection() is True
    assert controller.cancel_form() is True
    assert moves == [-1]
    assert tabs == [1]
    assert submits == [True]
    assert cancels == [True]


def test_up_down_on_a_text_step_never_reach_shell_history() -> None:
    """A text step must own Up/Down, or the editor default eats the value.

    prompt_toolkit's default Up/Down are ``auto_up``/``auto_down``, which recall
    shell history whenever the cursor is on the first LOGICAL line: a 330-char
    pasted OAuth callback wraps over many display rows but is still one logical
    line, so the very first Up would swap a single-use code for a previous
    prompt. Merely leaving the keys unbound on a list-less step is therefore not
    safe. Resolve the real merged table (defaults + ours) and pin the winner.
    """

    from prompt_toolkit.key_binding import merge_key_bindings
    from prompt_toolkit.key_binding.defaults import load_key_bindings
    from prompt_toolkit.keys import Keys

    def winning_handler(*, form_active: bool, has_list: bool, key: Keys) -> str | None:
        controller = ComposerController(
            form_is_active=lambda: form_active,
            form_tab_enabled=lambda: form_active,
            form_has_navigable_list=lambda: has_list,
            on_form_move=lambda _n: None,
            on_form_tab=lambda _n: None,
        )
        merged = merge_key_bindings([load_key_bindings(), controller.key_bindings])
        # prompt_toolkit picks the LAST match whose filter is true.
        live = [b for b in merged.get_bindings_for_keys((key,)) if b.filter()]
        return live[-1].handler.__name__ if live else None

    # Text step: our caret bindings win, so history is unreachable.
    assert (
        winning_handler(form_active=True, has_list=False, key=Keys.Up)
        == "_form_caret_up"
    )
    assert (
        winning_handler(form_active=True, has_list=False, key=Keys.Down)
        == "_form_caret_down"
    )
    # A step that HAS a list still drives the list.
    assert winning_handler(form_active=True, has_list=True, key=Keys.Up) == "_form_up"
    assert (
        winning_handler(form_active=True, has_list=True, key=Keys.Down) == "_form_down"
    )
    # No form: we claim neither key, leaving normal editor/history behavior.
    # (Outside an Application the editor defaults' own filters read false, so
    # this asserts only that none of OUR handlers win.)
    assert winning_handler(form_active=False, has_list=False, key=Keys.Up) is None
    assert winning_handler(form_active=False, has_list=False, key=Keys.Down) is None


def test_form_caret_bindings_move_the_cursor_without_touching_the_text() -> None:
    controller = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: False,
    )
    buffer = controller.text_area.buffer
    buffer.text = "line one\nline two"
    buffer.cursor_position = len(buffer.text)

    assert controller.form_caret_move_enabled() is True

    handlers = {
        binding.handler.__name__: binding.handler
        for binding in controller.key_bindings.bindings
    }
    event = SimpleNamespace(current_buffer=buffer, arg=1)
    handlers["_form_caret_up"](event)

    assert buffer.text == "line one\nline two"  # value intact, no history recall
    assert buffer.document.cursor_position_row == 0


def test_space_toggle_only_enabled_for_checkbox_field() -> None:
    checkbox = False
    toggles: list[bool] = []
    controller = ComposerController(
        form_is_active=lambda: True,
        active_field_is_checkbox=lambda: checkbox,
        on_form_toggle=lambda: toggles.append(True),
    )

    assert controller.form_toggle_enabled() is False
    assert controller.toggle_form_option() is False

    checkbox = True
    assert controller.form_toggle_enabled() is True
    assert controller.toggle_form_option() is True
    assert toggles == [True]


def test_enter_submits_form_when_active_else_submits_buffer() -> None:
    active = True
    submissions: list[ComposerSubmission] = []
    form_submits: list[bool] = []
    controller = ComposerController(
        on_submit=submissions.append,
        form_is_active=lambda: active,
        on_form_submit=lambda: form_submits.append(True),
    )
    buffer = controller.text_area.buffer
    buffer.text = "hello"
    buffer.cursor_position = len(buffer.text)

    # While a form is active, Enter routes to the form, never the buffer.
    assert controller.submit_form_selection() is True
    assert form_submits == [True]
    assert submissions == []
    assert buffer.text == "hello"  # buffer untouched by form submit

    # With no form, Enter submits the buffer as a normal message.
    active = False
    assert controller.submit_form_selection() is False
    assert controller.handle_enter(buffer) is True
    assert [submission.message for submission in submissions] == ["hello"]
