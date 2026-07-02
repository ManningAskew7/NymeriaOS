"""Lifecycle-hook authoring tools for the agent.

A hook runs a canned action when a lifecycle event fires. Actions and the events
they attach to:

- ``inject_context`` -- inject a string into context. On ``prompt_submit`` it
  appends to every turn's model-facing tail; on ``post_tool_use`` it appends a
  note to a tool's result (use ``matcher`` to scope, e.g. "Edit|Write"); on
  ``done`` it re-drives once with the text (a "run checks on finish" follow-up).
- ``block_if_matches`` (``pre_tool_use``) -- deny a tool call when all
  ``conditions`` match the call's args (a guardrail, e.g. block ``bash`` when
  ``command`` contains "rm -rf").
- ``rewrite_arg`` (``pre_tool_use``) -- rewrite named args on a tool call when
  ``conditions`` match (``updates`` maps arg-name -> new value).

``matcher`` filters by tool NAME (pre/post tool events); ``conditions`` filter by
the call's ARGS. Both are ANDed. Hooks auto-bind to the current thread by
default (``scope="thread"``); use ``scope="global"`` for all the user's threads.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.hook_manager import EVENT_ACTIONS, TEXT_ACTIONS, HookDefinition, HookManager
from ..core.text_format import safe_format
from .utils import get_thread_id, get_user_id, is_admin

logger = logging.getLogger(__name__)

_hook_manager: Optional[HookManager] = None

_EVENTS = tuple(EVENT_ACTIONS.keys())

# Actions whose sole config is a single `text` field (a bare `text` arg is
# accepted as a convenience alias for `params={"text": ...}`). The rest need a
# structured `params` dict (conditions/updates/url). Derived from the
# core/hook_spec.py taxonomy via the store.
_TEXT_ACTIONS = TEXT_ACTIONS

# Representative sample values for the ``test`` dry-run render, per event.
_SAMPLE_VARS = {
    "event": "",
    "thread_id": "thread-123",
    "user_id": "you",
    "is_autonomous": "False",
    "holder_kind": "interactive",
    "trigger_label": "User Message",
    "prompt": "the user's message",
    "tool_name": "Edit",
    "tool_result": "the tool's output",
    "tool_status": "success",
    "tool_args": '{"file": "a.py"}',
    "final_text": "the assistant's final reply",
}

_INJECTION_TARGET = {
    "prompt_submit": "appended to the model-facing message tail this turn",
    "post_tool_use": "appended to the matching tool's result",
    "done": "re-driven as a follow-up prompt when the turn finishes",
}


def _get_hook_manager() -> HookManager:
    global _hook_manager
    if _hook_manager is None:
        from ..config import get_settings
        _hook_manager = HookManager(get_settings().data_dir)
    return _hook_manager


def _logic_preview(logic) -> str:
    """A short human summary of a hook's logic, per action variant."""
    action = logic.action
    if action in _TEXT_ACTIONS:
        t = logic.text
        short = t if len(t) <= 60 else t[:57] + "..."
        return f"{action} {short!r}"
    if action == "block_if_matches":
        n = len(logic.conditions)
        cond = "always" if n == 0 else f"{n} condition(s)"
        return f"deny if {cond}" + (f" (reason: {logic.reason})" if logic.reason else "")
    if action == "rewrite_arg":
        return f"rewrite args {list(logic.updates.keys())}"
    if action == "webhook":
        return f"POST webhook {logic.url}"
    if action == "run_command":
        cmd = logic.command
        short = cmd if len(cmd) <= 60 else cmd[:57] + "..."
        return f"run_command {short!r} (timeout {logic.timeout_seconds}s)"
    return action


def _summary(h: HookDefinition) -> str:
    state = "ON" if h.enabled else "OFF"
    scope = "global" if h.scope == "global" else f"thread:{h.thread_id or '?'}"
    matcher = f" matcher={h.matcher}" if h.matcher else ""
    return f"- {h.id} [{state}] {h.event}{matcher} ({scope}) :: {h.name} -> {_logic_preview(h.logic)}"


def _gated_action_error(hook_action: str, config: RunnableConfig) -> Optional[str]:
    """Admin + flag gate for run_command (returns an ``[Error]:`` string or None)."""
    from ..core.hook_manager import run_command_authoring_error
    reason = run_command_authoring_error(
        hook_action, is_admin=is_admin(get_user_id(config))
    )
    return f"[Error]: {reason}" if reason else None


