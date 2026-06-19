from __future__ import annotations

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
