"""Pure navigation model for the wizard.

Kept free of Textual so the forward/back/skip logic is unit-testable without a
terminal. The Textual app delegates index decisions here and only translates
them into push_screen / pop_screen calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .state import WizardState


@dataclass
class Step:
    """One wizard step: an applicability predicate and a screen factory.

    `build` is only used by the Textual app; the navigator never calls it, which
    keeps navigation testable in isolation.
    """

    id: str
    applies: Callable[["WizardState"], bool]
    build: Callable[..., object]


class Navigator:
    """Walks the step list honoring per-step applicability, with a back stack."""

    def __init__(self, steps: list[Step], state: "WizardState") -> None:
        self.steps = steps
        self.state = state
        self.history: list[int] = []

    def _next_applicable(self, start: int) -> int | None:
        for i in range(start, len(self.steps)):
            if self.steps[i].applies(self.state):
                return i
        return None

    def applicable_indices(self) -> list[int]:
        return [i for i, step in enumerate(self.steps) if step.applies(self.state)]

    def current(self) -> int | None:
        return self.history[-1] if self.history else None

    def start(self) -> int | None:
        first = self._next_applicable(0)
        self.history = [] if first is None else [first]
        return first

    def advance(self) -> int | None:
        """Move to the next applicable step. Return None when finished."""
        cur = self.current()
        start = 0 if cur is None else cur + 1
        nxt = self._next_applicable(start)
        if nxt is None:
            return None
        self.history.append(nxt)
        return nxt

    def back(self) -> int | None:
        """Pop to the previous step. Return None when already at the first."""
        if len(self.history) <= 1:
            return None
        self.history.pop()
        return self.history[-1]

    def at_start(self) -> bool:
        return len(self.history) <= 1

    def position(self) -> tuple[int, int]:
        """Return (number, total) of the current step among applicable steps."""
        applicable = self.applicable_indices()
        cur = self.current()
        if cur is None or cur not in applicable:
            return (0, len(applicable))
        return (applicable.index(cur) + 1, len(applicable))


__all__ = ["Step", "Navigator"]
