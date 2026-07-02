"""Execution glue for the ``workflow`` custom-tool type.

``CustomToolLoader._create_workflow_tool`` binds a saved workflow definition
as a LangChain tool; this module is the coroutine behind it. Responsibilities:

- Re-read the definition from the loader at CALL time (never the bound
  closure), so an edit or a revoked approval takes effect immediately (the
  run_command execution-time re-gate precedent).
- Apply the per-revision approval gate (``authoring.workflow_execution_gate``),
  fail closed.
- Resolve ``user_id``/``thread_id`` from the standard ``RunnableConfig``
  configurable (the bash.py injection pattern; custom tools historically ran
  context-free, workflows cannot).
- Enforce nesting depth: a workflow dispatched from inside another workflow
  (``nym.tools.<name>``) carries ``workflow_depth`` in its configurable;
  beyond ``budget.max_depth`` the call is refused BEFORE spawning, which is
  what makes workflow-calls-workflow recursion bounded.
- Map the result envelope to agent-facing text: plain output on success,
  ``[Error]:`` taxonomy plus the author traceback on failure (the plan's
  self-repair affordance), always with a compact budget stats line.

Turn abort is NOT handled here: ``execute_workflow`` kills the child process
group and re-raises ``CancelledError``, which must keep propagating.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from .authoring import budget_from_config, workflow_execution_gate
from .envelope import STATUS_NEEDS_APPROVAL, STATUS_OK, WorkflowEnvelope

logger = logging.getLogger(__name__)


def format_envelope_for_agent(envelope: WorkflowEnvelope) -> str:
    """Envelope to tool-result text (output, or taxonomy + traceback)."""
    budget = envelope.budget or {}
    stats = (
        f"[workflow: {budget.get('calls_used', 0)} calls, "
        f"{budget.get('ai_calls_used', 0)} ai, "
        f"{float(budget.get('wall_seconds', 0.0) or 0.0):.1f}s]"
    )
    if envelope.status == STATUS_OK:
        output = envelope.output
        if isinstance(output, (dict, list)):
            text = json.dumps(output, indent=2, default=str)
        else:
            text = str(output) if output is not None else ""
        return f"{text}\n\n{stats}" if text else stats
    if envelope.status == STATUS_NEEDS_APPROVAL:
        # Reserved: nym.approve suspend/resume ships in phase 4.
        return f"[Workflow suspended] approval required before it can continue. {stats}"
    error = envelope.error
    kind = error.kind if error else "runner_error"
    message = error.message if error else "workflow failed"
    text = f"[Error]: workflow {envelope.status} ({kind}) - {message}"
    if error is not None and error.step is not None:
        verb = f", verb {error.verb}" if error.verb else ""
        text += f" (step {error.step}{verb})"
    if error is not None and error.traceback:
        # Author bugs return the traceback so the authoring agent can self-repair.
        text += f"\n{error.traceback}"
    return f"{text}\n{stats}"


async def run_workflow_tool(
    loader: Any,
    tool_id: str,
    params: dict,
    config: Optional[Mapping[str, Any]],
) -> str:
    """One workflow-tool invocation: re-gate, budget, execute, format."""
    definition = loader.get_definition(tool_id)
    if definition is None or definition.workflow_config is None:
        return f"[Error]: workflow definition {tool_id!r} was not found (deleted or disabled?)"
    workflow_config = definition.workflow_config

    gate_error = workflow_execution_gate(workflow_config, definition.parameters)
    if gate_error:
        return f"[Error]: approval_required - {gate_error}"

    configurable = (config or {}).get("configurable") or {}
    user_id = str(configurable.get("user_id") or "default")
    thread_id = str(configurable.get("thread_id") or "")
    try:
        depth = int(configurable.get("workflow_depth") or 0)
    except (TypeError, ValueError):
        depth = 0

    budget = budget_from_config(workflow_config)
    if depth > budget.max_depth:
        return (
            "[Error]: workflow error (budget_exceeded) - workflow nesting depth "
            f"{depth} exceeds max_depth {budget.max_depth}"
        )

    from .executor import execute_workflow

    run = await execute_workflow(
        source=workflow_config.source_code,
        entrypoint=workflow_config.entrypoint,
        params=dict(params or {}),
        user_id=user_id,
        thread_id=thread_id,
        workflow_id=tool_id,
        budget=budget,
        depth=depth,
    )
    return format_envelope_for_agent(run.envelope)


__all__ = ["format_envelope_for_agent", "run_workflow_tool"]
