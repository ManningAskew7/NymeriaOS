"""Factory for creating LangChain tools from callable thread configurations.

Callable threads replace the old sub-agent system. Any thread with callable=True
gets a tool wrapper so it can be invoked directly (e.g., MyAgent(task="...")).

Also hosts the kit-declared thread-template tools (backlog #26): a Skill Kit
can declare callable-thread TEMPLATES; ``create_template_thread_tool`` wraps
one as a tool whose first call materializes the thread via the spawn_thread
machinery and whose later calls route to the now-existing callable thread.
"""

import logging
import re
import threading
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, tool as tool_decorator

logger = logging.getLogger(__name__)


def _check_callable_ownership(agent, user_id, *, name, thread_id) -> Optional[str]:
    """Fail-closed cross-user ownership gate for a callable thread tool.

    The global tool registry is shared across users (sync_agent_tools rebuilds
    one default graph), so a callable thread created by user A would otherwise
    be invocable from user B's chat. Reject mismatched ownership at runtime,
    including admin-as-admin. Admin act-as keeps working because the API
    resolves config.user_id to the target owner before the graph runs. Legacy
    unowned callables are admin-only for migration. Non-admin invocation of an
    unowned callable is rejected because stale caches or guessed tool names must
    fail closed at this auth boundary.

    Returns an error string to surface to the model, or None to allow the call.
    """
    if not (agent and user_id):
        return None
    try:
        owner = agent.accounts_repo.get_thread_owner(thread_id)
        caller = agent.accounts_repo.get_user_by_id(user_id)
        caller_is_admin = bool(caller and caller.role == "admin")
        if owner is None:
            if caller_is_admin:
                logger.warning(
                    f"{name}: admin user_id={user_id} invoking legacy "
                    f"unowned callable thread {thread_id}"
                )
            else:
                logger.warning(
                    f"{name}: invocation blocked - legacy unowned callable "
                    f"thread {thread_id} can only be invoked by an admin"
                )
                return (
                    f"[Error]: {name} is not available to this user. "
                    f"Callable threads are scoped to the user who created them."
                )
        elif owner != user_id:
            logger.warning(
                f"{name}: invocation blocked - caller user_id={user_id} "
                f"does not own callable thread {thread_id} (owned by {owner})"
            )
            return (
                f"[Error]: {name} is not available to this user. "
                f"Callable threads are scoped to the user who created them."
            )
    except Exception as e:  # noqa: BLE001
        # Fail closed: this is an auth boundary, not a best-effort
        # lookup. If ownership can't be verified, refuse rather than
        # let an unverified caller through.
        logger.warning(f"{name}: ownership check failed: {e}")
        return (
            f"[Error]: {name} ownership could not be verified. "
            f"Refusing invocation."
        )
    return None


def _check_team_visibility(agent, parent_thread_id, *, name, thread_id) -> Optional[str]:
    """Reject a callable outside the parent thread's team bubble.

    Teams are isolated in both directions: a teamed thread invokes only
    same-team callables, and an unteamed thread invokes only unteamed
    callables. Returns an error string, or None when allowed or not
    applicable.
    """
    if agent and parent_thread_id and hasattr(agent, "is_callable_visible_to_thread"):
        if not agent.is_callable_visible_to_thread(parent_thread_id, thread_id):
            return (
                f"[Error]: {name} is not visible from this thread. Callable "
                "teams are isolated: a teamed thread sees only same-team "
                "callables, and an unteamed thread sees only unteamed "
                "callables. Put both threads in the same team (or both "
                "outside teams) to allow this call."
            )
    return None


def _inline_wait_timeout(args: Dict[str, Any]) -> Optional[float]:
    """SafeToolNode's per-call kill for a callable-tool call that waits inline
    (``wait_seconds``): the clamped wait plus a margin; None (the node's
    default) for a call that does not wait."""
    from ..core.thread_requests import wait_kill_timeout

    return wait_kill_timeout((args or {}).get("wait_seconds"))


