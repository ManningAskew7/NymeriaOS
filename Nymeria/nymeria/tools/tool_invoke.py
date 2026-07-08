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

It enforces the SAME gates as binding (management denylist, admin/developer
role gates, and the thread's authoritative ``disabled_tools``), resolving
credentials as the CALLING user, so the deferred path is never a gate bypass.
It shares the workflow dispatcher's invocation envelope
(``core/workflows/verbs_tools.py::invoke_resolved_tool``): one call path, one
policy.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Dict, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool

from .schema_render import render_tool_args_schema
from .utils import caller_role, current_agent, get_thread_id, get_user_id

logger = logging.getLogger(__name__)


# Tools that cannot travel the deferred path, and why. The deferred path runs a
# tool ONCE without binding it, so it excludes tools that are not real
# dispatchable targets AND tools whose whole job is to mutate the thread's tool
# set (binding, not a one-off invoke, is the surface for those: they return a
# graph-control Command that a nested invoke cannot apply).
_DEFERRED_EXCLUDED_REASONS: dict[str, str] = {
    "tool_invoke": "tool_invoke cannot call itself; that would recurse with no added capability.",
    "Skill": "Skill has its own resident tool; load skills through it, not the deferred path.",
    "run_tools_in_order": "run_tools_in_order is an in-turn ordering marker, meaningless when invoked alone.",
    "install_skill": "install_skill binds a skill's tools and reloads the tool set; bind it first-class instead of deferring it.",
    "install_mcp_server": "install_mcp_server binds an MCP server's tools and reloads the tool set; bind it first-class instead of deferring it.",
}
DEFERRED_EXCLUDED_TOOL_NAMES = frozenset(_DEFERRED_EXCLUDED_REASONS)


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
    """Locate ``name`` in the caller's dispatch SUPERSET (what could be bound).

    The superset (``compute_tool_superset``) is every tool the executor could
    dispatch: SEED + CATALOG + registry (MCP / custom / workflow) + callable
    threads. Resolving here keeps "what tool_invoke can run" equal to "what the
    thread could bind and then call", one source of truth. It applies no gates,
    so the caller must (see ``_gate_reason`` + disabled_tools below).
    """
    try:
        tools, _names = agent._compute_tool_superset(user_id, thread_id)
    except Exception:  # noqa: BLE001 - resolution failure falls through to not-found
        logger.debug("compute_tool_superset failed during tool_invoke resolve", exc_info=True)
        return None
    for tool_obj in tools:
        if getattr(tool_obj, "name", None) == name:
            return tool_obj
    return None


def _thread_disabled_tools(agent: Any, thread_id: str) -> set[str]:
    """Return the thread's authoritative ``disabled_tools`` set (best-effort)."""
    try:
        tc = agent.thread_config_manager.get_config(thread_id)
    except Exception:  # noqa: BLE001
        return set()
    if tc is None:
        return set()
    return set(getattr(tc, "disabled_tools", None) or [])


def deferred_gate_reason(
    agent: Any, name: str, user_id: str, thread_id: str, role: str
) -> Optional[str]:
    """The single deferred-path gate: return a refusal reason, or None to allow.

    Shared by ``tool_invoke`` and the permissive unbound-call dispatch path
    (``allow_unbound_tool_calls``) so both enforce ONE policy: the deferred
    exclusion set, the shared management denylist + admin/developer role gates,
    and the thread's authoritative ``disabled_tools``. Credentials still resolve
    as the calling user at invoke time; this only decides admissibility.
    """
    from ..core.workflows.verbs_tools import _gate_reason

    excluded = _DEFERRED_EXCLUDED_REASONS.get(name)
    if excluded is not None:
        return f"{name!r} cannot be called through the deferred path. {excluded}"
    reason = _gate_reason(name, role)
    if reason:
        return reason
    if name in _thread_disabled_tools(agent, thread_id):
        return (
            f"{name!r} is disabled on this thread (disabled_tools is "
            "authoritative). Ask the user to re-enable it, or use a different tool."
        )
    return None


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
        )
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
    are already in ``_DEFERRED_EXCLUDED_REASONS``; this is a defensive backstop
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

    Use this for a tool you need once or a few times, or while you are still
    choosing among candidates: it does not change your tool list, so the prompt
    cache is preserved. For a tool you will use repeatedly across a long
    conversation, or one whose arguments must be exactly right, prefer
    tool_manage(action="enable") to bind it first-class (its arguments are then
    grammar-constrained by the real schema).

    You can call any tool discoverable via tool_search (catalog, MCP, custom, or
    workflow tools), including ones not currently in your tool list. Get a
    tool's argument schema from tool_search(include_schemas=true), from a
    schema handed to you (a hook, the user, a prior result), or, if you guess
    wrong, from the corrected schema this tool echoes back on a validation error.

    Arguments are validated against the target's real schema server-side but,
    unlike a bound tool, are NOT grammar-constrained as you type them, so format
    them carefully from the schema. The same gates as binding apply (role,
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
