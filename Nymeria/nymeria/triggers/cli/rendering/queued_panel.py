"""Under-composer panel listing queued prompts (backlog #38).

While a turn is streaming, submissions queue in the Rich runtime's
client-local deque. This panel renders below the composer whenever the queue
is non-empty so the user can see exactly which prompts are waiting and in
what order. Queued text is deliberately NOT echoed into the transcript at
enqueue time: the transcript reflects true delivery order (the user sees what
the agent sees), so a queued message appears there only when the drain
actually sends it.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from rich.cells import cell_len

from .markdown import truncate_cell_width

if TYPE_CHECKING:
    from collections.abc import Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

QUEUED_PANEL_MAX_ROWS = 3


def submission_text(submission: Any) -> str:
    """Best-effort single-line text for one queued submission.

    ``ComposerSubmission`` carries the parsed prompt as ``message`` (the text
    the drain will actually deliver); plain strings queue as-is in tests.
    """

    text = getattr(submission, "message", None)
    if text is None:
        text = submission
    return " ".join(str(text or "").split())


def queued_panel_height(
    submissions: "Sequence[Any]",
    *,
    max_rows: int = QUEUED_PANEL_MAX_ROWS,
) -> int:
    """Return the number of rows the panel will occupy (0 when hidden)."""

    count = len(submissions)
    if count <= 0:
        return 0
    visible = min(count, max(1, max_rows))
    overflow = 1 if count > visible else 0
    return visible + overflow


def queued_panel_fragments(
    submissions: "Sequence[Any]",
    *,
    width: int,
    max_rows: int = QUEUED_PANEL_MAX_ROWS,
) -> "StyleAndTextTuples":
    """Render the queued prompts as prompt_toolkit fragments, oldest first."""

    count = len(submissions)
    if count <= 0:
        return []

    panel_width = max(8, int(width or 0))
    visible = list(submissions)[: max(1, max_rows)]
    hidden_count = count - len(visible)

    fragments: "StyleAndTextTuples" = []
    for index, submission in enumerate(visible):
        label = f"queued {index + 1}: "
        text_width = max(1, panel_width - cell_len(label))
        line = truncate_cell_width(submission_text(submission), text_width)
        used = cell_len(label) + cell_len(line)
        fragments.append(("class:queued-panel.index", label))
        fragments.append(("class:queued-panel", line))
        if used < panel_width:
            fragments.append(("class:queued-panel", " " * (panel_width - used)))
        if index < len(visible) - 1 or hidden_count > 0:
            fragments.append(("", "\n"))

    if hidden_count > 0:
        hint = f"+{hidden_count} more queued"
        pad = max(0, panel_width - cell_len(hint))
        fragments.append(("class:queued-panel.more", hint + " " * pad))

    return fragments
