"""``tool_invoke``: the cache-safe deferred execution path.

Binding a tool (``tool_manage(action="enable")`` / a Skill Kit) mutates the
thread's tool list, which sits in the cached tools/system prefix, so the next
request re-pays the whole input as a cache miss. That is the right trade for a
tool used repeatedly over a long conversation and the wrong trade for a tool
needed once.

``tool_invoke(name, arguments)`` is the other option: a single resident
meta-tool that runs any discoverable catalog / MCP / custom / workflow tool by
name WITHOUT writing thread config, so the cached prefix never changes. The
target's schema rides in conversation history (delivered by
``tool_search(include_schemas=True)``, a hook, the user, or echoed on a
validation miss here), which is cache-safe. This is Nymeria's provider-agnostic
emulation of Anthropic's deferred tool loading.

The model-facing description opens with a hard precondition: call this ONLY
with the target's full schema already in context, and ONLY for a target that is
not already bound first-class. Models pattern-match ``tool_invoke`` to the
generic dispatch idiom they are trained on and guess arguments by tool name
(observed in dogfooding, 2026-07-31), so the description pushes back up front;
the schema echo on a validation miss is a recovery path, not a license to
discover schemas by guessing.

It enforces the SAME gates as binding (management denylist, admin/developer
role gates, and the thread's authoritative ``disabled_tools``), resolving
credentials as the CALLING user, so the deferred path is never a gate bypass.
Both the gate and the execution envelope live in ``core/tool_execution.py`` and
are shared with every other by-name spelling, so lifecycle hooks fire here
exactly as they do for a bound call: a hook matching ``bash_execute`` sees
``tool_invoke(name="bash_execute")`` under the target's real name, once.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Dict, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool

from ..core.tool_execution import ToolDenied, by_name_gate_reason, resolve_by_name
from .schema_render import render_tool_args_schema
from .utils import caller_role, current_agent, get_thread_id, get_user_id

logger = logging.getLogger(__name__)


# The exclusion set moved to core/tool_execution.py as BY_NAME_EXCLUDED_REASONS
# when the gate became shared: a workflow calling nym.tools.tool_invoke used to
# slip past it entirely. The old ``DEFERRED_EXCLUDED_TOOL_NAMES`` alias that
# stood here is gone rather than re-exported, because it had zero consumers
# before this change too.


def _normalize_arguments(arguments: Any) -> tuple[Optional[dict], Optional[str]]:
    """Coerce the ``arguments`` field to a dict; return (args, error_text).

    Accepts a dict directly or a JSON-object string (some providers stringify
    object-typed tool-call args). Returns ``({}, None)`` for an omitted value
    and ``(None, error)`` when the value is present but not a JSON object.
    """
    if arguments is None:
        return {}, None
    if isinstance(arguments, dict):
        return arguments, None
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"arguments was a string but not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, "arguments must be a JSON object (a mapping of arg name to value)"
        return parsed, None
    return None, f"arguments must be an object, got {type(arguments).__name__}"


def _resolve_bindable_tool(agent: Any, user_id: str, thread_id: str, name: str) -> Optional[Any]:
    """Locate ``name`` in the caller's dispatch superset.

    Core owns the resolution (``tool_execution.resolve_by_name``) so every
    superset-scoped by-name spelling answers the same question. Kept as a named
    local alias only so the deferred path reads in its own vocabulary.
    """
    return resolve_by_name(agent, user_id, thread_id, name)


def deferred_gate_reason(
    agent: Any, name: str, user_id: str, thread_id: str, role: str
) -> Optional[str]:
    """The deferred path's name for the shared by-name gate.

    Kept as a named entry point because three callers reason in deferred-path
    terms: this tool, the permissive unbound-call dispatch path
    (``allow_unbound_tool_calls`` in the graph tool node), and
    ``react.reaction_guidance_block``, which asks the gate whether advertising a
    ``tool_invoke`` recipe would only be refused. The policy itself lives in
    ``core/tool_execution.by_name_gate_reason`` so every by-name spelling
    enforces one set of rules.
    """
    return by_name_gate_reason(agent, name, user_id, thread_id, role)


async def _tool_invoke_impl(
    name: str,
    arguments: Any,
    *,
    tool_call_id: str,
    config: RunnableConfig,
) -> str:
    from ..core.workflows.verbs_tools import invoke_resolved_tool

    name = (name or "").strip()
    if not name:
        return "[tool_invoke error] name is required (the exact tool name to run)."

    thread_id = get_thread_id(config)
    user_id = get_user_id(config)
    role = caller_role(user_id)

    agent = current_agent()
    if agent is None:
        return "[tool_invoke error] no agent runtime is available to dispatch the tool."

    # One deferred gate: exclusion set, management denylist + role gates, then
    # the thread's authoritative disabled_tools. Never a gate bypass.
    gate = deferred_gate_reason(agent, name, user_id, thread_id, role)
    if gate:
        return f"[tool_invoke error] {gate}"

    args, arg_err = _normalize_arguments(arguments)
    if arg_err is not None or args is None:
        return f"[tool_invoke error] {arg_err or 'invalid arguments'}"

    target = _resolve_bindable_tool(agent, user_id, thread_id, name)
    if target is None:
        return (
            f"[tool_invoke error] no tool named {name!r} is available to this "
            "thread. Find the exact name with tool_search, or the tool's "
            "backing source (e.g. an MCP server) may be installed but disabled."
        )

    # A schema mismatch is the common, correctable case: pre-validate the args
    # against the target's real call schema and, on failure, echo that schema so
    # the model self-corrects in one retry. Pre-validating here (rather than
    # sniffing the dispatch exception) means a pydantic ValidationError raised
    # inside the tool BODY is never misread as a bad-arguments error.
    schema_error = _validate_against_schema(target, args)
    if schema_error is not None:
        schema = render_tool_args_schema(target)
        schema_block = f"\nExpected arguments for {name!r}:\n{schema}" if schema else ""
        return (
            f"[tool_invoke error] {name!r} rejected the arguments: {schema_error}"
            f"{schema_block}\nFix the arguments and call tool_invoke again."
        )

    try:
        result = await invoke_resolved_tool(
            tool=target,
            tool_name=name,
            user_id=user_id,
            thread_id=thread_id,
            args=args,
            tool_call_id=tool_call_id,
            workflow_depth=0,
            # The turn's own config, so the envelope inside fires THIS thread's
            # hooks against the effective tool. The graph tool node suppresses
            # its outer fire for tool_invoke (TRANSPORT_TOOL_NAMES), so the call
            # is seen once, under the target's real name, instead of once as
            # "tool_invoke" and never as what it actually ran.
            base_config=config,
        )
    except ToolDenied as denied:
        # A PRE hook vetoed the effective tool. Structured refusal, not an
        # exception the model never sees: the turn continues and it can react.
        return f"[tool_invoke error] {name!r} was blocked: {denied.reason}"
    except Exception as exc:  # noqa: BLE001 - normalize target failures for the model
        logger.info("tool_invoke: target %r failed: %s", name, exc)
        return f"[tool_invoke error] {name!r} failed: {exc}"

    if isinstance(result, str):
        return result
    return _describe_non_str_result(name, result)


def _is_validation_error(exc: BaseException) -> bool:
    """True when ``exc`` is a pydantic argument-validation error.

    Matched by class name across pydantic v2's core/python error types so the
    schema echo does not depend on importing a specific symbol. Used ONLY on the
    pre-validation exception (``_validate_against_schema``), where a
    ValidationError unambiguously means the args don't fit the schema, never on a
    dispatch exception (which could be a body-raised ValidationError).
    """
    for cls in type(exc).__mro__:
        if cls.__name__ == "ValidationError":
            return True
    return False


def _validate_against_schema(target: Any, args: dict) -> Optional[str]:
    """Pre-validate ``args`` against ``target``'s CALL schema; error text or None.

    Returns the validation-error text when the args don't fit (caller echoes the
    real schema), or None when they fit, no schema is available, or the pre-check
    hit a non-validation error (let the real dispatch surface that). Prefers
    ``tool_call_schema`` (injected args already removed), matching what langchain
    validates a real call against, so this is faithful and side-effect-free.
    """
    model = getattr(target, "tool_call_schema", None) or getattr(target, "args_schema", None)
    if model is None or not hasattr(model, "model_validate"):
        return None
    try:
        model.model_validate(args)
    except Exception as exc:  # noqa: BLE001 - only a ValidationError means bad args
        return str(exc) if _is_validation_error(exc) else None
    return None


def _describe_non_str_result(name: str, result: Any) -> str:
    """Render a non-string tool result for the model.

    A tool that returns a LangGraph ``Command`` (a graph-control directive such
    as a tool-list reload) cannot be delivered through the deferred path: the
    directive does not survive a nested invoke. The mutating tools that do this
    are already in ``BY_NAME_EXCLUDED_REASONS``; this is a defensive backstop
    for any future one, refusing clearly and pointing at binding. Anything else
    is stringified.
    """
    if type(result).__name__ == "Command":
        return (
            f"[tool_invoke error] {name!r} returned a graph-control directive that "
            "the deferred path cannot apply. Bind it first with "
            f'tool_manage(action="enable", tools=["{name}"]) and call it directly.'
        )
    return str(result)


@tool
async def tool_invoke(
    name: str,
    arguments: Optional[Union[Dict[str, Any], str]] = None,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Run one tool by name WITHOUT binding it to this thread (cache-safe).

    DO NOT call this tool unless BOTH of these hold:
    1. You already have the target tool's FULL argument schema in context,
       from tool_search(include_schemas=true), a Skill Kit's deferred-load
       listing, or a schema handed to you (a hook, the user, a prior result).
       Never guess a tool's arguments from its name or description.
    2. The target tool is NOT in your current first-class tool list. A bound
       tool must be called directly, never through tool_invoke.

    Use this for a tool you need once or a few times, or while you are still
    choosing among candidates: it does not change your tool list, so the prompt
    cache is preserved. For a tool you will use repeatedly across a long
    conversation, or one whose arguments must be exactly right, prefer
    tool_manage(action="enable") to bind it first-class (its arguments are then
    grammar-constrained by the real schema).

    You can call any tool discoverable via tool_search (catalog, MCP, custom,
    or workflow tools). Arguments are validated against the target's real
    schema server-side but, unlike a bound tool, are NOT grammar-constrained as
    you type them, so format them exactly from the schema you hold. A
    validation miss echoes the correct schema back, but that is a recovery
    path, not a discovery mechanism. The same gates as binding apply (role,
    disabled, and management-tool limits).

    Args:
        name: The exact tool name to run (as shown by tool_search).
        arguments: The target tool's arguments as a JSON object (mapping of arg
            name to value). Omit or pass an empty object for a no-argument tool.
    """
    return await _tool_invoke_impl(
        name, arguments, tool_call_id=tool_call_id, config=config
    )


TOOL_INVOKE_TOOLS = [tool_invoke]
