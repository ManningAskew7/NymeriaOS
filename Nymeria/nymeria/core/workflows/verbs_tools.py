"""``nym.tools.<name>``: dispatch a bound or catalog tool by name.

This is the shared dispatch primitive the plan pairs with the future
``tool_invoke`` meta-tool (``deferred-tool-loading.md``): one denylist, one
role-gate set, credentials resolving as the CALLING user via the standard
``RunnableConfig`` shape.

Heavy imports (the tools package, agent) are function-local, following the
repo idiom for dodging circular imports; the small module-level aliases are
deliberate monkeypatch seams for tests (the ``service_integration_base``
pattern).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .registry import VerbContext, VerbError, register_verb

logger = logging.getLogger(__name__)


def _caller_role(user_id: str) -> str:
    """Seam: resolve the caller's role (fail-closed to ``user``)."""
    from ...tools.utils import caller_role

    return caller_role(user_id)


def _current_agent() -> Optional[Any]:
    """Seam: the active agent runtime."""
    from ...tools.utils import current_agent

    return current_agent()


def _gate_reason(tool_name: str, role: str) -> Optional[str]:
    """Why ``role`` may not dispatch ``tool_name``, or None if allowed.

    Order matters for the copy: the management denylist applies to everyone
    (workflows must not rewire the tool system from inside a run); the role
    gates mirror the graph-build chokepoints.
    """
    from ...tools import filter_admin_only_tools, filter_developer_only_tools
    from ...tools.tool_search import PROTECTED_MANAGEMENT_TOOL_NAMES

    if tool_name in PROTECTED_MANAGEMENT_TOOL_NAMES:
        return (
            f"tool {tool_name!r} is a protected management tool and cannot be "
            "called from a workflow"
        )
    _, blocked = filter_admin_only_tools({tool_name}, role)
    if blocked:
        return f"tool {tool_name!r} is admin-only and the caller is not an admin"
    _, blocked = filter_developer_only_tools({tool_name}, role)
    if blocked:
        return f"tool {tool_name!r} is developer-only and the caller is not an admin"
    return None


def _find_tool(agent: Any, user_id: str, thread_id: str, tool_name: str) -> Optional[Any]:
    """Seam: locate ``tool_name`` in the caller's EFFECTIVE tool set.

    Uses ``select_tools_for_graph`` (via the agent facade) rather than the
    dispatch superset, so a workflow can call exactly what a turn in this
    thread could execute: the thread's authoritative ``disabled_tools`` and the
    role gates are already applied there. This keeps the "disabled_tools is
    authoritative" invariant true for workflow dispatch, not just model turns.
    """
    tools, _tc = agent._select_tools_for_graph(user_id, thread_id)
    for tool in tools:
        if getattr(tool, "name", None) == tool_name:
            return tool
    return None


def _wants_injected_tool_call_id(tool: Any) -> bool:
    """True if ``tool``'s entrypoint declares an ``InjectedToolCallId`` arg.

    Detected by inspecting the callable's annotations, so we inject the id
    preemptively for exactly the tools that need it, instead of retrying on a
    substring match of arbitrary error text (which could re-run a tool that
    already produced a side effect before failing).
    """
    try:
        import typing

        from langchain_core.tools.base import InjectedToolCallId

        fn = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
        if fn is None:
            return False
        for hint in typing.get_type_hints(fn, include_extras=True).values():
            for meta in getattr(hint, "__metadata__", ()):  # Annotated extras
                if meta is InjectedToolCallId or isinstance(meta, InjectedToolCallId):
                    return True
    except Exception:  # noqa: BLE001 - detection is best-effort, defaults to no
        return False
    return False


async def invoke_resolved_tool(
    *,
    tool: Any,
    tool_name: str,
    user_id: str,
    thread_id: str,
    args: dict,
    tool_call_id: str,
    workflow_depth: int = 0,
) -> Any:
    """Invoke an already-resolved, already-gated tool as the calling user.

    The shared invocation envelope: ``ainvoke`` with the standard configurable
    so per-user credentials and thread scoping resolve exactly as they do for an
    in-turn tool call. Tools that declare an injected ``tool_call_id`` are
    invoked with a full ToolCall envelope carrying a synthesized id (workflow
    and deferred calls have no LLM tool_call to inherit one from). Raises the
    tool's own exception on failure (callers normalize it); no gate or
    resolution logic lives here so both the workflow verb and ``tool_invoke``
    share one call path.
    """
    # workflow_depth makes workflow-calls-workflow nesting bounded: a workflow
    # custom tool dispatched from inside a run reads it from its configurable
    # and refuses past budget.max_depth (core/workflows/tool_runtime.py).
    config = {
        "configurable": {
            "user_id": user_id,
            "thread_id": thread_id,
            "workflow_depth": workflow_depth,
        }
    }
    call_args = {k: v for k, v in (args or {}).items() if k != "tool_call_id"}
    # A tool that declares an InjectedToolCallId arg must be invoked with a full
    # ToolCall envelope (langchain's contract), with a synthesized id since a
    # workflow/deferred call has no LLM tool_call to inherit one from. Detection
    # is by signature, so the id is supplied preemptively, never as a retry
    # after a partial side effect.
    if _wants_injected_tool_call_id(tool):
        # ToolCall envelope form: langchain populates the InjectedToolCallId
        # arg from the ``id`` field and returns a ToolMessage, so hand the
        # caller its ``content`` rather than the message wrapper.
        invocation = {
            "args": call_args,
            "name": tool_name,
            "type": "tool_call",
            "id": tool_call_id,
        }
        raw = await tool.ainvoke(invocation, config)
        return getattr(raw, "content", raw)
    return await tool.ainvoke(call_args, config)


async def dispatch_tool_by_name(
    *,
    agent: Any,
    user_id: str,
    thread_id: str,
    tool_name: str,
    args: dict,
    tool_call_id: str,
    workflow_depth: int = 0,
) -> Any:
    """Resolve and invoke one tool as the calling user; raises VerbError.

    The workflow-side dispatcher: denylist, role gates, effective-tool-set
    lookup, then the shared ``invoke_resolved_tool`` envelope. Resolution is the
    thread's EFFECTIVE set (``select_tools_for_graph``), so ``disabled_tools``
    is enforced by the lookup itself.
    """
    role = _caller_role(user_id)
    reason = _gate_reason(tool_name, role)
    if reason:
        raise VerbError(reason)

    tool = _find_tool(agent, user_id, thread_id, tool_name)
    if tool is None:
        raise VerbError(
            f"tool {tool_name!r} is not enabled for this thread"
        )

    try:
        return await invoke_resolved_tool(
            tool=tool,
            tool_name=tool_name,
            user_id=user_id,
            thread_id=thread_id,
            args=args,
            tool_call_id=tool_call_id,
            workflow_depth=workflow_depth,
        )
    except VerbError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize tool failures
        raise VerbError(f"tool {tool_name!r} failed: {exc}") from exc


@register_verb(
    "tools.*",
    side_effect=True,
    description="Dispatch a bound or catalog tool by name as the calling user.",
)
async def _tools_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    tool_name = verb.split(".", 1)[1]
    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available to dispatch tools")
    return await dispatch_tool_by_name(
        agent=agent,
        user_id=ctx.user_id,
        thread_id=ctx.thread_id,
        tool_name=tool_name,
        args=args,
        tool_call_id=f"wf_{ctx.run_id}_{ctx.usage.calls_used}",
        workflow_depth=ctx.depth + 1,
    )
