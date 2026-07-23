"""Build the memory-load exchange seeded into a thread's conversation history.

Memory (the global profile + the per-thread notepad, plus the shared team
registry for teamed threads) is surfaced to the agent as an authentic
``memory_read`` tool exchange living in the conversation tail rather than in
the system prompt. The tail is append-only and cache-stable, whereas the
front-positioned system prompt busts the provider prompt cache whenever memory
changes. Both the fresh-thread init seed (``agent.py``) and the redesigned
compaction turn (``agent_compaction.py``) reuse the builder here so the
in-context shape is identical.

The exchange is stored in LangChain canonical form (``AIMessage.tool_calls`` plus
matching ``ToolMessage``s) and is re-serialized to each provider's wire format at
request time, so it is safe across Anthropic-direct, OpenAI/Codex via CLIProxy,
and OpenRouter. The only hard rule is that every ``tool_call`` id has a matching
``ToolMessage.tool_call_id`` (no dangling calls) and that the exchange never ends
on an open tool-calls ``AIMessage``.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# internal_type tags for the seeded/retained openers. Both are marked
# ``internal=True`` so the frontend display path hides them by default; the LLM
# still receives them from raw checkpoint state.
MEMORY_INIT_TYPE = "memory_init"
MEMORY_SEED_MARKER_TYPE = "memory_seed_marker"

# Neutral, instruction-free opener for the fresh-thread init seed.
MEMORY_INIT_OPENER = "[Session start] Loading your persistent memory for this conversation."
MEMORY_INIT_TRAILING = "Memory loaded."

# Resume opener for the post-compaction retained tail. Carries the summary inline
# and frames the (synthetic) memory reload, then the task continuation. The tail
# deliberately has NO trailing assistant message so re-invoking the graph with
# {"messages": []} resumes the agent from the read-back tool results ("hit play").
MEMORY_RESUME_OPENER_TEMPLATE = (
    "[Session resume] You reached your context window threshold, so the earlier "
    "conversation was summarized:\n\n{summary}\n\nYour persistent memory is "
    "reloaded below. Continue where you left off if there is pending work, and "
    "re-read any key files referenced in the summary as needed."
)

# Returned when a read raises so seeding/compaction never aborts a turn.
_MEMORY_READ_FALLBACK = "[empty]"


def _read_memory(scope: str, user_id: str, thread_id: str) -> str:
    """Invoke the real ``memory_read`` tool and return its string output.

    Returns a safe ``"[empty]"`` fallback on any failure so a read error never
    aborts the turn or compaction that is seeding the exchange.
    """
    try:
        from ..tools.memory import memory_read

        return memory_read.invoke(
            {"scope": scope},
            config={"configurable": {"thread_id": thread_id, "user_id": user_id}},
        )
    except Exception as exc:  # never let a read failure abort the turn
        logger.warning(
            "memory seed: %s read failed (thread=%s user=%s): %s",
            scope,
            thread_id,
            user_id,
            exc,
        )
        return _MEMORY_READ_FALLBACK


def read_global_memory(user_id: str, thread_id: str) -> str:
    """Full global-profile listing as the agent would see it (or a sentinel)."""
    return _read_memory("global", user_id, thread_id)


def read_thread_memory(user_id: str, thread_id: str) -> str:
    """Full thread notepad as the agent would see it (or ``"[empty]"``)."""
    return _read_memory("thread", user_id, thread_id)


def read_team_memory(user_id: str, thread_id: str) -> Optional[str]:
    """Team memory listing iff the thread is teamed at seed time, else None.

    The team read (identity header plus shared entries) joins the seed only
    for teamed threads; unteamed threads carry no third read at all, so the
    exchange shape stays byte-stable for them (backlog #100 phase 3). The
    teamed probe rides ``tools.memory.acting_team_id`` (the same resolution
    the tool uses, including the dream-shadow parent retarget) and degrades
    to None on any failure, never aborting a seed or compaction.
    """
    try:
        from ..tools.memory import acting_team_id

        if not acting_team_id(
            {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        ):
            return None
    except Exception:  # never let the probe abort the turn
        logger.debug(
            "memory seed: team probe failed (thread=%s user=%s)",
            thread_id,
            user_id,
            exc_info=True,
        )
        return None
    return _read_memory("team", user_id, thread_id)


def build_memory_exchange(
    *,
    opener_internal_type: str,
    opener_text: str,
    global_text: str,
    thread_text: str,
    team_text: Optional[str] = None,
    trailing_text: Optional[str] = None,
) -> List[BaseMessage]:
    """Build a canonical, provider-neutral ``memory_read`` exchange.

    Shape: internal ``HumanMessage`` opener -> ``AIMessage`` with two
    ``memory_read`` tool calls (global + thread; a third with scope="team"
    when ``team_text`` is given, i.e. the thread is teamed at seed time) ->
    matching ``ToolMessage``s carrying the real content -> optional trailing
    ``AIMessage``. Every message gets a fresh uuid id, ``AIMessage.content``
    is a plain empty string (no thinking blocks, safe past the Anthropic
    sanitizer), and tool-call ids are paired with their ``ToolMessage``s so
    the sequence is never dangling.
    """
    global_call_id = f"mem_read_global_{_uuid.uuid4().hex[:12]}"
    thread_call_id = f"mem_read_thread_{_uuid.uuid4().hex[:12]}"
    team_call_id = f"mem_read_team_{_uuid.uuid4().hex[:12]}"

    opener = HumanMessage(
        content=opener_text,
        additional_kwargs={"internal": True, "internal_type": opener_internal_type},
        id=str(_uuid.uuid4()),
    )
    tool_calls = [
        {
            "id": global_call_id,
            "name": "memory_read",
            "args": {"scope": "global"},
            "type": "tool_call",
        },
        {
            "id": thread_call_id,
            "name": "memory_read",
            "args": {"scope": "thread"},
            "type": "tool_call",
        },
    ]
    if team_text is not None:
        tool_calls.append(
            {
                "id": team_call_id,
                "name": "memory_read",
                "args": {"scope": "team"},
                "type": "tool_call",
            }
        )
    ai_calls = AIMessage(
        content="",
        tool_calls=tool_calls,
        id=str(_uuid.uuid4()),
    )
    tool_global = ToolMessage(
        content=global_text,
        tool_call_id=global_call_id,
        name="memory_read",
        id=str(_uuid.uuid4()),
    )
    tool_thread = ToolMessage(
        content=thread_text,
        tool_call_id=thread_call_id,
        name="memory_read",
        id=str(_uuid.uuid4()),
    )

    messages: List[BaseMessage] = [opener, ai_calls, tool_global, tool_thread]
    if team_text is not None:
        messages.append(
            ToolMessage(
                content=team_text,
                tool_call_id=team_call_id,
                name="memory_read",
                id=str(_uuid.uuid4()),
            )
        )
    if trailing_text is not None:
        messages.append(AIMessage(content=trailing_text, id=str(_uuid.uuid4())))
    return messages


def build_init_seed_exchange(user_id: str, thread_id: str) -> List[BaseMessage]:
    """Build the fresh-thread init seed exchange with authentic memory content.

    Teamed threads (the common case after spawn inheritance) get the team
    identity + shared-memory read from turn one; threads that join a team
    mid-thread are NOT reseeded (that would bust the warm prompt cache) and
    see team memory on their next compaction or explicit read.
    """
    return build_memory_exchange(
        opener_internal_type=MEMORY_INIT_TYPE,
        opener_text=MEMORY_INIT_OPENER,
        global_text=read_global_memory(user_id, thread_id),
        thread_text=read_thread_memory(user_id, thread_id),
        team_text=read_team_memory(user_id, thread_id),
        trailing_text=MEMORY_INIT_TRAILING,
    )


def build_resume_compaction_tail(
    *, user_id: str, thread_id: str, summary: str
) -> List[BaseMessage]:
    """Build the post-compaction retained tail (resume framing, no trailer).

    Shape: ``[resume opener (Human, internal_type=memory_seed_marker), AIMessage
    with memory_read global+thread tool_calls, ToolMessage global, ToolMessage
    thread]``. The memory content is read fresh (post-edit) so it reflects any
    memory the agent wrote during its compaction turn. There is intentionally no
    trailing assistant message: re-invoking the graph with ``{"messages": []}``
    resumes the agent from these tool results. The caller is responsible for
    stamping the opener's ``additional_kwargs`` (summary/messages_removed/
    auto_resumed/timestamp) for the frontend compaction notice.
    """
    return build_memory_exchange(
        opener_internal_type=MEMORY_SEED_MARKER_TYPE,
        opener_text=MEMORY_RESUME_OPENER_TEMPLATE.format(summary=summary),
        global_text=read_global_memory(user_id, thread_id),
        thread_text=read_thread_memory(user_id, thread_id),
        team_text=read_team_memory(user_id, thread_id),
        trailing_text=None,
    )
