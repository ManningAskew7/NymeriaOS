"""Workflow SDK engine (the ``nym`` runtime).

Phase 1 of ``docs/private/plans/workflow-tools.md``: subprocess execution of
agent-authored workflow code with a private, per-run, token-authenticated RPC
channel back to this (the API) process. The child runs author Python against a
generic ``nym`` proxy; every ``nym.*`` verb is answered here by the verb
registry, budget-charged, step-traced, and scoped to the calling user.

Public surface:

- :func:`execute_workflow` runs one workflow source end to end and returns a
  :class:`WorkflowRunResult` (normalized envelope plus step trace).
- :func:`register_verb` adds a ``nym.*`` verb (one registration per verb; the
  child SDK is verb-agnostic and needs no change).
- :class:`WorkflowBudget` carries every cap for a run.
"""

from .budget import BudgetUsage, WorkflowBudget, WorkflowBudgetExceeded
from .envelope import WorkflowEnvelope, WorkflowError
from .executor import WorkflowRunResult, execute_workflow
from .registry import VerbContext, VerbError, VerbSpec, register_verb, registered_verbs

__all__ = [
    "BudgetUsage",
    "VerbContext",
    "VerbError",
    "VerbSpec",
    "WorkflowBudget",
    "WorkflowBudgetExceeded",
    "WorkflowEnvelope",
    "WorkflowError",
    "WorkflowRunResult",
    "execute_workflow",
    "register_verb",
    "registered_verbs",
]
