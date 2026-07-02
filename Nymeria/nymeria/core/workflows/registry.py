"""The ``nym.*`` verb registry: modularity as a first-class requirement.

Adding a verb later is ONE registration here (or in a sibling module that
calls :func:`register_verb` at import). The child SDK is a generic attribute
proxy that learns the verb list from the handshake, so a new verb needs no
child, protocol, or SDK change (plan: "Verb registry").

``tools.*`` is the one wildcard namespace: the segment after ``tools.`` is an
argument (the tool name), not a registered verb, so it resolves through the
``TOOLS_WILDCARD`` entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .budget import BudgetUsage, WorkflowBudget

logger = logging.getLogger(__name__)

TOOLS_WILDCARD = "tools.*"

# Handler signature: (ctx, verb, args) -> result. ``verb`` is the full dotted
# name from the frame (meaningful for wildcard handlers).
VerbHandler = Callable[["VerbContext", str, dict], Awaitable[Any]]


class VerbError(Exception):
    """A verb handler failed in a way the author's code may want to catch.

    Carries the envelope error ``kind`` so an uncaught ``VerbError`` in the
    child propagates its taxonomy (e.g. ``budget_exceeded``) instead of
    collapsing into ``author_error``.
    """

    def __init__(self, message: str, *, kind: str = "verb_error") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class VerbContext:
    """Per-run context threaded into every verb handler."""

    user_id: str
    thread_id: str
    run_id: str
    workflow_id: str = "adhoc"
    depth: int = 0
    budget: WorkflowBudget = field(default_factory=WorkflowBudget)
    usage: BudgetUsage = field(default_factory=BudgetUsage)


@dataclass(frozen=True)
class VerbSpec:
    """One registered verb: the unit of SDK growth."""

    name: str
    handler: VerbHandler
    side_effect: bool = False
    ai: bool = False
    description: str = ""
    # Names of kwargs that a child-side POSITIONAL call maps onto, in order
    # (e.g. ("prompt",) lets authors write nym.llm("...")). Shipped to the
    # child in the welcome frame; the child never hardcodes verb knowledge.
    positional: tuple = ()


_REGISTRY: Dict[str, VerbSpec] = {}


def register_verb(
    name: str,
    *,
    side_effect: bool = False,
    ai: bool = False,
    description: str = "",
    positional: tuple = (),
) -> Callable[[VerbHandler], VerbHandler]:
    """Register ``handler`` as the ``nym`` verb ``name`` (decorator).

    Re-registration replaces (module reload friendliness) with a log line so
    an accidental collision is visible.
    """

    def _decorator(handler: VerbHandler) -> VerbHandler:
        if name in _REGISTRY:
            logger.info("workflow verb %r re-registered", name)
        _REGISTRY[name] = VerbSpec(
            name=name,
            handler=handler,
            side_effect=side_effect,
            ai=ai,
            description=description,
            positional=tuple(positional),
        )
        return handler

    return _decorator


def resolve_verb(name: str) -> Optional[VerbSpec]:
    """Exact registry hit, else the ``tools.*`` wildcard for ``tools.<name>``."""
    spec = _REGISTRY.get(name)
    if spec is not None:
        return spec
    if name.startswith("tools.") and len(name) > len("tools."):
        return _REGISTRY.get(TOOLS_WILDCARD)
    return None


def registered_verbs() -> List[str]:
    return sorted(_REGISTRY)


def verb_metadata() -> Dict[str, dict]:
    """The welcome-frame payload: what the child SDK needs, nothing more."""
    return {
        spec.name: {"positional": list(spec.positional)}
        for spec in _REGISTRY.values()
    }


def load_builtin_verbs() -> None:
    """Import the built-in verb modules (idempotent; function-local imports).

    Grows one line per verb module as phases land. Import errors are raised,
    not swallowed: an engine missing its built-ins is a deployment bug.
    """
    from . import verbs_effects  # noqa: F401 - registration happens at import
    from . import verbs_llm  # noqa: F401
    from . import verbs_thread  # noqa: F401
    from . import verbs_tools  # noqa: F401