def _merge_run_command_params(
    hook_action: Optional[str],
    params: Optional[dict],
    command: Optional[str],
    timeout_seconds: Optional[float],
    *,
    existing_logic=None,
) -> Optional[dict]:
    """Fold the flat run_command fields into a params dict (explicit params win).

    On update, ``existing_logic`` (the stored hook's logic) resolves the action
    when the caller did not re-state it, and lets a partial edit (e.g. just
    ``command``) merge onto the stored params so a sibling like
    ``timeout_seconds`` is preserved. This mirrors the REST
    ``build_update_kwargs`` merge-onto-existing behavior so the surfaces cannot
    drift.
    """
    if params is not None or (command is None and timeout_seconds is None):
        return params
    action = hook_action
    if action is None and existing_logic is not None:
        action = getattr(existing_logic, "action", None)
    if action != "run_command":
        return params
    # An in-place edit of a run_command hook merges onto its stored params; a
    # switch TO run_command from another action starts fresh.
    if existing_logic is not None and getattr(existing_logic, "action", None) == "run_command":
        out: dict = existing_logic.model_dump(exclude={"action"})
    else:
        out = {}
    if command is not None:
        out["command"] = command
    if timeout_seconds is not None:
        out["timeout_seconds"] = timeout_seconds
    return out or None


def _hook_create(
    *,
    name: str,
    event: str,
    action: str,
    text: Optional[str],
    params: Optional[dict],
    matcher: Optional[str],
    scope: Optional[str],
    config: RunnableConfig,
) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    scope_value = (scope or "thread").strip().lower()
    if scope_value not in ("thread", "global"):
        return "[Error]: scope must be 'thread' or 'global'."
    # Bind to the actual current thread (including 'default', which is a real
    # thread in single-thread deployments) when thread-scoped.
    thread_id = get_thread_id(config) if scope_value == "thread" else ""
    try:
        hook = manager.add_hook(
            user_id,
            name=name,
            event=event,
            action=action,
            # ``text`` is the manager-level convenience alias for the text
            # actions; explicit ``params`` wins when both are given (the manager
            # only maps ``text`` when ``params`` is None).
            params=dict(params) if params else None,
            text=text,
            matcher=matcher,
            scope=scope_value,
            thread_id=thread_id,
            created_by="agent",
        )
    except Exception as e:  # noqa: BLE001 - surface validation as a human string
        return f"[Error]: {e}"
    if hook is None:
        return "[Error]: hook limit reached for this user."
    scope_desc = "all threads" if hook.scope == "global" else f"thread {hook.thread_id}"
    return (
        f"[Success]: Created hook '{hook.name}' ({hook.id}) on {hook.event} for "
        f"{scope_desc}: {_logic_preview(hook.logic)}. Use hook_info(action='detail', "
        f"hook_id='{hook.id}') to inspect it."
    )


def _hook_update(
    *,
    hook_id: str,
    name: Optional[str],
    event: Optional[str],
    action: Optional[str],
    text: Optional[str],
    params: Optional[dict],
    matcher: Optional[str],
    enabled: Optional[bool],
    scope: Optional[str],
    config: RunnableConfig,
) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    if scope is not None:
        # Mirror the REST PATCH / `/hook edit` rule: a re-scope needs a thread
        # binding (and its access gate), which an update cannot supply without
        # silently binding to "" (an inert orphan that never fires).
        return (
            "[Error]: scope cannot be changed on update. Delete the hook and "
            "re-create it with the new scope."
        )
    updates: dict = {}
    if name is not None:
        updates["name"] = name
    if event is not None:
        updates["event"] = event
    if action is not None:
        updates["action"] = action
    if params is not None:
        updates["params"] = params
    if text is not None:
        updates["text"] = text
    if matcher is not None:
        updates["matcher"] = matcher or None
    if enabled is not None:
        updates["enabled"] = enabled
    if not updates:
        return "[Error]: update requires at least one field to change."
    try:
        ok = manager.update_hook(user_id, hook_id, **updates)
    except Exception as e:  # noqa: BLE001
        return f"[Error]: {e}"
    if not ok:
        return f"[Error]: no hook found with id '{hook_id}'."
    return f"[Success]: Updated hook {hook_id}."


