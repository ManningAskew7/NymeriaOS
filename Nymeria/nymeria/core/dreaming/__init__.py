"""Dreaming (self-reflection) subsystem.

Dreams run in a shadow thread spawned for one parent thread. The shadow thread
reuses the normal agent runtime but with a different system prompt
(``config/dream_prompt.md``), a tight tool whitelist, and ``shadow_parent_id``
set on its ``ThreadConfig`` so dream-only tools (and the standard memory/TODO
tools, transparently) target the parent.
"""

from .invoke import (
    DEFAULT_DREAM_ENABLED_CORE_TOOLS,
    DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS,
    DEFAULT_DREAM_DISABLED_CORE_TOOLS,
    DreamInvocationError,
    invoke_dream,
)
from .scheduler import (
    DREAM_SWEEP_INTERVAL_SECONDS,
    DreamDecision,
    evaluate_dream_eligibility,
    sweep_dreamable_threads,
)

__all__ = [
    "DEFAULT_DREAM_ENABLED_CORE_TOOLS",
    "DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS",
    "DEFAULT_DREAM_DISABLED_CORE_TOOLS",
    "DreamInvocationError",
    "invoke_dream",
    "DREAM_SWEEP_INTERVAL_SECONDS",
    "DreamDecision",
    "evaluate_dream_eligibility",
    "sweep_dreamable_threads",
]
