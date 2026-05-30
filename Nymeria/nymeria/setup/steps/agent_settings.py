"""Placeholder agent-settings step (max iterations, compaction, etc.)."""

from __future__ import annotations

from ..nav import Step
from .base import Choice, placeholder_step


def make_agent_settings_step() -> Step:
    return placeholder_step(
        step_id="agent_settings",
        title="Agent settings",
        note=(
            "Placeholder. Tuning (max iterations, tool-progress display, "
            "context compaction) is not wired up yet."
        ),
        options=[
            Choice("balanced", "Balanced defaults", "Recommended starting point."),
            Choice("thorough", "Thorough", "More iterations, later compaction."),
            Choice("fast", "Fast", "Fewer iterations, earlier compaction."),
        ],
    )


__all__ = ["make_agent_settings_step"]
