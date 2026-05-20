"""Resolution-driven turn for fire-and-forget credential prompts.

The agent's ``request_credential`` tool no longer blocks on the coordinator
future. Instead it attaches a done-callback that, when the user finishes
(or cancels, or the prompt times out), schedules a new agent turn whose
prompt summarises what happened. The agent reads the summary in its next
sub-turn, references the existing thread context, and decides what to do
next (retry the original tool, apologise to the user, etc.).

Why a dedicated module: the future resolves from many call sites
(``credential_prompts.py`` submit/cancel/exit endpoints, the OAuth
callback handler, the device-code poller, the coordinator's sweep task).
They all dispatch through ``AuthPromptCoordinator.resolve`` which fires
the done-callback. We need a single, well-tested formatter + injector
that all of them implicitly share.

This module is callable from any coroutine context. ``agent.astream``
itself handles thread-lock contention via the pending-prompt queue, so
we do not need a "try acquire or enqueue" wrapper here. Calling
``astream`` on a busy thread simply enrols us as the contender; the
holder will absorb the prompt at its next sub-turn boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_RESOLUTION_SOURCE = "credential_resolution"
_RESOLUTION_SOURCE_LABEL = "Credential prompt"


def format_resolution_message(
    *,
    provider: str,
    credential_id: Optional[str],
    payload: dict[str, Any],
    bind_outcome: Optional[str] = None,
) -> str:
    """Build the single-line summary the agent reads on its next turn.

    Keep this terse and structured. The agent uses the keywords to decide
    what to do next: ``status=active`` means retry the original tool;
    ``status=cancelled`` or ``user_exited`` mean ask the user what
    they want; ``status=user_message`` means treat the user's chat reply
    as the next instruction.
    """
    status = str(payload.get("status") or ("active" if payload.get("ok") else "error"))
    parts = [
        "CREDENTIAL_PROMPT_RESOLVED:",
        f"provider={provider}",
        f"status={status}",
    ]
    if credential_id:
        parts.append(f"credential_id={credential_id}")

    account_label = payload.get("account_label")
    if account_label:
        parts.append(f"account={account_label}")

    user_message = payload.get("user_message")
    if user_message:
        # Quote the user's reply so the agent can distinguish it from
        # the resolution metadata. Bound length to keep prompts compact.
        snippet = str(user_message).strip().replace("\n", " ")
        if len(snippet) > 500:
            snippet = snippet[:500] + ", (truncated)"
        parts.append(f"user_message={snippet!r}")

    last_test_error = payload.get("last_test_error") or payload.get("test_error")
    if last_test_error and status != "active":
        snippet = str(last_test_error).strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200]
        parts.append(f"last_test_error={snippet!r}")

    attempts = payload.get("attempts")
    if attempts:
        parts.append(f"attempts={attempts}")

    if bind_outcome:
        parts.append(f"bind={bind_outcome}")

    body = " ".join(parts)

    hint = _next_step_hint(status)
    if hint:
        return f"{body}\n\n{hint}"
    return body


def _next_step_hint(status: str) -> str:
    """Short imperative nudge appended to the summary so the agent knows
    what to do without needing the system prompt to spell it out."""
    if status == "active":
        return (
            "The credential is ready. Continue the original task that needed it "
            "(retry the tool call that was waiting on auth, or acknowledge the "
            "connection to the user)."
        )
    if status in {"user_exited", "cancelled"}:
        return (
            "The user closed the prompt without connecting. Ask them how they "
            "want to proceed instead of retrying silently."
        )
    if status == "user_message":
        return (
            "The user sent a chat reply instead of completing the prompt. Treat "
            "their reply as the next instruction."
        )
    if status in {"pending", "swept", "expired"}:
        return (
            "The prompt timed out without completion. Let the user know and ask "
            "whether they want to try again."
        )
    if status in {"denied", "error"}:
        return (
            "The prompt failed. Surface the failure to the user, do not silently "
            "retry the original tool."
        )
    return ""


def schedule_resolution_turn(
    *,
    thread_id: str,
    user_id: str,
    message: str,
) -> None:
    """Schedule a resolution turn on the running event loop.

    Safe to call from inside a ``future.add_done_callback`` (which runs on
    the future's loop). Uses ``asyncio.create_task`` so the resolver is
    not blocked while the turn runs.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "credential_prompt_injector cannot schedule turn: no running event loop "
            "(thread_id=%s, user_id=%s)",
            thread_id,
            user_id,
        )
        return

    loop.create_task(
        _drive_resolution_turn(
            thread_id=thread_id,
            user_id=user_id,
            message=message,
        ),
        name=f"credential-resolution-{thread_id}",
    )


async def _drive_resolution_turn(
    *,
    thread_id: str,
    user_id: str,
    message: str,
) -> None:
    """Run a fresh agent turn whose prompt is the resolution summary.

    Mirrors the chunk-publishing loop in ``trigger_manager._stream_live``:
    each chunk yielded by ``agent.astream`` is forwarded to the
    autonomous SSE bus so frontends and bots observe the new turn.

    Errors here are logged but never propagated: the future has already
    been resolved at this point, the user has already submitted (or
    cancelled) their action, and the original tool already returned.
    Failing to schedule a follow-up turn is a UX bug, not a correctness
    bug, so we don't crash the resolver thread.
    """
    # Lazy imports to avoid circular dependencies (agent.py imports tools
    # which import this module; this module imports agent only at fire
    # time).
    from .agent import get_current_agent
    from .event_bus import publish_agent_stream_chunk, publish_autonomous_event

    agent = get_current_agent()
    if agent is None:
        logger.error(
            "credential_prompt_injector: no current agent registered; "
            "cannot drive resolution turn for thread=%s",
            thread_id,
        )
        return

    task_id = f"credential-resolution-{thread_id}"

    publish_autonomous_event(
        event_type="task_started",
        thread_id=thread_id,
        user_id=user_id,
        task_id=task_id,
        data={
            "source": _RESOLUTION_SOURCE,
            "source_label": _RESOLUTION_SOURCE_LABEL,
        },
    )

    iteration_limit_hit = False
    response_chars = 0
    try:
        async for chunk in agent.astream(
            message=message,
            thread_id=thread_id,
            user_id=user_id,
            source=_RESOLUTION_SOURCE,
            source_label=_RESOLUTION_SOURCE_LABEL,
        ):
            publish_agent_stream_chunk(
                chunk,
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
            )
            chunk_type = chunk.get("type")
            if chunk_type == "response":
                response_chars += len(str(chunk.get("content") or ""))
            elif chunk_type == "iteration_limit":
                iteration_limit_hit = True
                logger.warning(
                    "credential_prompt_injector hit iteration limit on thread=%s",
                    thread_id,
                )
            elif chunk_type == "error":
                logger.error(
                    "credential_prompt_injector stream error on thread=%s: %s",
                    thread_id,
                    chunk.get("content") or chunk.get("code"),
                )
    except Exception:
        logger.exception(
            "credential_prompt_injector failed driving resolution turn on thread=%s",
            thread_id,
        )
    finally:
        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=user_id,
            task_id=task_id,
            data={
                "source": _RESOLUTION_SOURCE,
                "response_chars": response_chars,
                "partial": iteration_limit_hit,
            },
        )


__all__ = [
    "format_resolution_message",
    "schedule_resolution_turn",
]
