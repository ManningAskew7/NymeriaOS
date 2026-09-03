"""Shared turn-loop helpers for ``NymeriaAgent.chat()`` / ``astream()``.

Hosts the two orchestration loop families the sync and async entry points
must keep behaviorally identical, each as a sync helper driven by
``chat()`` plus an async-generator twin driven by ``astream()``: the
in-turn tool-reload loop and the sub-turn compaction halt loop. Before
this module each was copy-pasted between (and within) the two paths,
the dominant drift risk in ``agent.py``. Also hosts
``build_queued_prompt_messages``, the one non-divergent piece of the
queued-prompt drain; the drain skeletons themselves stay in
``chat()``/``astream()`` (they legitimately differ) and the batch
mailbox-wake choreography lives in ``pending_prompt_queue.py``.

Conventions (matching the ``agent_*.py`` extraction family):

- Helpers take ``agent`` first and reach every collaborator through
  ``agent.<attr>`` so the test monkeypatch seams keep working.
- Async helpers are generators that ``yield`` SSE-bound event dicts; they
  cannot ``return`` a value, so the (possibly rebuilt) graph is surfaced
  through a caller-supplied ``graph_sink`` list (the
  ``compact_with_progress`` result-sink precedent).
- ``_create_human_message`` is imported function-locally from ``.agent``
  to keep the import graph acyclic (documented sibling convention).
"""

from __future__ import annotations

import logging
import threading
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    List,
    Optional,
    Tuple,
)

from .agent_compaction import COMPACTING_MESSAGE
from .agent_streaming import drive_with_fanout
from .pending_prompt_queue import PendingPrompt, queued_prompt_header

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from .agent_streaming import GraphStreamProcessor

logger = logging.getLogger(__name__)


def run_tool_reload_loop_sync(
    agent: "NymeriaAgent",
    *,
    graph: Any,
    thread_id: str,
    user_id: str,
    config: Dict[str, Any],
    context_label: str,
) -> Tuple[Any, Optional[List[Any]]]:
    """Sync in-turn tool-reload loop (the ``chat()`` flavor).

    If ``tool_manage(action="enable")`` flagged a genuinely new tool during
    the previous drive, rebuild a fresh graph with it bound and continue via
    an internal resume message, up to ``MAX_TOOL_RELOADS_PER_TURN`` times.
    ``context_label`` is "tool reload" (first pass) or "queued prompt tool
    reload" (drain inject), preserving the original log wording per site.

    Returns ``(graph, messages)`` where ``messages`` is the message list of
    the last resume drive, or ``None`` when no reload ran (callers must gate
    on ``is not None``: an empty list is a valid update).
    """
    messages: Optional[List[Any]] = None
    reload_count = 0
    while reload_count < agent.MAX_TOOL_RELOADS_PER_TURN:
        reload_info = agent._pending_tool_reload.pop(thread_id, None)
        if not reload_info:
            break
        reload_count += 1
        agent._turn_reload_count[thread_id] = reload_count
        new_tools = reload_info.get("new_tools", [])
        logger.info(
            f"[CHAT] Thread {thread_id}: {context_label} #{reload_count}, "
            f"{len(new_tools)} new tool(s): {', '.join(new_tools)}"
        )
        agent.invalidate_thread_config_cache(thread_id)
        reload_graph = agent._get_graph_for_user(user_id, thread_id=thread_id)
        resume_msg = agent._create_tool_reload_resume_message(reload_info)
        result = reload_graph.invoke({"messages": [resume_msg]}, config=config)
        messages = result.get("messages", [])
        graph = reload_graph
    return graph, messages


