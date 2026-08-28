"""Callable lifecycle: invocation tracking and tool-timeout handling.

Extracted from ``NymeriaAgent``. Each function takes the agent instance
as its first argument; thin facades on the class preserve the original
call shape so ``chat()``/``astream()`` unchanged, ``agent_graph.py``'s
bound-method capture of ``agent._on_tool_timeout`` keeps working, and
every external ``abort_with_cascade`` caller sees no change.

The cluster handles two interlocked concerns:

1. Invocation tracking: a parent->children DAG of active callable
   invocations, walked by ``is_ancestor_invocation`` for cycle
   detection and by ``abort_with_cascade`` for recursive abort
   propagation.
2. Tool-timeout handling: when ``SafeToolNode`` times out a callable
   tool, resolve the target thread (respecting team scoping) and
   trigger a cascading abort. ``patch_dangling_tool_calls`` cleans up
   the checkpoint afterwards so the next turn sees a valid message
   sequence.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)


def register_callable_invocation(
    agent: "NymeriaAgent",
    parent_thread_id: str,
    child_thread_id: str,
) -> None:
    """Register that parent_thread_id has spawned child_thread_id.

    Used for cascading abort: stopping a parent also stops its children.
    """
    with agent._invocations_lock:
        if parent_thread_id not in agent._active_callable_invocations:
            agent._active_callable_invocations[parent_thread_id] = set()
        agent._active_callable_invocations[parent_thread_id].add(child_thread_id)


def unregister_callable_invocation(
    agent: "NymeriaAgent",
    parent_thread_id: str,
    child_thread_id: str,
) -> None:
    """Remove a completed/aborted child from the parent's active set."""
    with agent._invocations_lock:
        if parent_thread_id in agent._active_callable_invocations:
            agent._active_callable_invocations[parent_thread_id].discard(child_thread_id)
            if not agent._active_callable_invocations[parent_thread_id]:
                del agent._active_callable_invocations[parent_thread_id]


def is_ancestor_invocation(
    agent: "NymeriaAgent",
    child_thread_id: str,
    target_thread_id: str,
) -> bool:
    """Check if target_thread_id is an ancestor of child_thread_id in the active call chain.

    Returns True if invoking target from child would create a circular
    call (i.e. target is waiting -- directly or transitively -- for
    child's output).
    """
    if child_thread_id and child_thread_id == target_thread_id:
        # Direct self-call: no invocation edge exists yet, but a blocking
        # ask would wait on the thread's own lock (held by the calling
        # turn) until tool timeout. Reject it up front.
        return True
    with agent._invocations_lock:
        visited = set()
        queue = [child_thread_id]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            for parent, children in agent._active_callable_invocations.items():
                if current in children:
                    if parent == target_thread_id:
                        return True
                    queue.append(parent)
    return False


