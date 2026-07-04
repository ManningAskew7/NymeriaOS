from __future__ import annotations

from nymeria.triggers.cli.input import ComposerSubmission
from nymeria.triggers.cli.rendering.queued_panel import (
    queued_panel_fragments,
    queued_panel_height,
    submission_text,
)


def _texts(fragments) -> str:
    return "".join(fragment for _style, fragment in fragments)


def test_queued_panel_hidden_when_queue_is_empty() -> None:
    assert queued_panel_height(()) == 0
    assert queued_panel_fragments((), width=80) == []


def test_queued_panel_lists_pending_prompts_oldest_first() -> None:
    submissions = (
        ComposerSubmission("first message"),
        ComposerSubmission("second message"),
    )

    assert queued_panel_height(submissions) == 2
    rendered = _texts(queued_panel_fragments(submissions, width=80))
    assert "queued 1: first message" in rendered
    assert "queued 2: second message" in rendered
    assert rendered.index("first message") < rendered.index("second message")


def test_queued_panel_caps_rows_and_reports_overflow() -> None:
    submissions = tuple(ComposerSubmission(f"msg {i}") for i in range(1, 6))

    # Three visible rows plus one overflow hint row.
    assert queued_panel_height(submissions) == 4
    rendered = _texts(queued_panel_fragments(submissions, width=80))
    assert "msg 3" in rendered
    assert "msg 4" not in rendered
    assert "+2 more queued" in rendered


def test_queued_panel_truncates_and_flattens_multiline_text() -> None:
    long_text = "line one\nline two " + "x" * 200
    submissions = (ComposerSubmission(long_text),)

    assert submission_text(submissions[0]).startswith("line one line two")
    rendered = _texts(queued_panel_fragments(submissions, width=40))
    lines = rendered.split("\n")
    assert len(lines) == 1
    assert all(len(line) <= 40 for line in lines)
    assert "line one" in rendered
