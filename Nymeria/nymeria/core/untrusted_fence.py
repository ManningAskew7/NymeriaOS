"""One fence idiom for content the model must read as data, not instructions.

Three emit sites grew the same shape independently (browser page text,
Twitch chat lines, Claude Code transcript output): an opening and closing
marker around the body, with any closing marker INSIDE the body neutralized
so the content cannot end the fence early and continue as trusted narration.
The neutralizer is separator-tolerant: ``< /tag``, ``</ tag``, mixed case,
and zero-width characters wedged between the parts all count as the marker,
because matching only the literal string let a page close the fence early.
Each site keeps its own tag (the model sees WHICH kind of data it is) and
its own note line; this module owns the mechanics so a separator-class fix
lands once.
"""

from __future__ import annotations

import re

# Whitespace plus the zero-width family a page or file can wedge between the
# parts of a closing marker.
SEPARATOR = r"[\s\u200b-\u200f\u2060\ufeff]*"


class UntrustedFence:
    """The open/close markers and neutralizer for one ``tag``."""

    __slots__ = ("tag", "open", "close", "close_re", "_replacement")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.open = f"<{tag}>"
        self.close = f"</{tag}>"
        self.close_re = re.compile(
            SEPARATOR.join([r"<", r"/", *list(tag)]), re.IGNORECASE
        )
        # A literal backslash after the "<" breaks the marker for every
        # reader while leaving the text legible.
        self._replacement = f"<\\\\/{tag}"

    def neutralize(self, text: str) -> tuple[str, int]:
        """``text`` with every embedded closing marker defused, plus the count."""
        return self.close_re.subn(self._replacement, text)

    def wrap(self, text: str, *, note: str = "") -> str:
        """``note`` (the emitter's own provenance line, may be empty) followed
        by the fenced, neutralized body."""
        body, _ = self.neutralize(text)
        fenced = f"{self.open}\n{body}\n{self.close}"
        return f"{note}\n{fenced}" if note else fenced