def _hook_delete(*, hook_id: str, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    if manager.delete_hook(user_id, hook_id):
        return f"[Success]: Deleted hook {hook_id}."
    return f"[Error]: no hook found with id '{hook_id}'."


def _hook_list(*, current_thread_only: bool, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    hooks = manager.get_hooks(user_id)
    if current_thread_only:
        tid = get_thread_id(config)
        hooks = [h for h in hooks if h.scope == "global" or h.thread_id == tid]
    if not hooks:
        return "[Info]: No hooks configured."
    lines = [f"{len(hooks)} hook(s):"] + [_summary(h) for h in hooks]
    return "\n".join(lines)


def render_hook_detail(hook: HookDefinition) -> str:
    """Full human-readable configuration of a hook (the 'detail' view).

    Pure (no store access) so the ``/hook show`` command reuses it verbatim.
    """
    logic = hook.logic
    lines = [
        f"Hook {hook.id}: {hook.name}",
        f"  event: {hook.event}",
        f"  enabled: {hook.enabled}",
        f"  matcher: {hook.matcher or '(any tool)'}",
        f"  scope: {hook.scope}"
        + (f" (thread {hook.thread_id})" if hook.scope == "thread" else ""),
        f"  action: {logic.action}",
    ]
    if logic.action in _TEXT_ACTIONS:
        lines.append(f"  text: {getattr(logic, 'text', '')!r}")
    elif logic.action == "block_if_matches":
        lines.append(f"  conditions: {[c.model_dump() for c in logic.conditions] or '(always)'}")
        lines.append(f"  reason: {logic.reason or '(default)'}")
    elif logic.action == "rewrite_arg":
        lines.append(f"  conditions: {[c.model_dump() for c in logic.conditions] or '(always)'}")
        lines.append(f"  updates: {logic.updates}")
    elif logic.action == "webhook":
        lines.append(f"  url: {logic.url}")
        lines.append(f"  text: {logic.text!r}")
    elif logic.action == "run_command":
        lines.append(f"  command: {logic.command!r}")
        lines.append(f"  timeout_seconds: {logic.timeout_seconds}")
    lines.append(f"  created_by: {hook.created_by}")
    return "\n".join(lines)


def render_hook_test(hook: HookDefinition) -> str:
    """Dry-run description of what a hook would do against sample data (no fire).

    Pure (no store access) so the ``/hook test`` command reuses it verbatim.
    """
    logic = hook.logic
    if logic.action in _TEXT_ACTIONS:
        sample = dict(_SAMPLE_VARS)
        sample["event"] = hook.event
        rendered = safe_format(getattr(logic, "text", ""), sample)
        target = {
            "notify": "delivered as a notification",
            "create_todo": "added as a TODO",
        }.get(logic.action, _INJECTION_TARGET.get(hook.event, "injected"))
        return (
            f"[Info]: Test render of hook {hook.id} ({hook.event}, {logic.action}). With "
            f"sample data the text is {target}:\n---\n{rendered}\n---"
        )
    if logic.action == "webhook":
        return (
            f"[Info]: Hook {hook.id} (webhook) POSTs to {safe_format(logic.url, _SAMPLE_VARS)} "
            f"on {hook.event} with body text {safe_format(logic.text, _SAMPLE_VARS)!r}."
        )
    if logic.action == "block_if_matches":
        cond = "any tool call it matches" if not logic.conditions else (
            "a tool call whose args satisfy: "
            + " AND ".join(f"{c.field} {c.operator} {c.value!r}" for c in logic.conditions)
        )
        tool = hook.matcher or "any tool"
        return (
            f"[Info]: Hook {hook.id} (block_if_matches) denies {tool} on {cond}. "
            f"The model sees: {safe_format(logic.reason or 'blocked by a lifecycle hook', _SAMPLE_VARS)!r}"
        )
    if logic.action == "rewrite_arg":
        cond = "always" if not logic.conditions else (
            " AND ".join(f"{c.field} {c.operator} {c.value!r}" for c in logic.conditions)
        )
        tool = hook.matcher or "any tool"
        return (
            f"[Info]: Hook {hook.id} (rewrite_arg) on {tool} (gate: {cond}) rewrites args: "
            f"{logic.updates}."
        )
    if logic.action == "run_command":
        from ..core.hook_spec import plane_for
        plane = plane_for("run_command", hook.event)
        effect = {
            "prompt_submit": "its stdout is injected into the turn",
            "pre_tool_use": "exit 2 (or stdout JSON) denies/rewrites the tool call",
            "post_tool_use": "it runs off-turn for side effects (output is logged)",
            "done": "it runs off-turn for side effects (output is logged)",
        }.get(hook.event, "it runs")
        return (
            f"[Info]: Hook {hook.id} (run_command, {plane} plane) on {hook.event} runs "
            f"{logic.command!r} (timeout {logic.timeout_seconds}s) with the hook context "
            f"as JSON on stdin; {effect}. No command is executed by this dry run."
        )
    return f"[Info]: Hook {hook.id} action {logic.action} has no test render."


def _hook_inspect(*, hook_id: str, action: str, config: RunnableConfig) -> str:
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    hook = manager.get_hook(user_id, hook_id)
    if hook is None:
        return f"[Error]: no hook found with id '{hook_id}'."
    if action == "detail":
        return render_hook_detail(hook)
    return render_hook_test(hook)


def _hook_log(*, hook_id: Optional[str], limit: int, config: RunnableConfig) -> str:
    """Recent hook executions (newest first), optionally for one hook.

    An entry with status ``no_op`` means the hook ran and produced nothing; no
    entry at all means it never fired. ``saturated``/``timeout``/``error`` on a
    ``pre_tool_use`` hook explain a fail-closed deny.
    """
    user_id = get_user_id(config)
    manager = _get_hook_manager()
    entries = manager.get_executions(user_id, hook_id=hook_id or None, limit=max(1, limit))
    if not entries:
        scope = f" for hook {hook_id}" if hook_id else ""
        return f"[Info]: No hook executions recorded{scope}."
    lines = [f"{len(entries)} hook execution(s), newest first:"]
    for e in entries:
        ts = str(e.get("timestamp") or "")
        tool = f" tool={e['tool_name']}" if e.get("tool_name") else ""
        detail = f" :: {e['detail']}" if e.get("detail") else ""
        lines.append(
            f"- {ts} {e.get('hook_id') or '?'} [{e.get('status', '?')}] "
            f"{e.get('event', '')}{tool}{detail}"
        )
    return "\n".join(lines)


@tool
def hook_config(
    action: str,
    hook_id: Optional[str] = None,
    name: Optional[str] = None,
    event: Optional[str] = None,
    hook_action: Optional[str] = None,
    text: Optional[str] = None,
    params: Optional[dict] = None,
    matcher: Optional[str] = None,
    scope: Optional[str] = None,
    enabled: Optional[bool] = None,
    command: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create, update, or delete a lifecycle hook.

    A hook runs a canned action when an event fires. Use action="create" for a
    new hook, "update" to change one, "delete" to remove one. Hooks auto-bind to
    the current thread unless scope="global".

    Args:
        action: "create", "update", or "delete" (the CRUD verb).
        hook_id: Required for update/delete.
        name: Hook display name (create; optional on update).
        event: The lifecycle event. "pre_tool_use" takes block_if_matches/
            rewrite_arg; "prompt_submit" takes inject_context; "post_tool_use"/
            "done" take inject_context/notify/create_todo/webhook. run_command
            attaches to all four (admin + HOOKS_RUN_COMMAND_ENABLED only).
        hook_action: The hook's action. "inject_context" (default) injects text;
            "block_if_matches"/"rewrite_arg" guard a tool call; "notify" sends a
            notification; "create_todo" adds a TODO; "webhook" POSTs to a URL;
            "run_command" runs a shell command (admin-gated).
        text: For the text actions (inject_context/notify/create_todo, and the
            webhook body): the text. Supports {placeholder} interpolation
            ({tool_name}, {tool_result}, {prompt}, {final_text}, {thread_id}).
            Static text is used verbatim.
        params: For the structured actions, the action params. block_if_matches:
            {"conditions": [{"field","operator","value","case_sensitive"}], "reason": "..."}.
            rewrite_arg: {"conditions": [...], "updates": {"arg_name": "new value"}}.
            webhook: {"url": "https://...", "text": "..."}.
            Operators: equals, not_equals, contains, starts_with, matches_regex.
            Conditions match the tool call's ARGS (field is an arg name).
        matcher: Pipe-list tool-NAME filter for pre_tool_use/post_tool_use, e.g.
            "Edit|Write" (omit to match every tool). Ignored on other events.
        scope: "thread" (default; only the current thread) or "global" (all your
            threads). Create only; to re-scope, delete and re-create the hook.
        enabled: Enable/disable an existing hook on update.
        command: For run_command: the shell command. It receives the hook
            context as JSON on stdin. On pre_tool_use, exit 2 denies (stderr is
            the reason) or stdout JSON {"decision","reason","updated_args"};
            on prompt_submit, stdout is injected.
        timeout_seconds: For run_command: subprocess budget (1..300; in-band
            events clamped to 60). Defaults to 10.
    """
    action_key = (action or "").strip().lower()
    hook_action_key = (hook_action or "inject_context").strip().lower()

    if action_key == "create":
        if not name:
            return "[Error]: create requires name."
        if not event or event not in _EVENTS:
            return f"[Error]: event must be one of: {', '.join(_EVENTS)}."
        legal = EVENT_ACTIONS.get(event, set())
        if hook_action_key not in legal:
            return (
                f"[Error]: action '{hook_action_key}' is not valid for event '{event}'. "
                f"Valid: {', '.join(sorted(legal))}."
            )
        gate = _gated_action_error(hook_action_key, config)
        if gate is not None:
            return gate
        params = _merge_run_command_params(hook_action_key, params, command, timeout_seconds)
        if hook_action_key == "run_command":
            if not (params and params.get("command")):
                return "[Error]: run_command requires command."
        elif hook_action_key in _TEXT_ACTIONS:
            if not text and not params:
                return f"[Error]: {hook_action_key} requires text."
        elif not params:
            return f"[Error]: {hook_action_key} requires params."
        return _hook_create(
            name=name, event=event, action=hook_action_key, text=text, params=params,
            matcher=matcher, scope=scope, config=config,
        )

    if action_key == "update":
        if not hook_id:
            return "[Error]: update requires hook_id."
        if event is not None and event not in _EVENTS:
            return f"[Error]: event must be one of: {', '.join(_EVENTS)}."
        existing = _get_hook_manager().get_hook(get_user_id(config), hook_id)
        # Gate a switch TO a gated action AND any behavior edit of an existing
        # gated hook; enabled/name-only updates stay ungated (shared rule in
        # ``gated_update_action`` so the surfaces cannot drift).
        from ..core.hook_manager import gated_update_action
        touched = {
            key
            for key, value in {
                "name": name, "event": event, "text": text, "params": params,
                "matcher": matcher, "scope": scope, "enabled": enabled,
                "command": command, "timeout_seconds": timeout_seconds,
            }.items()
            if value is not None
        }
        gate_on = gated_update_action(
            getattr(existing.logic, "action", None) if existing is not None else None,
            hook_action_key if hook_action is not None else None,
            touched,
        )
        if gate_on is not None:
            gate = _gated_action_error(gate_on, config)
            if gate is not None:
                return gate
        # Resolve the effective action + merge onto stored params from the
        # existing hook, so editing a run_command hook's command without
        # re-stating the action works and does not reset timeout_seconds.
        params = _merge_run_command_params(
            hook_action_key if hook_action is not None else None,
            params, command, timeout_seconds,
            existing_logic=(existing.logic if existing is not None else None),
        )
        return _hook_update(
            hook_id=hook_id, name=name, event=event,
            action=(hook_action_key if hook_action is not None else None),
            text=text, params=params, matcher=matcher, enabled=enabled, scope=scope,
            config=config,
        )

    if action_key == "delete":
        if not hook_id:
            return "[Error]: delete requires hook_id."
        return _hook_delete(hook_id=hook_id, config=config)

    return "[Error]: action must be one of: create, update, delete."


@tool
def hook_info(
    action: str = "list",
    hook_id: Optional[str] = None,
    current_thread_only: bool = False,
    limit: int = 20,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """List, inspect, or debug lifecycle hooks.

    Actions: "list" for hook summaries, "detail" for one hook's full config,
    "test" for a dry-run render of a hook's text against sample data (no fire),
    "log" for recent executions (what fired, its outcome or fault, and timing;
    a "no_op" entry means the hook ran and produced nothing, no entry means it
    never fired).

    Args:
        action: "list", "detail", "test", or "log".
        hook_id: Required for detail/test; optional filter for log.
        current_thread_only: List only hooks that apply to this thread.
        limit: Max execution entries for log (default 20).
    """
    action_key = (action or "list").strip().lower()

    if action_key == "list":
        return _hook_list(current_thread_only=current_thread_only, config=config)

    if action_key in {"detail", "test"}:
        if not hook_id:
            return f"[Error]: {action_key} requires hook_id."
        return _hook_inspect(hook_id=hook_id, action=action_key, config=config)

    if action_key == "log":
        return _hook_log(hook_id=hook_id, limit=limit, config=config)

    return "[Error]: action must be one of: list, detail, test, log."


# Grouped export for CATALOG_TOOLS registration (opt-in).
HOOK_TOOLS = [
    hook_config,
    hook_info,
]
