"""The two seed tools of the thread request/reply contract (backlog #357).

``reply_to_thread`` is how a thread answers a request another thread made to
it; ``wait_for_reply`` is how a requesting thread waits inline for, or checks
on, a reply. The ledger, the prompts, and the delivery live in
``core/thread_requests.py``; these functions only resolve the calling thread
from the tool config, validate, and hand off. Both are ordinary seed tools:
a user or thread may disable them, and the disable surfaces warn about what
stops working (``tools/metadata.py::CAPABILITY_LOSS_NOTES``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Dict, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id_or_none

logger = logging.getLogger(__name__)


@tool
def reply_to_thread(
    request_id: str,
    content: str,
    final: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Send your reply to a request another thread made to you.

    A message that arrived under a [Request Metadata] block is a request from
    another thread; its request_id is in that block. That thread receives ONLY
    what you send through this tool: your ordinary final message is not
    delivered to it. Call this once with the complete answer (final=true, the
    default): the request closes and the answer reaches the asker exactly once,
    inline if it is waiting or as a new prompt on its thread otherwise. Do not
    also call the asker back with the same content.

    If you are not done yet and are waiting on something that will wake this
    thread later (a delegated thread, a background job), send a short status
    with final=false and end your turn: the request stays open, the asker sees
    the status when it checks, and you will be reminded to finish.

    Args:
        request_id: The id from the [Request Metadata] block (starts with "req-").
        content: The reply text (final=true) or a short progress status (final=false).
        final: True (default) closes the request with this as the answer; False records progress and keeps it open.
    """
    from ..core.agent import get_current_agent
    from ..core import thread_requests as tr

    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: reply_to_thread needs a thread context (no thread id on this call)."
    agent = get_current_agent()
    if agent is None:
        return "[Error]: NymeriaAgent not initialized."
    return tr.reply(
        request_id=(request_id or "").strip(),
        content=content,
        final=bool(final),
        replier_thread_id=thread_id,
        agent=agent,
    )


@tool
def wait_for_reply(
    request_id: str,
    timeout_seconds: int = 0,
    steps: int = 5,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Wait for, or check on, the reply to a request you made to another thread.

    A callable-thread call returns a [Requested] receipt with a request_id and
    the reply arrives later as a prompt on this thread. When you need the
    answer before you can continue, call this with timeout_seconds > 0: it
    blocks up to that long and returns the reply inline if it lands in time,
    else a [Waiting] status (the thread's state, progress it reported, and its
    recent steps as tool names plus what it said). Each call is capped at the
    server maximum (default 600 s); call again to keep waiting. With
    timeout_seconds=0 it returns the status immediately without waiting.

    Set the timeout above the time the task should plausibly take. Between
    waits your own queued messages get through, so several shorter waits keep
    you responsive where one long block would not. If you do not need the
    answer now, do not wait: end your turn and the reply will wake you.

    Args:
        request_id: The id from the [Requested] receipt (starts with "req-").
        timeout_seconds: Seconds to wait for the final reply; 0 checks without waiting. Clamped to the server maximum.
        steps: How many of the thread's most recent steps to show in a status (1..20, default 5).
    """
    from ..core.agent import get_current_agent
    from ..core import thread_requests as tr

    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: wait_for_reply needs a thread context (no thread id on this call)."
    agent = get_current_agent()
    if agent is None:
        return "[Error]: NymeriaAgent not initialized."
    req = tr.get_request((request_id or "").strip())
    seconds = tr.clamp_wait(timeout_seconds)
    registered = False
    if req is not None and seconds > 0 and req.caller_thread_id == thread_id:
        # The caller is actively waiting on the target: a /stop on the caller
        # cascades into the target for the duration of the wait, as a
        # blocking call's did. A request nobody waits on is the target's own
        # work and is not aborted by the caller's stop.
        try:
            agent.register_callable_invocation(thread_id, req.target_thread_id)
            registered = True
        except Exception:  # noqa: BLE001 - the cascade edge is best-effort
            logger.debug("wait_for_reply: invocation registration failed", exc_info=True)
    try:
        return tr.wait_for_reply(
            request_id=(request_id or "").strip(),
            timeout=seconds,
            waiter_thread_id=thread_id,
            agent=agent,
            steps=steps,
        )
    finally:
        if registered and req is not None:
            try:
                agent.unregister_callable_invocation(thread_id, req.target_thread_id)
            except Exception:  # noqa: BLE001
                logger.debug("wait_for_reply: invocation unregistration failed", exc_info=True)


def _wait_inline_timeout(args: Dict[str, Any]) -> Optional[float]:
    """SafeToolNode's per-call kill for a wait: the clamped wait plus a margin."""
    from ..core.thread_requests import wait_kill_timeout

    return wait_kill_timeout((args or {}).get("timeout_seconds"))


wait_for_reply.metadata = {"inline_wait_timeout": _wait_inline_timeout}

# Seed tools (tools/__init__.py SEED_TOOLS); no ToolGroup, so seed and catalog
# stay disjoint.
THREAD_REQUEST_TOOLS = [reply_to_thread, wait_for_reply]