def _resolve_parent_name_and_trigger(
    agent, user_id, parent_thread_id
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the parent thread's display name and the trigger_override string.

    Tries the thread title, then the parent's callable_name, then falls back to
    the raw thread id. Returns ``(parent_name, trigger_override)``; both are
    ``None`` when there is no parent thread to resolve.
    """
    trigger_override = None
    parent_name = None
    if agent and parent_thread_id:
        # Try thread title first
        try:
            parent_meta = agent.thread_metadata_manager.get_thread(user_id, parent_thread_id)
            if parent_meta and parent_meta.title:
                parent_name = parent_meta.title
        except Exception:
            logger.debug("Failed to resolve parent thread title")
        # Fall back to callable_name if parent is itself a callable thread
        if not parent_name:
            try:
                parent_tc = agent.thread_config_manager.get_config(parent_thread_id)
                if parent_tc and parent_tc.callable_name:
                    parent_name = parent_tc.callable_name
            except Exception:
                logger.debug("Failed to resolve parent callable name")
        # Last resort: raw thread ID
        if not parent_name:
            parent_name = parent_thread_id
        trigger_override = f'Thread("{parent_thread_id}", "{parent_name}")'
    return parent_name, trigger_override


def create_callable_thread_tool(thread_config) -> BaseTool:
    """Create a LangChain tool from a callable thread config.

    The generated tool wraps the thread executor and routes calls through
    NymeriaAgent.chat() to a persistent callable thread.

    Args:
        thread_config: ThreadConfig with callable=True

    Returns:
        A BaseTool that delegates to the callable thread
    """
    name = thread_config.callable_name
    thread_id = thread_config.thread_id
    description = thread_config.callable_description or f"Invoke the {name} thread"

    tool_description = f"""{description}

This is a specialized thread with its own tools and context. Calling it sends
a REQUEST: you get a [Requested] receipt with a request_id at once, the thread
works on the task, and its reply (sent with its reply_to_thread tool) reaches
you exactly once as a new prompt on your thread, so you can end your turn and
continue when it arrives. For a quick question you need answered now, pass
wait_seconds: the call also waits up to that long and returns the reply
inline; if it does not land in time you get a [Waiting] status and the request
stays open (wait again with wait_for_reply(request_id, timeout_seconds), or
check progress with timeout_seconds=0). Waits are capped per call (server
maximum, default 600 s); while you wait your own incoming messages queue
until the wait returns, so prefer no wait or short waits for long work.
Nothing the thread says outside reply_to_thread is delivered to you. A second
call with a new task while one is open is a second, separate request.
"""

    # Capture in closure
    _name = name
    _thread_id = thread_id

    @tool_decorator(_name, return_direct=False)
    def callable_thread_tool_func(
        task: str,
        wait_seconds: int = 0,
        scheduled_for: Optional[str] = None,
        if_busy: Literal["queue", "error"] = "queue",
        *,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        """Send this thread a task as a request and, optionally, wait for its reply.

        Args:
            task: A clear description of what you want this thread to do. Be specific about the goal, the constraints, and what the reply should contain.
            wait_seconds: 0 (default) returns the [Requested] receipt at once; the reply arrives later as a prompt on your thread. N > 0 also waits up to N seconds for the reply and returns it inline when it lands in time, else a [Waiting] status (the request stays open). Clamped to the server maximum (default 600). Set it above the time the task should take, or wait again with wait_for_reply.
            scheduled_for: Delay the request until a time such as "30s", "5m", "1h", "1d", or "YYYY-MM-DD HH:MM". Omit for an immediate request; cannot be combined with wait_seconds.
            if_busy: "queue" (default) queues the request behind the thread's current turn (it is absorbed at that turn's next tool-round boundary); "error" returns [Busy] instead when the thread is mid-turn.
        """
        from langchain_core.runnables.config import var_child_runnable_config
        from langchain_core.tracers.context import tracing_v2_callback_var, run_collector_var
        from ..core.thread_agent_executor import (
            invoke as thread_invoke,
            request as thread_request,
        )

        user_id = config.get("configurable", {}).get("user_id", "default") if config else "default"
        parent_thread_id = config.get("configurable", {}).get("thread_id") if config else None

        logger.info(
            f"{_name} callable thread tool called: wait_seconds={wait_seconds}, task={task[:100]}..."
        )

        if if_busy not in ("queue", "error"):
            return "[Error]: if_busy must be 'queue' or 'error'."
        try:
            wait_requested = int(wait_seconds or 0)
        except (TypeError, ValueError):
            return "[Error]: wait_seconds must be a whole number of seconds (0 for no wait)."
        if wait_requested < 0:
            return "[Error]: wait_seconds must be 0 or a positive number of seconds."
        if wait_requested > 0 and scheduled_for and scheduled_for.strip():
            return (
                "[Error]: wait_seconds cannot be combined with scheduled_for; a "
                "scheduled request replies when its schedule fires."
            )

        from ..core.agent import get_current_agent
        _agent = get_current_agent()

        # Auth / visibility guards, in this order. The first guard returning a
        # non-None message short-circuits and is surfaced to the model; each
        # reproduces its own applicability precondition, including the
        # fail-closed ownership boundary which returns an error rather than
        # raising. See the _check_* helpers above.
        error = _check_callable_ownership(_agent, user_id, name=_name, thread_id=_thread_id)
        if error:
            return error
        error = _check_team_visibility(_agent, parent_thread_id, name=_name, thread_id=_thread_id)
        if error:
            return error

        # Resolve parent thread name for trigger metadata
        parent_name, trigger_override = _resolve_parent_name_and_trigger(
            _agent, user_id, parent_thread_id
        )

        # Break the LangChain callback/tracing inheritance chain so an inner
        # graph.invoke() doesn't propagate LLM token events back to the
        # parent's astream_events() (stream leakage). The request path copies
        # THIS context onto its worker thread, so the reset must happen
        # before the dispatch; the no-caller-thread fallback runs inline here.
        config_token = var_child_runnable_config.set(None)
        callback_token = tracing_v2_callback_var.set(None)
        collector_token = run_collector_var.set(None)
        try:
            if not parent_thread_id:
                # No calling thread to reply to (no turn context): the
                # synchronous form, the callee's text returned directly. A
                # schedule needs a thread for the reply to land on.
                if scheduled_for and scheduled_for.strip():
                    return (
                        "[Error]: scheduled_for needs a calling thread for the "
                        "reply to reach (no thread context on this call)."
                    )
                if if_busy == "error" and _agent is not None:
                    try:
                        if _agent._thread_locks.is_thread_busy(_thread_id):
                            return (
                                f"[Busy]: {_name} is busy on thread '{_thread_id}'. "
                                "Ask again later or retry with if_busy='queue'."
                            )
                    except Exception:  # noqa: BLE001 - an unreadable lock is idle
                        pass
                return thread_invoke(
                    _thread_id,
                    task,
                    user_id,
                    _name,
                    trigger_override=trigger_override,
                )
            # The request path owns the wait too (waiter registered before
            # the callee is dispatched, abort-cascade edge for its duration).
            receipt, _req = thread_request(
                _thread_id,
                task,
                user_id,
                _name,
                caller_thread_id=parent_thread_id,
                caller_name=parent_name,
                trigger_override=trigger_override,
                scheduled_for=scheduled_for,
                if_busy=if_busy,
                wait_seconds=wait_requested,
            )
            return receipt
        except Exception as e:
            logger.error(f"{_name} callable thread failed: {e}", exc_info=True)
            return f"[Error]: {_name} invocation failed: {str(e)}"
        finally:
            run_collector_var.reset(collector_token)
            tracing_v2_callback_var.reset(callback_token)
            var_child_runnable_config.reset(config_token)

    callable_thread_tool_func.description = tool_description
    # SafeToolNode reads this per call: a waiting call is killed at its own
    # wait plus a margin, never at the plain tool_timeout.
    callable_thread_tool_func.metadata = {"inline_wait_timeout": _inline_wait_timeout}
    return callable_thread_tool_func


# ---------------------------------------------------------------------------
# Kit-declared thread templates (backlog #26)
# ---------------------------------------------------------------------------

# In-process materialization locks, keyed (user_id, tool_name). Process-local
# is authoritative because the API process is the single agent runtime (the
# same argument that keeps ThreadLockManager in-process).
_template_locks_guard = threading.Lock()
_template_locks: Dict[Tuple[str, str], threading.Lock] = {}

_SPAWN_RECEIPT_RE = re.compile(r"^\[Spawned\]: thread_id=(\S+)", re.MULTILINE)

# Sentinel returned by _find_materialized_thread when the lookup itself
# failed: the caller must NOT treat that as "not materialized" and spawn
# (a duplicate rename would make name-based routing arbitrary forever).
_LOOKUP_FAILED = object()


def _template_lock(user_id: str, tool_name: str) -> threading.Lock:
    key = (user_id, tool_name)
    with _template_locks_guard:
        lock = _template_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _template_locks[key] = lock
        return lock


def _resolve_declared_template(agent, skill_name: str, tool_name: str, user_id: str):
    """Re-resolve the template from the CURRENT skill store, or None.

    A stale graph may still carry a template tool after the kit was
    uninstalled or the template removed; calls must fail closed on the
    skill's existence (installed and visible to the user; thread enablement
    is deliberately not required, matching the deferred-path loadability
    rule).
    """
    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is None:
        return None
    try:
        skill = skill_manager.get(skill_name, user_id=user_id)
    except Exception:  # noqa: BLE001 - lookup failure = unresolvable
        logger.warning(
            "template %r: skill lookup failed for %r", tool_name, skill_name,
            exc_info=True,
        )
        return None
    if skill is None:
        return None
    for template in skill.thread_templates:
        if template.name == tool_name:
            return template
    return None


def _find_materialized_thread(agent, user_id: str, tool_name: str):
    """Find the user's already-materialized thread for this template.

    Routing is by ``callable_name`` equality: materialization renames the
    spawned thread's callable_name to the template tool name, so subsequent
    calls (and ordinary graph builds) reach it like any callable thread.
    Returns the ThreadConfig, ``None`` when genuinely not materialized, or
    ``_LOOKUP_FAILED`` when a lookup errored (fail closed, mirroring
    ``_check_callable_ownership``: the caller returns a retryable error
    instead of spawning a duplicate).
    """
    try:
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
    except Exception:  # noqa: BLE001 - fail closed, never spawn blind
        logger.warning(
            "template %r: owned-thread lookup failed", tool_name, exc_info=True
        )
        return _LOOKUP_FAILED
    if not owned:
        return None
    try:
        return agent.thread_config_manager.get_callable_thread_by_name(
            tool_name, owned_thread_ids=owned
        )
    except Exception:  # noqa: BLE001 - fail closed, never spawn blind
        logger.warning(
            "template %r: callable lookup failed", tool_name, exc_info=True
        )
        return _LOOKUP_FAILED


def _spawn_template_thread(agent, template, config) -> str:
    """Seam: create the thread via the spawn_thread tool machinery.

    Calling the tool function (not a private path) keeps every spawn gate
    intact for the CALLER: depth cap, rate cap, admin/developer tool gates,
    kit strict-bind rollback, ownership claim, and the ``[Spawned]`` receipt.
    """
    from ..tools.spawn_thread import spawn_thread

    return cast(Any, spawn_thread).func(
        title=template.thread_title,
        instructions=template.instructions,
        optional_tools=list(template.tools) or None,
        kit=template.kit,
        llm_provider=template.provider,
        llm_model=template.model,
        ttl_hours=template.ttl_hours,
        make_callable=True,
        action="create",
        config=config,
    )


def _finalize_materialized_thread(
    agent, thread_id: str, skill_name: str, template, user_id: str
):
    """Rename the fresh spawn to the template tool name; returns (tc, error)."""
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return None, (
            f"[Error]: Spawned thread {thread_id} has no config after "
            "materialization; retry the call."
        )
    updated = tc.model_copy(
        update={
            "callable": True,
            "callable_name": template.name,
            "callable_description": template.description[:500],
        }
    )
    if not agent.thread_config_manager.save_config(updated):
        return None, (
            f"[Error]: Failed to bind the template name to spawned thread "
            f"{thread_id}. Delete the orphan with spawn_thread("
            f'action="delete", delete_thread_id="{thread_id}") and retry.'
        )
    try:
        agent.invalidate_thread_config_cache(thread_id)
    except Exception:  # noqa: BLE001
        logger.debug("template finalize: cache invalidate failed", exc_info=True)
    # Provenance stamp (best-effort; routing never depends on it).
    try:
        meta = agent.thread_metadata_manager.get_thread(user_id, thread_id)
        platform_meta = dict(meta.platform_meta or {}) if meta else {}
        platform_meta["template_skill"] = skill_name
        platform_meta["template_name"] = template.name
        agent.thread_metadata_manager.upsert_thread(
            user_id, thread_id, platform_meta=platform_meta
        )
    except Exception:  # noqa: BLE001
        logger.debug("template finalize: provenance stamp failed", exc_info=True)
    try:
        agent.sync_agent_tools()
    except Exception:  # noqa: BLE001
        logger.warning("template finalize: sync_agent_tools failed", exc_info=True)
    return updated, None


def _invoke_materialized(child_tc, task: str, wait_seconds: int, config) -> str:
    """Seam: route the call through the ordinary callable-thread tool.

    Shares the ownership / team guards and the request/wait plumbing with
    every other callable thread.
    """
    callable_tool = create_callable_thread_tool(child_tc)
    return cast(Any, callable_tool).func(
        task=task, wait_seconds=wait_seconds, config=config
    )


def create_template_thread_tool(skill_name: str, template) -> BaseTool:
    """Create the tool for one kit-declared thread template.

    The tool's first call materializes the thread (spawned from the declared
    config, then renamed to the template tool name) and delivers the call's
    task as its first turn; later calls route to the same thread like any
    callable thread. Once materialized, the ordinary callable tool shadows
    this one at graph build (same name), so this wrapper mostly serves the
    not-yet-materialized state.
    """
    tool_name = template.name
    tool_description = f"""{template.description}