async def run_tool_reload_loop(
    agent: "NymeriaAgent",
    *,
    stream_processor: "GraphStreamProcessor",
    thread_id: str,
    user_id: str,
    abort_event: threading.Event,
    pending_batch: Optional[List[PendingPrompt]],
    context_label: str,
    graph_sink: List[Any],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Async in-turn tool-reload loop (the ``astream()`` flavor).

    Yields a ``tool_reload`` event per reload, then the resumed stream's
    events. When ``pending_batch`` is set (drain-inject site) every yielded
    event is also mirrored into each prompt's fanout mailbox so queued
    SSE consumers see the reload and the resumed stream; with no batch the
    fanout list is empty and the drive is equivalent to a plain
    ``stream_processor.drive``. Each rebuilt graph is appended to
    ``graph_sink`` (callers adopt ``graph_sink[-1]`` when non-empty).

    The reload's ``source`` field stays local to this helper; the caller's
    turn-source variable is deliberately not touched (historically the
    first-pass copy rebound it, corrupting ``holder_kind`` for DONE hooks).
    """
    reload_count = 0
    while reload_count < agent.MAX_TOOL_RELOADS_PER_TURN:
        if abort_event.is_set():
            break
        reload_info = agent._pending_tool_reload.pop(thread_id, None)
        if not reload_info:
            break
        reload_count += 1
        agent._turn_reload_count[thread_id] = reload_count
        new_tools = reload_info.get("new_tools", [])
        ttl_key = reload_info.get("ttl", "2h")
        ttl_seconds = reload_info.get("ttl_seconds")
        reload_source = reload_info.get("source") or "tool_search"
        skill_name = reload_info.get("skill_name")
        reason = reload_info.get("reason")

        logger.info(
            f"[ASTREAM] Thread {thread_id}: {context_label} #{reload_count}, "
            f"{len(new_tools)} new tool(s): {', '.join(new_tools)} (ttl={ttl_key})"
        )
        reload_evt = {
            "type": "tool_reload",
            "tools": new_tools,
            "ttl": ttl_key,
            "ttl_seconds": ttl_seconds,
            "source": reload_source,
            "skill_name": skill_name,
            "reason": reason,
        }
        yield reload_evt
        if pending_batch:
            for p in pending_batch:
                if p.fanout_mailbox is not None:
                    p.fanout_mailbox.put(reload_evt)

        # Build a fresh graph. invalidate_thread_config_cache was already
        # called by the enable tool, but we invalidate again defensively in
        # case something else cached in between.
        agent.invalidate_thread_config_cache(thread_id)
        reload_graph = agent._get_async_graph_for_user(user_id, thread_id=thread_id)
        resume_msg = agent._create_tool_reload_resume_message(reload_info)
        async for evt in drive_with_fanout(
            stream_processor,
            reload_graph,
            {"messages": [resume_msg]},
            pending_batch or [],
        ):
            yield evt
        graph_sink.append(reload_graph)


def run_subturn_compact_loop_sync(
    agent: "NymeriaAgent",
    *,
    graph: Any,
    thread_id: str,
    user_id: str,
    config: Dict[str, Any],
) -> Optional[List[Any]]:
    """Sync sub-turn auto-compaction halt loop (the ``chat()`` flavor).

    ``route_after_tools`` flagged that the running context crossed the
    trigger mid-loop; compact and re-invoke ``{"messages": []}`` to
    continue on the SAME graph. Capped per turn by
    ``should_halt_for_subturn_compaction``. Returns the last re-drive's
    message list, or ``None`` when no compaction ran (gate on
    ``is not None``).
    """
    messages: Optional[List[Any]] = None
    while thread_id in agent._subturn_compact_requested:
        agent._subturn_compact_requested.discard(thread_id)
        compact_result = agent._compaction._do_compact_sync(thread_id, user_id)
        if not (compact_result and compact_result.get("success")):
            logger.warning(
                f"[CHAT] Thread {thread_id}: sub-turn compaction "
                f"skipped/failed: "
                f"{compact_result.get('reason') if compact_result else 'none'}"
            )
            break
        agent._compactions_this_turn[thread_id] = (
            agent._compactions_this_turn.get(thread_id, 0) + 1
        )
        result = graph.invoke({"messages": []}, config=config)
        messages = result.get("messages", [])
    agent._subturn_compact_requested.discard(thread_id)
    return messages


async def run_subturn_compact_loop(
    agent: "NymeriaAgent",
    *,
    stream_processor: "GraphStreamProcessor",
    thread_id: str,
    user_id: str,
    abort_event: threading.Event,
    graph_sink: List[Any],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Async sub-turn auto-compaction halt loop (the ``astream()`` flavor).

    route_after_tools flagged that the running context crossed the
    auto-compact trigger mid-loop (the agent had a pending LLM call, it was
    NOT done). Compact, then re-drive with ``{"messages": []}`` on a FRESH
    graph so the agent continues from the reloaded memory. The continuation
    may cross the trigger again; the loop repeats until it doesn't, capped
    by ``should_halt_for_subturn_compaction`` (MAX_COMPACTIONS_PER_TURN).
    Each resume graph is appended to ``graph_sink``.
    """
    while (
        thread_id in agent._subturn_compact_requested
        and not abort_event.is_set()
    ):
        agent._subturn_compact_requested.discard(thread_id)
        yield {"type": "compacting", "message": COMPACTING_MESSAGE}
        compact_result = await agent._do_auto_compact(thread_id, user_id)
        if not (compact_result and compact_result.get("success")):
            logger.warning(
                f"[ASTREAM] Thread {thread_id}: sub-turn compaction "
                f"skipped/failed: "
                f"{compact_result.get('reason') if compact_result else 'none'}"
            )
            break
        agent._compactions_this_turn[thread_id] = (
            agent._compactions_this_turn.get(thread_id, 0) + 1
        )
        yield {
            "type": "compacted",
            "messages_removed": compact_result.get("messages_removed", 0),
            "auto_resumed": True,
            "summary": compact_result.get("summary"),
            "subturn": True,
        }
        resume_graph = agent._get_async_graph_for_user(user_id, thread_id=thread_id)
        async for evt in stream_processor.drive(resume_graph, {"messages": []}):
            yield evt
        graph_sink.append(resume_graph)
    agent._subturn_compact_requested.discard(thread_id)


def build_queued_prompt_messages(
    pending_batch: List[PendingPrompt],
    *,
    agent: Any = None,
    thread_id: Optional[str] = None,
) -> List[Any]:
    """Build one HumanMessage per drained prompt.

    Per-prompt visibility/history semantics survive the absorption:
    autonomous prompts stay ``internal=True`` (filtered from user-facing
    history); user prompts stay visible. A ``system`` prompt (the mid-turn
    tool-expiry notice, nodes._enqueue_tool_expiry_notice) is internal
    under its own ``internal_type`` so history renders it as a typed
    ``tool_expiry_notice`` card rather than a wakeup (which the
    show_autonomous_prompts toggle would hide, sub-turn included).

    Absorb is where the notice COMMITS: with ``agent`` and ``thread_id`` the
    system prompt's text is re-rendered from the still-un-notified records
    and those records flip to notified (``consume_tool_expiry_notice``), so a
    prompt dropped before this point (stop, abort, inject failure) is simply
    carried by the next prompt's prefix instead. A system prompt whose
    records are already delivered, or whose commit save failed, is skipped:
    the next prompt covers it. Without an agent the queued text is used as
    is (tests, and callers that only shape messages).
    """
    # Lazy: avoid circular import at module load (sibling convention).
    from .agent import _create_human_message

    messages: List[Any] = []
    for p in pending_batch:
        if p.source == "system":
            text = p.message
            if agent is not None and thread_id:
                from .agent_tools import consume_tool_expiry_notice

                text = consume_tool_expiry_notice(agent, thread_id, commit=True)
                if not text:
                    logger.info(
                        "Thread %s: queued tool expiry notice skipped at absorb "
                        "(already delivered, or its commit save failed)",
                        thread_id,
                    )
                    continue
            msg = _create_human_message(
                f"{queued_prompt_header(p)}\n\n{text}",
                internal=True,
                internal_type="tool_expiry_notice",
            )
            msg.additional_kwargs["tool_expiry_notice"] = text
        else:
            msg = _create_human_message(
                f"{queued_prompt_header(p)}\n\n{p.message}",
                internal=p.is_autonomous,
                internal_type="autonomous_wakeup" if p.is_autonomous else None,
            )
        messages.append(msg)
    return messages