def abort_with_cascade(
    agent: "NymeriaAgent",
    thread_id: str,
    *,
    restore_queue: bool = False,
) -> list:
    """Signal abort on a thread and recursively on all its active callable children.

    Also clears the per-thread pending-prompt queue. By default every
    entry is dropped with ``abandoned=True`` so any blocked queuers
    (user chat, callable threads, MCP) wake up with an explicit
    ``aborted`` error event instead of hanging on the lock_timeout.

    ``restore_queue=True`` (the user-initiated stop path) instead hands
    user-source prompts back: they are drained via ``clear_with_restore``
    and returned (FIFO) so the stop surface can give the raw texts back
    to the user; programmatic queuers keep the abandoned semantics. The
    cascade into callable children always discards: only the thread the
    user explicitly stopped restores.

    Returns the restored ``PendingPrompt`` entries (empty unless
    ``restore_queue`` is set and user prompts were queued).
    """
    restored: list = []
    agent._thread_locks.signal_abort(thread_id)
    try:
        from .pending_prompt_queue import get_pending_queue
        if restore_queue:
            restored, discarded = get_pending_queue().clear_with_restore(thread_id)
            if restored or discarded:
                logger.info(
                    f"Stop on thread {thread_id} restored {len(restored)} and "
                    f"discarded {discarded} pending prompt(s)"
                )
        else:
            cleared = get_pending_queue().clear(thread_id, abandoned=True)
            if cleared:
                logger.info(
                    f"Abort on thread {thread_id} cleared {cleared} pending prompt(s)"
                )
    except Exception as e:
        logger.warning(f"Failed to clear pending prompts on abort for {thread_id}: {e}")
    try:
        from .browser_command_coordinator import get_browser_command_coordinator
        aborted = get_browser_command_coordinator().abort_thread(thread_id)
        if aborted:
            logger.info(
                f"Abort on thread {thread_id} aborted {aborted} pending browser command(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to abort pending browser commands on abort for {thread_id}: {e}"
        )
    try:
        from .browser_login_sessions import get_browser_login_registry
        login_sessions = get_browser_login_registry().abort_thread(thread_id)
        if login_sessions:
            logger.info(
                f"Abort on thread {thread_id} ended {login_sessions} live browser login session(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to end browser login sessions on abort for {thread_id}: {e}"
        )
    try:
        from .ui_prompt_coordinator import get_ui_prompt_coordinator
        prompts_aborted = get_ui_prompt_coordinator().abort_thread(thread_id)
        if prompts_aborted:
            logger.info(
                f"Abort on thread {thread_id} aborted {prompts_aborted} pending UI prompt(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to abort pending UI prompts on abort for {thread_id}: {e}"
        )
    try:
        from .cli_config_coordinator import get_cli_config_coordinator
        cli_aborted = get_cli_config_coordinator().abort_thread(thread_id)
        if cli_aborted:
            logger.info(
                f"Abort on thread {thread_id} aborted {cli_aborted} pending CLI config command(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to abort pending CLI config commands on abort for {thread_id}: {e}"
        )
    try:
        from .hook_approvals import get_hook_approval_coordinator
        held = get_hook_approval_coordinator().abort_thread(thread_id)
        if held:
            logger.info(
                f"Abort on thread {thread_id} aborted {held} held hook approval(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to abort held hook approvals on abort for {thread_id}: {e}"
        )
    try:
        from .fallback_approvals import get_fallback_approval_coordinator
        parked = get_fallback_approval_coordinator().abort_thread(thread_id)
        if parked:
            logger.info(
                f"Abort on thread {thread_id} aborted {parked} parked fallback prompt(s)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to abort parked fallback prompts on abort for {thread_id}: {e}"
        )
    with agent._invocations_lock:
        children = set(agent._active_callable_invocations.get(thread_id, ()))
    for child_id in children:
        logger.info(f"Cascading abort from thread {thread_id} to child {child_id}")
        agent.abort_with_cascade(child_id)
    return restored


DANGLING_MARKER_CANCELLED = "[Cancelled by user before this tool completed]"
DANGLING_MARKER_REPEATED = (
    "[Not run: stopped by the repeated-tool-call safety guard "
    "before this tool executed]"
)
DANGLING_MARKER_ITERATION_LIMIT = (
    "[Not run: the turn reached its iteration limit before this tool executed]"
)


def dangling_marker_for_safety_reason(reason: Optional[str]) -> str:
    """Marker text for a turn-safety stop, by reason.

    Cap halts normally leave no dangling calls anymore (they halt at the
    sub-turn boundary after the batch executes, backlog #27), so the
    iteration-limit marker only appears on legacy/edge tails.
    """
    from ..vendor.react_agent.nodes import TURN_SAFETY_REASON_REPEATED_TOOL_RESULT

    if reason == TURN_SAFETY_REASON_REPEATED_TOOL_RESULT:
        return DANGLING_MARKER_REPEATED
    return DANGLING_MARKER_ITERATION_LIMIT