Callable thread template from Skill Kit '{skill_name}'. The first call creates
the thread from the kit's declared configuration and sends it your task as a
request; later calls route to the same thread. You get a [Requested] receipt
with a request_id at once and the thread's reply (sent with reply_to_thread)
arrives as a prompt on your thread; pass wait_seconds to wait inline for it.
"""

    _skill_name = skill_name
    _tool_name = tool_name

    @tool_decorator(_tool_name, return_direct=False)
    def template_thread_tool(
        task: str,
        wait_seconds: int = 0,
        *,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        """Send a task to this kit-declared thread (created on first call) as a request.

        Args:
            task: A clear description of what you want this thread to do.
            wait_seconds: 0 (default) returns the [Requested] receipt at once
                (the reply arrives later as a prompt on your thread); N > 0
                also waits up to N seconds for the reply inline (clamped to
                the server maximum).
        """
        from ..core.agent import get_current_agent

        try:
            wait_requested = int(wait_seconds or 0)
        except (TypeError, ValueError):
            return "[Error]: wait_seconds must be a whole number of seconds (0 for no wait)."
        if wait_requested < 0:
            return "[Error]: wait_seconds must be 0 or a positive number of seconds."

        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent; cannot run this thread template."
        user_id = (
            config.get("configurable", {}).get("user_id", "default")
            if config
            else "default"
        )

        template_now = _resolve_declared_template(
            agent, _skill_name, _tool_name, user_id
        )
        if template_now is None:
            return (
                f"[Error]: Kit '{_skill_name}' no longer declares thread "
                f"template '{_tool_name}' (or is uninstalled); this tool is "
                "stale. Re-check the kit with Skill() or search_skills."
            )

        materialized_receipt = ""
        with _template_lock(user_id, _tool_name):
            child_tc = _find_materialized_thread(agent, user_id, _tool_name)
            if child_tc is _LOOKUP_FAILED:
                return (
                    f"[Error]: Could not verify whether template "
                    f"'{_tool_name}' already has a thread (ownership lookup "
                    "failed), so nothing was spawned to avoid a duplicate. "
                    "Retry shortly."
                )
            if child_tc is None:
                spawn_result = _spawn_template_thread(agent, template_now, config)
                match = _SPAWN_RECEIPT_RE.search(spawn_result or "")
                if match is None:
                    # Spawn refused (depth/rate cap, gated tools, bad kit, ...):
                    # surface its error verbatim; nothing was created.
                    return spawn_result or (
                        "[Error]: thread template spawn returned no receipt."
                    )
                thread_id = match.group(1)
                child_tc, finalize_error = _finalize_materialized_thread(
                    agent, thread_id, _skill_name, template_now, user_id
                )
                if child_tc is None:
                    return finalize_error or "[Error]: materialization failed."
                logger.info(
                    "materialized template thread %s as %s (kit %s)",
                    thread_id, _tool_name, _skill_name,
                )
                materialized_receipt = (
                    f"[Materialized]: thread_id={thread_id} (created from kit "
                    f"'{_skill_name}' template '{_tool_name}')\n\n"
                )

        response = _invoke_materialized(child_tc, task, wait_requested, config)
        return materialized_receipt + response

    template_thread_tool.description = tool_description
    template_thread_tool.metadata = {"inline_wait_timeout": _inline_wait_timeout}
    return template_thread_tool


def get_callable_thread_tools(thread_config_manager) -> List[BaseTool]:
    """Get tools for all callable threads.

    Scans all callable=True thread configs and creates a LangChain tool
    for each one.

    Args:
        thread_config_manager: ThreadConfigManager instance

    Returns:
        List of BaseTool instances for callable threads
    """
    tools = []
    for tc in thread_config_manager.list_callable_threads():
        if not tc.callable_name:
            continue
        try:
            tool = create_callable_thread_tool(tc)
            tools.append(tool)
            logger.debug(f"Created callable thread tool: {tc.callable_name} -> {tc.thread_id}")
        except Exception as e:
            logger.error(f"Failed to create callable thread tool for {tc.callable_name}: {e}")
    return tools
