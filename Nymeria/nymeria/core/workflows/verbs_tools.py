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

from langchain_core.messages import ToolMessage

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


def _gate_reason(agent: Any, tool_name: str, user_id: str, thread_id: str, role: str) -> Optional[str]:
    """Seam: why this caller may not dispatch ``tool_name``, or None if allowed.

    Delegates to the shared by-name gate so a workflow enforces exactly what the
    deferred meta-tool and the self-modification test step enforce. It used to
    apply only the management denylist and the role gates, leaving the exclusion
    set unenforced (``nym.tools.tool_invoke`` was a legal, pointless recursion)
    and relying on ``_find_tool``'s effective-set lookup to carry
    ``disabled_tools`` as a side effect of resolution rather than as a decision.
    """
    from ..tool_execution import by_name_gate_reason

    return by_name_gate_reason(agent, tool_name, user_id, thread_id, role)


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
    base_config: Any = None,
) -> Any:
    """Invoke an already-resolved, already-gated tool as the calling user.

    The by-name half of the shared execution envelope: it builds the config, runs
    the call inside ``core/tool_execution``'s PRE/POST hook sandwich, and hands
    back the tool's own result. Per-user credentials and thread scoping resolve
    exactly as they do for an in-turn tool call. Raises the tool's own exception
    on failure and ``ToolDenied`` on a hook veto; callers normalize both.

    ``base_config`` is the caller's run config when it has one. ``tool_invoke``
    passes the turn's, which is what lets a hook fire against the effective
    tool: this function used to build a fresh three-key dict unconditionally and
    discard the turn config, so the deferred path had no hook registry to fire
    against. That discard, not a missing lookup, is why by-name calls were
    invisible to hooks.
    """
    from ..tool_execution import arun_tool_envelope, by_name_tool_config

    config = by_name_tool_config(
        user_id=user_id,
        thread_id=thread_id,
        base=base_config,
        workflow_depth=workflow_depth,
        agent=_current_agent(),
    )
    call_args = {k: v for k, v in (args or {}).items() if k != "tool_call_id"}
    # A tool that declares an InjectedToolCallId arg must be invoked with a full
    # ToolCall envelope (langchain's contract), with a synthesized id since a
    # workflow/deferred call has no LLM tool_call to inherit one from. Detection
    # is by signature, so the id is supplied preemptively, never as a retry
    # after a partial side effect.
    wants_id = _wants_injected_tool_call_id(tool)

    async def _execute(call: dict) -> Any:
        # Args are read off the call the envelope hands back, not off the
        # closure, so a PRE hook's ``modify`` decision actually reaches the tool
        # rather than being computed and dropped.
        effective_args = call.get("args") or {}
        if wants_id:
            # ToolCall envelope form: langchain populates the InjectedToolCallId
            # arg from the ``id`` field and returns a ToolMessage.
            invocation = {
                "args": effective_args,
                "name": tool_name,
                "type": "tool_call",
                "id": tool_call_id,
            }
            # Returned WHOLE rather than unwrapped to ``.content`` here. That
            # ToolMessage carries a real success/error status, and unwrapping
            # before the envelope reads it would leave every POST hook on this
            # path seeing status=None: a hook with fire_conditions on
            # tool_status would fire for bound calls and silently never for
            # by-name ones, which is the same half-coverage this module exists
            # to remove, one level down. The unwrap happens after the envelope.
            return await tool.ainvoke(invocation, config)
        return await tool.ainvoke(effective_args, config)

    result = await arun_tool_envelope(
        call={"name": tool_name, "args": call_args, "id": tool_call_id},
        config=config,
        execute=_execute,
    )
    # Hand the caller the tool's own payload, never the message wrapper.
    # isinstance rather than getattr(result, "content", result): a tool that
    # happens to return an object with a ``.content`` attribute is not a
    # ToolMessage and must not be silently unwrapped.
    return result.content if isinstance(result, ToolMessage) else result


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

    The workflow-side dispatcher: the shared by-name gate, effective-tool-set
    lookup, then the shared ``invoke_resolved_tool`` envelope. Resolution is the
    thread's EFFECTIVE set (``select_tools_for_graph``), so ``disabled_tools``
    is enforced twice over: once as a decision in the gate and once by the
    lookup. Belt and braces on purpose, since the two answer to different
    owners and the lookup's version is a side effect of resolution rather than
    a stated rule.
    """
    from ..tool_execution import ToolDenied

    role = _caller_role(user_id)
    reason = _gate_reason(agent, tool_name, user_id, thread_id, role)
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
    except ToolDenied as denied:
        raise VerbError(
            f"tool {tool_name!r} was blocked by a lifecycle hook: {denied.reason}"
        ) from denied
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