def patch_dangling_tool_calls(
    agent: "NymeriaAgent",
    graph,
    config: dict,
    marker: str = DANGLING_MARKER_CANCELLED,
) -> int:
    """Patch dangling AIMessage tool_calls with synthetic ToolMessages after cancellation.

    When a turn is cancelled mid-execution, the checkpoint may contain
    an AIMessage with tool_calls but no corresponding ToolMessages (the
    tools were still running when the abort fired). This leaves an
    invalid message sequence that confuses the LLM on the next turn --
    it may hallucinate that earlier (completed) tool calls also never
    ran.

    Loads the current state, detects unmatched tool_calls on the last
    AIMessage, and injects synthetic ToolMessages via update_state so
    the next turn sees a clean, valid history.

    ``marker`` is the synthetic ToolMessage text the model reads on the
    next turn (or on a /resume re-drive). It defaults to the cancellation
    wording; turn-safety stops pass a reason-aware marker via
    ``dangling_marker_for_safety_reason`` so a safety halt is not
    mislabeled as a user cancel.

    Returns the number of synthetic ToolMessages injected (0 if state
    was clean).
    """
    try:
        state = graph.get_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return 0

        last_msg = messages[-1]
        if not (isinstance(last_msg, AIMessage) and last_msg.tool_calls):
            return 0

        pending_ids = {tc["id"] for tc in last_msg.tool_calls if tc.get("id")}

        for msg in reversed(messages[:-1]):
            if isinstance(msg, ToolMessage) and msg.tool_call_id in pending_ids:
                pending_ids.discard(msg.tool_call_id)
            elif isinstance(msg, (AIMessage, HumanMessage)):
                break

        if not pending_ids:
            return 0

        synthetic = []
        for tc in last_msg.tool_calls:
            if tc.get("id") in pending_ids:
                synthetic.append(ToolMessage(
                    content=marker,
                    tool_call_id=tc["id"],
                    name=tc.get("name", ""),
                ))

        graph.update_state(config, {"messages": synthetic})
        names = [tc.get("name", "?") for tc in last_msg.tool_calls if tc.get("id") in pending_ids]
        logger.info(
            f"Patched {len(synthetic)} dangling tool call(s) after cancellation: {names}"
        )
        return len(synthetic)

    except Exception as e:
        logger.warning(f"Failed to patch dangling tool calls: {e}")
        return 0


def callable_timeout_scope_user_id(
    agent: "NymeriaAgent",
    user_id: Optional[str],
    caller_thread_id: Optional[str],
) -> Optional[str]:
    """Resolve the user whose callable list was bound into the active graph."""
    scope_user_id = user_id
    if not caller_thread_id:
        return scope_user_id

    try:
        caller_tc = agent.thread_config_manager.get_config(caller_thread_id)
        if caller_tc and caller_tc.callable and caller_tc.callable_name:
            return agent.accounts_repo.get_thread_owner(caller_thread_id) or "default"

        if not scope_user_id or scope_user_id == "default":
            return agent.accounts_repo.get_thread_owner(caller_thread_id) or scope_user_id
    except Exception as e:
        logger.debug(
            f"Could not resolve callable timeout owner for thread {caller_thread_id}: {e}"
        )

    return scope_user_id


def resolve_callable_timeout_thread_id(
    agent: "NymeriaAgent",
    tool_name: Optional[str],
    user_id: Optional[str],
    caller_thread_id: Optional[str],
) -> Optional[str]:
    if not tool_name:
        return None

    global_thread_id = agent._callable_tool_thread_map.get(tool_name)
    scope_user_id = agent._callable_timeout_scope_user_id(user_id, caller_thread_id)
    if not scope_user_id:
        return global_thread_id

    try:
        scoped_callables = agent._get_team_scoped_callable_threads(
            user_id=scope_user_id,
            caller_thread_id=caller_thread_id or "",
        )
    except Exception as e:
        if global_thread_id:
            logger.warning(
                "Skipping auto-abort for timed-out callable tool '%s': "
                "could not resolve scoped callable list for user=%s thread=%s: %s",
                tool_name,
                scope_user_id,
                caller_thread_id,
                e,
            )
        return None

    for callable_tc in scoped_callables:
        if callable_tc.callable_name == tool_name:
            return callable_tc.thread_id

    if global_thread_id:
        logger.warning(
            "Skipping auto-abort for timed-out callable tool '%s': "
            "not visible to user=%s thread=%s",
            tool_name,
            scope_user_id,
            caller_thread_id,
        )
    return None


def on_tool_timeout(
    agent: "NymeriaAgent",
    input_dict: dict,
    config: Optional[dict] = None,
) -> None:
    """Called when SafeToolNode times out. Auto-aborts callable threads (with cascade)."""
    messages = input_dict.get("messages", []) if isinstance(input_dict, dict) else []
    last_message = messages[-1] if messages else None
    if not (isinstance(last_message, AIMessage) and last_message.tool_calls):
        return
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    user_id = configurable.get("user_id")
    caller_thread_id = configurable.get("thread_id")
    for tc in last_message.tool_calls:
        tool_name = tc.get("name")
        thread_id = agent._resolve_callable_timeout_thread_id(
            tool_name,
            user_id,
            caller_thread_id,
        )
        if thread_id:
            logger.warning(
                f"Auto-aborting callable thread '{tool_name}' "
                f"(thread={thread_id}, caller_thread={caller_thread_id}, "
                f"user={user_id}) after tool timeout"
            )
            agent.abort_with_cascade(thread_id)
