"""One execution envelope for every tool call, whatever spelling reached it.

A tool can be executed from six places: the graph tool node (a bound call), the
deferred meta-tool ``tool_invoke``, the workflow verb ``nym.tools.*``, the
self-modification test step ``self_invoke_tool``, and two workflow verbs that
invoke a specific tool directly. Before this module only the first fired
lifecycle hooks, so a ``require_approval`` or ``block_if_matches`` hook on
``bash_execute`` was bypassed by ``tool_invoke(name="bash_execute")``: one call,
nothing disabled, no approval. ``SECURITY.md`` 2.8 documented that gap.

The fix is not "fire hooks in the other five too", which is five copies that
drift. It is one envelope every caller passes through:

    resolve registry -> PRE hooks -> apply outcome (deny | modify args)
                     -> execute(call)          <- the CALLER supplies this
                     -> POST hooks -> apply outcome -> schedule observe

``execute`` is a callable because the mechanics genuinely differ and should not
be unified: the graph node runs a langgraph ``ToolCallRequest`` through the
parent's timeout, truncation and interrupt-re-raise machinery, while a by-name
caller has a plain ``ainvoke``. Dragging langgraph internals into core to share
them would violate the vendor fork policy in the opposite direction. So core
owns the POLICY (which hooks fire, in what order, how an outcome is applied) and
each caller keeps its own MECHANICS.

The envelope is deliberately polymorphic over the result rather than defining an
adapter protocol: the graph node deals in ``ToolMessage`` and by-name callers in
plain strings, which are the only two shapes that exist, and both are handled
directly by :func:`tool_result_text` / :func:`apply_post_tool_outcome`. A third
shape passes through untouched rather than erroring, because a POST hook that
cannot read a result must never be able to break the call.

Scope, because both the docstring above and ``SECURITY.md`` read as exhaustive:
"every path" means every AGENT-initiated one. One platform-initiated call site
stays outside deliberately (the error-report mail in ``api/routers/system.py``):
it is the server acting on its own behalf, not a model choosing a tool, so a
per-turn hook has no turn to attach to.

This module is also where a future per-tool permission model or security profile
attaches (:func:`tool_allowlist`, :func:`by_name_gate_reason`). Read
:func:`tool_allowlist`'s docstring before assuming that arm is sufficient on its
own: it governs by-name dispatch, and bound calls are decided at graph build.
Nothing here reads config today, on purpose.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, cast

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


# Tools whose entire job is to dispatch ANOTHER tool by name. The graph node
# suppresses its own PRE/POST fire for these, because the envelope inside them
# fires on the effective target under the target's real name.
#
# Both halves are load-bearing and neither works alone. Without the inner fire,
# a hook matching ``bash_execute`` misses ``tool_invoke(name="bash_execute")``,
# which is the live bypass. Without the outer suppression, a matcher-less hook
# fires twice for one user-visible action, and worse, a ``require_approval`` hook
# would prompt the user twice for the same call.
# ``self_invoke_tool`` is here for the same reason and it is easy to miss: it is
# ALSO graph-bound (`SELF_AGENT_TOOLS`), so without suppression the node fires on
# the name `self_invoke_tool` and its own envelope fires on the target, which is
# two prompts for one `require_approval`.
TRANSPORT_TOOL_NAMES = frozenset({"tool_invoke", "self_invoke_tool"})


# Tools that cannot be reached BY NAME, and why. Two kinds, and the distinction
# matters: a name that is not a real dispatchable target at all, and a name whose
# whole job is to mutate the thread's tool set (those return a graph-control
# Command that a nested invoke cannot apply, so a by-name call would appear to
# succeed and change nothing).
#
# Owned here rather than in tools/tool_invoke.py because the gate that consumes
# it is now shared: a workflow calling nym.tools.tool_invoke used to slip past
# this map entirely, which is a recursion with no added capability.
BY_NAME_EXCLUDED_REASONS: dict[str, str] = {
    "tool_invoke": "tool_invoke cannot call itself; that would recurse with no added capability.",
    "Skill": "Skill has its own resident tool; load skills through it, not by name.",
    "run_tools_in_order": "run_tools_in_order is an in-turn ordering marker, meaningless when invoked alone.",
    "install_skill": "install_skill binds a skill's tools and reloads the tool set; bind it first-class instead.",
    "install_mcp_server": "install_mcp_server binds an MCP server's tools and reloads the tool set; bind it first-class instead.",
    "self_invoke_tool": "self_invoke_tool is itself a by-name dispatcher; reaching it by name adds a layer and no capability.",
}
BY_NAME_EXCLUDED_TOOL_NAMES = frozenset(BY_NAME_EXCLUDED_REASONS)


# Configurable keys a by-name call inherits from the turn that spawned it.
#
# An explicit list is REQUIRED, not a style choice. langchain's ``ensure_config``
# merges a passed config over the ambient one with a plain ``dict.update``, so
# supplying ``configurable`` at all REPLACES the parent's wholesale rather than
# merging into it. That single line of langchain behaviour is why every by-name
# call was invisible to hooks: the old helper passed a three-key ``configurable``
# and thereby deleted the turn's ``hook_registry`` and turn-source stamps.
#
# ``callbacks`` is deliberately absent and its absence changes nothing: the same
# function inherits the ambient callback manager from a contextvar when the key
# is not supplied, so a nested call already streams into the parent's run tree
# whatever we do here. Copying it would be redundant, not protective.
_INHERITED_CONFIGURABLE_KEYS = (
    "hook_registry",
    "hook_is_autonomous",
    "hook_holder_kind",
    "hook_trigger_label",
    "hook_context_tokens",
    "hook_context_limit",
    "hook_compact_trigger_tokens",
    "tool_timing_in_results",
)


class ToolDenied(Exception):
    """A tool call refused before or during the envelope.

    Raised for a PRE-hook veto so every caller renders the refusal in its own
    shape (a ``ToolMessage`` at the node, an error string for ``tool_invoke``, a
    ``VerbError`` for a workflow verb) without the envelope needing to know
    which. Carries the tool name so a caller can name it in the copy.
    """

    def __init__(self, reason: str, *, tool_name: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_name = tool_name


# --------------------------------------------------------------------------- #
# The gate: admissibility, applied identically to every by-name spelling.
# --------------------------------------------------------------------------- #


def tool_allowlist(agent: Any, user_id: str, thread_id: str) -> Optional[frozenset[str]]:
    """The positive allowlist for this caller, or ``None`` when none is set.

    A seam, and it returns ``None`` today on purpose. ``disabled_tools`` is a
    denylist, so "this thread may use only X and Y" is currently inexpressible:
    narrowing a thread's tool list restricts what the model is OFFERED and
    nothing about what it can reach by name.

    READ THIS BEFORE TREATING IT AS A PERMISSION MODEL. It is consulted only
    from :func:`by_name_gate_reason`, so it constrains the BY-NAME half and
    nothing else. A bound call never runs this gate: what the model may bind is
    decided at graph build (`agent_graph.select_tools_for_graph`, role gates
    plus ``disabled_tools``). So an allowlist here cannot express "this thread
    may not use bash" for a thread that has bash bound, and populating it alone
    would produce a control that looks total and is not.

    That is not an argument against the arm, because by-name dispatch is exactly
    the reach binding does NOT already govern (the superset is wider than the
    bound set, which is the deferred-loading contract). It is an argument
    against reading it as finished. `beta-readiness/04-security-profile-standard.md`
    names two chokepoints; this module merged them into one envelope, and the
    profile's positive check wants to sit in the envelope rather than here, at
    the cost of a per-call role resolution that only the unbound path pays
    today. That is a policy decision with a hot-path cost, so it is deliberately
    not taken here.

    ``None`` (no allowlist) and ``frozenset()`` (an allowlist that permits
    nothing) are deliberately different answers.
    """
    return None


def by_name_gate_reason(
    agent: Any, name: str, user_id: str, thread_id: str, role: str
) -> Optional[str]:
    """Why this caller may not dispatch ``name`` by name, or None to allow.

    The single admissibility gate for every by-name spelling: the deferred
    exclusion set, the protected-management denylist, the admin/developer role
    gates, the thread's authoritative ``disabled_tools``, and the allowlist arm.
    Credentials still resolve as the calling user at invoke time; this decides
    only whether the call is admissible.

    Order is chosen for the copy the model sees: the most specific and most
    actionable reason wins, so "this tool cannot be deferred at all" beats "you
    are not an admin", which beats "it is disabled on this thread".
    """
    from ..tools.tool_search import PROTECTED_MANAGEMENT_TOOL_NAMES

    excluded = BY_NAME_EXCLUDED_REASONS.get(name)
    if excluded is not None:
        return f"{name!r} cannot be dispatched by name. {excluded}"

    if name in PROTECTED_MANAGEMENT_TOOL_NAMES:
        return (
            f"tool {name!r} is a protected management tool and cannot be "
            "dispatched by name"
        )

    from ..tools import filter_admin_only_tools, filter_developer_only_tools

    _, blocked = filter_admin_only_tools({name}, role)
    if blocked:
        return f"tool {name!r} is admin-only and the caller is not an admin"
    _, blocked = filter_developer_only_tools({name}, role)
    if blocked:
        return f"tool {name!r} is developer-only and the caller is not an admin"

    if name in thread_disabled_tools(agent, thread_id):
        return (
            f"{name!r} is disabled on this thread (disabled_tools is "
            "authoritative). Ask the user to re-enable it, or use a different tool."
        )

    allowed = tool_allowlist(agent, user_id, thread_id)
    if allowed is not None and name not in allowed:
        return (
            f"{name!r} is not in this thread's allowed tool set. "
            "Ask the user to permit it."
        )
    return None


def resolve_by_name(agent: Any, user_id: str, thread_id: str, name: str) -> Optional[Any]:
    """Locate ``name`` in the caller's dispatch SUPERSET, or None.

    The resolver for the superset-scoped by-name callers: ``tool_invoke`` and
    ``self_invoke_tool``. NOT the only resolver in the system: the workflow verb
    keeps ``verbs_tools._find_tool``, which resolves from the thread's EFFECTIVE
    set, deliberately narrower. Two scopes, both intentional, and the gate is
    the control in both cases. The superset
    (``agent_graph.compute_tool_superset``) is every tool the executor could
    dispatch for this user in this thread: seed + catalog + registry (MCP,
    custom, workflow) + team-scoped callable threads + kit templates. Resolving
    here keeps "what a by-name call can run" equal to "what the thread could bind
    and then call".

    It applies NO gates, deliberately: resolution scope is not a control, and
    treating it as one is how the three previous resolvers came to disagree. The
    caller must run :func:`by_name_gate_reason` first.

    Note what the superset is NOT: ``agent.tool_registry`` holds every user's
    callable-thread tools, so resolving from it reaches across accounts.
    """
    try:
        tools, _names = agent._compute_tool_superset(user_id, thread_id)
    except Exception:  # noqa: BLE001 - resolution failure reads as not-found
        logger.debug("compute_tool_superset failed during by-name resolve", exc_info=True)
        return None
    for tool_obj in tools:
        if getattr(tool_obj, "name", None) == name:
            return tool_obj
    return None


def superset_tool_names(agent: Any, user_id: str, thread_id: str) -> set[str]:
    """Names in the caller's dispatch superset, for "did you mean" copy.

    Exists so a not-found message enumerates what THIS caller could actually
    reach. The obvious alternative, ``agent.tool_registry.list_tools()``, holds
    every user's callable-thread tools, so an error string built from it leaks
    other accounts' callable names and omits catalog tools the caller should
    have been pointed at.
    """
    try:
        _tools, names = agent._compute_tool_superset(user_id, thread_id)
    except Exception:  # noqa: BLE001 - a help string must never fail a call
        logger.debug("compute_tool_superset failed while listing names", exc_info=True)
        return set()
    return set(names or ())


def thread_disabled_tools(agent: Any, thread_id: str) -> set[str]:
    """The thread's authoritative ``disabled_tools`` set (best-effort, empty on error).

    Best-effort is the right failure mode here only because this is one term of a
    denial test: an unreadable thread config yields an empty set, so the call
    falls through to the remaining gates rather than being refused for a reason
    the user cannot act on. The allowlist arm above is the opposite shape (absent
    means "no restriction"), so a read failure there cannot silently widen either.
    """
    try:
        tc = agent.thread_config_manager.get_config(thread_id)
    except Exception:  # noqa: BLE001 - see docstring
        return set()
    if tc is None:
        return set()
    return set(getattr(tc, "disabled_tools", None) or [])


# --------------------------------------------------------------------------- #
# Config: what a by-name call inherits from the turn that spawned it.
# --------------------------------------------------------------------------- #


def by_name_tool_config(
    *,
    user_id: str,
    thread_id: str,
    base: Any = None,
    workflow_depth: int = 0,
    agent: Any = None,
) -> dict:
    """Build the ``RunnableConfig`` for a tool dispatched by name.

    ``base`` is the caller's own config when it has one (``tool_invoke`` receives
    the turn's), and the keys in ``_INHERITED_CONFIGURABLE_KEYS`` carry across.
    That inheritance is the actual bug fix behind this module: the previous
    shared helper built a fresh three-key dict and threw the turn config away, so
    the deferred path had no hook registry to fire against and no turn-source
    fields to scope by. The registry was not missing because nobody resolved it,
    it was missing because a shared helper discarded it.

    When there is no base (a workflow verb, a trigger-time call) and ``agent``
    can resolve one, the registry is looked up the same way
    ``agent_safety.graph_run_config`` does, so a workflow-dispatched tool fires
    the same thread's hooks a bound call would.
    """
    configurable: dict[str, Any] = {
        "user_id": user_id,
        "thread_id": thread_id,
        # Read by a workflow custom tool dispatched from inside a run, which
        # refuses past budget.max_depth (core/workflows/tool_runtime.py).
        "workflow_depth": workflow_depth,
    }

    base_configurable: dict[str, Any] = {}
    if isinstance(base, dict):
        base_configurable = base.get("configurable") or {}
    for key in _INHERITED_CONFIGURABLE_KEYS:
        if key in base_configurable:
            configurable[key] = base_configurable[key]

    if configurable.get("hook_registry") is None and agent is not None:
        resolve = getattr(agent, "_hook_registry_for_turn", None)
        if callable(resolve):
            try:
                registry = resolve(thread_id, user_id)
            except Exception:  # noqa: BLE001 - a hook lookup must not fail a call
                logger.debug("hook registry lookup failed for by-name call", exc_info=True)
                registry = None
            if registry is not None:
                configurable["hook_registry"] = registry

    return {"configurable": configurable}


# --------------------------------------------------------------------------- #
# Hook plumbing, moved out of the vendored tool node so it is testable in core.
# --------------------------------------------------------------------------- #


def hook_registry_from_config(config: Any) -> Any:
    """The per-turn hook registry stamped into a run config.

    Falls back to the module ``default_registry`` when nothing is stamped
    (a turn with no enabled hooks, and every test that builds a bare config), so
    the no-hooks path stays byte-identical.
    """
    from . import hooks

    configurable = config.get("configurable") if isinstance(config, dict) else None
    return (configurable or {}).get("hook_registry") or hooks.default_registry


def _state_messages(state: Any) -> list:
    """Best-effort message list from a graph state (list/dict/model shapes)."""
    if isinstance(state, list):
        return state
    if isinstance(state, dict):
        messages = state.get("messages")
        return messages if isinstance(messages, list) else []
    messages = getattr(state, "messages", None)
    return messages if isinstance(messages, list) else []


def _fresh_context_tokens(state: Any, configurable: dict) -> Optional[int]:
    """Current context occupancy for a tool-event hook, or None.

    Freshest signal first: the most recent AIMessage's provider-reported input
    tokens from the running state (the same source ``should_subturn_compact``
    reads, so a long tool loop sees occupancy grow mid-turn). Falls back to the
    turn-entry stamp. Never raises.
    """
    try:
        from .token_usage import extract_last_from_messages

        input_tokens, _ = extract_last_from_messages(_state_messages(state))
        if input_tokens:
            return int(input_tokens)
    except Exception:  # noqa: BLE001 - a stats read must never break a tool call
        logger.debug("hook context-token extraction failed", exc_info=True)
    return configurable.get("hook_context_tokens")


def build_tool_hook_ctx(
    event: Any,
    call: Any,
    config: Any,
    *,
    result_text: Optional[str] = None,
    tool_status: Optional[str] = None,
    state: Any = None,
) -> Any:
    """Build a PRE/POST tool ``HookContext`` from the call plus the run config.

    thread_id/user_id come from ``configurable``; the turn-source fields are
    stamped there by ``agent_safety.graph_run_config`` for graph turns and by
    :func:`by_name_tool_config` for by-name calls, and default when absent, so a
    tool hook can scope by autonomous-vs-interactive / holder / trigger on either
    path.
    """
    from . import hooks

    configurable: dict[str, Any] = {}
    if isinstance(config, dict):
        configurable = config.get("configurable") or {}
    args = call.get("args") if isinstance(call, dict) else None
    return hooks.HookContext(
        event=event,
        thread_id=str(configurable.get("thread_id") or ""),
        user_id=str(configurable.get("user_id") or ""),
        is_autonomous=bool(configurable.get("hook_is_autonomous", False)),
        holder_kind=configurable.get("hook_holder_kind"),
        trigger_label=configurable.get("hook_trigger_label"),
        tool_name=call.get("name") if isinstance(call, dict) else None,
        tool_call_id=call.get("id") if isinstance(call, dict) else None,
        tool_args=args if isinstance(args, dict) else None,
        tool_result_text=result_text,
        tool_status=tool_status,
        context_tokens=_fresh_context_tokens(state, configurable),
        context_limit=configurable.get("hook_context_limit"),
        compact_trigger_tokens=configurable.get("hook_compact_trigger_tokens"),
    )


def apply_pre_tool_outcome(outcome: Any, call: dict) -> dict:
    """Apply a reduced PRE outcome to a call. Raises :class:`ToolDenied` on veto."""
    decision = getattr(outcome, "decision", "allow") if outcome else "allow"
    if decision == "deny":
        reason = getattr(outcome, "reason", None) or "blocked by a lifecycle hook"
        raise ToolDenied(reason, tool_name=str(call.get("name") or ""))
    if decision == "modify" and getattr(outcome, "updated_args", None):
        return {**call, "args": {**(call.get("args") or {}), **outcome.updated_args}}
    return call


def tool_result_text(result: Any) -> Optional[str]:
    """Plain-text content of a result, or None when it has no readable text.

    Handles both shapes that exist: a ``ToolMessage`` from the graph node and a
    bare string from a by-name call. Anything else (a Command, a structured
    payload) reads as None rather than being stringified, so a POST hook is told
    "no text" instead of being handed a repr it would match against by accident.
    """
    if isinstance(result, ToolMessage):
        return result.content if isinstance(result.content, str) else None
    if isinstance(result, str):
        return result
    return None


def tool_result_status(result: Any) -> Optional[str]:
    """"success"/"error" status of a result, or None when it carries no status.

    Note the asymmetry with :func:`tool_result_text`, which recovers text from
    both shapes. Only a ``ToolMessage`` carries a status at all: a by-name tool
    that returns a bare string signals failure in the text itself, by convention
    an ``"[Error] ..."`` prefix. So a POST hook conditioning on ``tool_status``
    sees it for bound calls and for the ``InjectedToolCallId`` subset of by-name
    calls, and sees None for the rest. That is a real gap in the contract rather
    than an oversight here, and it cannot be closed without inventing a status
    for results that never had one.
    """
    if isinstance(result, ToolMessage):
        return getattr(result, "status", None)
    return None


def apply_post_tool_outcome(result: Any, outcome: Any) -> Any:
    """Apply a reduced POST outcome (text rewrite and/or appended note).

    Command results and other non-text payloads pass through unchanged. For
    multimodal (list) content a rewrite replaces the text blocks and preserves
    the image/file blocks; a note is appended as a new text block. A fresh
    ``ToolMessage`` is returned via ``model_copy`` rather than mutated in place
    (matching ``_truncate_tool_message``), because the original is still
    referenced by the state the caller passed in.
    """
    if outcome is None:
        return result
    updated = getattr(outcome, "updated_result_text", None)
    extra = getattr(outcome, "additional_context", None)
    if updated is None and not extra:
        return result

    def _is_text_block(block: Any) -> bool:
        return isinstance(block, str) or (
            isinstance(block, dict) and block.get("type") == "text"
        )

    def rewrite(msg: Any) -> Any:
        # A by-name call's result is a bare string: rewrite it directly so a POST
        # hook is not silently inert on the deferred path, which was the whole
        # complaint.
        if isinstance(msg, str):
            content = updated if updated is not None else msg
            return f"{content}\n\n{extra}" if extra else content
        if not isinstance(msg, ToolMessage):
            return msg
        content = msg.content
        if updated is not None:
            if isinstance(content, list):
                non_text = [b for b in content if not _is_text_block(b)]
                content = non_text + [{"type": "text", "text": updated}]
            else:
                content = updated
        if extra:
            if isinstance(content, list):
                content = content + [{"type": "text", "text": extra}]
            else:
                content = f"{content}\n\n{extra}"
        return msg.model_copy(update={"content": content})

    if isinstance(result, list):
        return [rewrite(m) for m in result]
    return rewrite(result)


def dispatch_provider_event(name: str, payload: dict, config: Any) -> None:
    """Emit one custom event to the caller's stream (sync).

    Serves the tool envelope: ``hook_activity`` records and anything else the
    envelope streams. ``vendor/react_agent/nodes.py`` keeps its own near-twin
    for the LLM-layer events (``provider_retry`` and friends); read that one's
    docstring before collapsing them, the split is deliberate on both layering
    and test-seam grounds.

    A module-level function (rather than an inline call) so tests can intercept
    the wire without a running callback manager.

    Swallows everything: a stream line is never allowed to fail a tool call.
    """
    from langchain_core.callbacks.manager import dispatch_custom_event

    try:
        dispatch_custom_event(name, payload, config=config)
    except RuntimeError:
        logger.debug("no callback manager for %s event", name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("failed to dispatch %s event: %s", name, exc)


async def adispatch_provider_event(name: str, payload: dict, config: Any) -> None:
    """Async twin of :func:`dispatch_provider_event`."""
    from langchain_core.callbacks.manager import adispatch_custom_event

    try:
        await adispatch_custom_event(name, payload, config=config)
    except RuntimeError:
        logger.debug("no callback manager for %s event", name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("failed to dispatch %s event: %s", name, exc)



def emit_hook_activity(records: list, config: Any) -> None:
    """Stream collected mutate-plane hook-activity records to the chat stream.

    Each record becomes an ephemeral ``hook_activity`` custom event. Best-effort
    by contract: a call with no callback manager (every by-name call, see
    ``_INHERITED_CONFIGURABLE_KEYS``) or a stream that already closed simply
    drops the line rather than failing the tool.
    """
    for rec in records:
        dispatch_provider_event("hook_activity", dict(rec), config)


async def aemit_hook_activity(records: list, config: Any) -> None:
    """Async twin of :func:`emit_hook_activity` for the concurrent tool path."""
    for rec in records:
        await adispatch_provider_event("hook_activity", dict(rec), config)


# --------------------------------------------------------------------------- #
# The envelope.
# --------------------------------------------------------------------------- #


def run_tool_envelope(
    *,
    call: dict,
    config: Any,
    # Typed over Any rather than dict: the envelope always hands back a plain
    # dict, but a caller's closure legitimately annotates its own narrower
    # shape (the graph node's is a langgraph ``ToolCall`` TypedDict, which a
    # dict is not assignable to contravariantly).
    execute: Callable[[Any], Any],
    state: Any = None,
    registry: Any = None,
) -> Any:
    """Run one tool call inside the PRE/POST hook sandwich (sync).

    ``execute`` receives the (possibly hook-modified) call and performs the
    actual invocation. Raises :class:`ToolDenied` when a PRE hook vetoes, which
    the caller renders in its own shape.

    When no PRE/POST tool hook is registered this delegates straight to
    ``execute`` so the hot path is byte-identical to having no envelope at all.
    """
    from . import hooks

    registry = registry if registry is not None else hook_registry_from_config(config)
    if not hooks.tool_hooks_active(registry):
        return execute(call)

    pre_activity: list = []
    pre = hooks.dispatch(
        hooks.HookEvent.PRE_TOOL_USE,
        build_tool_hook_ctx(hooks.HookEvent.PRE_TOOL_USE, call, config, state=state),
        registry=registry,
        emit=pre_activity.append,
    )
    emit_hook_activity(pre_activity, config)
    call = apply_pre_tool_outcome(pre, call)

    result = execute(call)

    post_ctx = build_tool_hook_ctx(
        hooks.HookEvent.POST_TOOL_USE,
        call,
        config,
        result_text=tool_result_text(result),
        tool_status=tool_result_status(result),
        state=state,
    )
    post_activity: list = []
    post = hooks.dispatch(
        hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry,
        emit=post_activity.append,
    )
    emit_hook_activity(post_activity, config)
    final = apply_post_tool_outcome(result, post)
    # Observe-plane side effects (notify/create_todo/webhook) see the ORIGINAL
    # result, not the mutate plane's rewrite, and are scheduled off-turn so they
    # neither delay the tool return nor count against the tool timeout budget.
    hooks.schedule_observe(hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry)
    return final


async def arun_tool_envelope(
    *,
    call: dict,
    config: Any,
    execute: Callable[[Any], Awaitable[Any]],
    state: Any = None,
    registry: Any = None,
) -> Any:
    """Async twin of :func:`run_tool_envelope`."""
    from . import hooks

    registry = registry if registry is not None else hook_registry_from_config(config)
    if not hooks.tool_hooks_active(registry):
        return await execute(call)

    pre_activity: list = []
    pre = await hooks.adispatch(
        hooks.HookEvent.PRE_TOOL_USE,
        build_tool_hook_ctx(hooks.HookEvent.PRE_TOOL_USE, call, config, state=state),
        registry=registry,
        emit=pre_activity.append,
    )
    await aemit_hook_activity(pre_activity, config)
    call = apply_pre_tool_outcome(pre, call)

    result = await execute(call)

    post_ctx = build_tool_hook_ctx(
        hooks.HookEvent.POST_TOOL_USE,
        call,
        config,
        result_text=tool_result_text(result),
        tool_status=tool_result_status(result),
        state=state,
    )
    post_activity: list = []
    post = await hooks.adispatch(
        hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry,
        emit=post_activity.append,
    )
    await aemit_hook_activity(post_activity, config)
    final = apply_post_tool_outcome(result, post)
    hooks.schedule_observe(hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry)
    return cast(Any, final)
