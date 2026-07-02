"""The normalized workflow result envelope and its error taxonomy.

One stable shape returned to EVERY caller (agent tool call, ``tool_invoke``,
trigger, cron), after openclaw Lobster's normalized runner result. The error
taxonomy is what powers the authoring agent's self-repair loop: an
``author_error`` carries the traceback of the code the agent itself wrote.

Statuses: ``ok | error | timeout | cancelled | needs_approval``.
``needs_approval`` is reserved for the phase 4 suspend/resume checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_NEEDS_APPROVAL = "needs_approval"

# Error kinds (plan: "Result envelope and error taxonomy"). ``runner_error``
# is the infrastructure kind: the engine or protocol failed, not the author's
# code and not a verb.
KIND_AUTHOR_ERROR = "author_error"
KIND_VERB_ERROR = "verb_error"
KIND_BUDGET_EXCEEDED = "budget_exceeded"
KIND_APPROVAL_REQUIRED = "approval_required"
KIND_RESUME_INVALID = "resume_invalid"
KIND_STATE_TOO_LARGE = "state_too_large"
KIND_RUNNER_ERROR = "runner_error"

ERROR_KINDS = frozenset(
    {
        KIND_AUTHOR_ERROR,
        KIND_VERB_ERROR,
        KIND_BUDGET_EXCEEDED,
        KIND_APPROVAL_REQUIRED,
        KIND_RESUME_INVALID,
        KIND_STATE_TOO_LARGE,
        KIND_RUNNER_ERROR,
    }
)


@dataclass
class WorkflowError:
    kind: str
    message: str
    step: Optional[int] = None
    verb: Optional[str] = None
    traceback: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.step is not None:
            out["step"] = self.step
        if self.verb:
            out["verb"] = self.verb
        if self.traceback:
            out["traceback"] = self.traceback
        return out


@dataclass
class WorkflowEnvelope:
    ok: bool
    status: str
    output: Any = None
    error: Optional[WorkflowError] = None
    budget: dict = field(default_factory=dict)
    resume_token: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "output": self.output,
            "budget": dict(self.budget),
        }
        if self.error is not None:
            out["error"] = self.error.to_dict()
        if self.resume_token:
            out["resume_token"] = self.resume_token
        return out


def ok_envelope(output: Any, budget: dict) -> WorkflowEnvelope:
    return WorkflowEnvelope(ok=True, status=STATUS_OK, output=output, budget=budget)


def error_envelope(error: WorkflowError, budget: dict) -> WorkflowEnvelope:
    status = STATUS_ERROR
    return WorkflowEnvelope(ok=False, status=status, error=error, budget=budget)


def timeout_envelope(message: str, budget: dict) -> WorkflowEnvelope:
    return WorkflowEnvelope(
        ok=False,
        status=STATUS_TIMEOUT,
        error=WorkflowError(kind=KIND_BUDGET_EXCEEDED, message=message),
        budget=budget,
    )


# NOTE: STATUS_CANCELLED has no engine-side producer: execute_workflow kills the
# child and RE-RAISES CancelledError (correct asyncio practice), so the
# "cancelled" envelope is synthesized by the phase-3 tool wrapper that catches
# the cancellation at the turn boundary, not here.
