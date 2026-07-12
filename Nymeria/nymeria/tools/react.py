"""``react``: post an emoji reaction to the chat-platform message behind a turn.

The outbound half of two-way bot reactions (backlog #45,
``docs/private/plans/emoji-reactions.md``). Chat-platform bots stamp every
dispatched turn with the originating platform message
(``ChatRequest.platform_origin`` -> ``core/bot_reactions.set_turn_origin``,
honored for admin/service-token callers only, and cleared at the start of
every turn without one); this tool reads that origin and publishes one
``reaction_request`` autonomous event, which the origin platform's bot
executes (Discord ``add_reaction``, Telegram ``setMessageReaction``). The
transport is the same event-bus path ``notification`` events ride, so it works
in both deployment shapes and stays best-effort fire-and-forget: the tool
result honestly says "queued", since delivery needs the platform's bot to be
running and able to reach the message.

``suppress_reply=true`` makes the reaction the turn's ONLY visible output: the
tool marks the per-thread suppress flag (the authority: read back by
``agent_results.tool_result_extra_events`` before it emits the
``reply_suppressed`` stream event, and by the chat routes for the terminal
``done``/``ChatResponse`` stamp) and appends ``REPLY_SUPPRESSED_MARKER`` to
its result text. Marker text from any other tool is inert without the flag.

``reaction_guidance_block`` renders the deferred-use guidance the chat routes
inject into reaction-triggered synthetic prompts: the tool description plus
its compact ``schema_render`` schema for a cache-safe ``tool_invoke`` call,
omitted (down to a one-line reminder) while the tool is actually bound, and
omitted entirely when the deferred gate would refuse the call (for example
``react`` in the thread's ``disabled_tools``). The authorities are
``agent._select_tools_for_graph`` and ``tool_invoke.deferred_gate_reason``
themselves, so neither rule can drift from what actually runs.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.bot_reactions import (
    REACTION_REQUEST_EVENT,
    REPLY_SUPPRESSED_MARKER,
    get_turn_origin,
    mark_reply_suppressed,
)
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

_MAX_EMOJI_CHARS = 64  # covers Discord custom-emoji forms like <:name:id>


def _publish_reaction_request(
    *,
    thread_id: str,
    user_id: str,
    platform: str,
    channel_id: str,
    message_id: str,
    emoji: str,
) -> None:
    """Seam: publish the outbound reaction event (monkeypatched in tests)."""
    from ..core.event_bus import publish_autonomous_event

    publish_autonomous_event(
        event_type=REACTION_REQUEST_EVENT,
        thread_id=thread_id,
        user_id=user_id,
        task_id="",
        data={
            "platform": platform,
            "channel_id": channel_id,
            "message_id": message_id,
            "emoji": emoji,
        },
    )


async def _react_impl(
    emoji: str,
    suppress_reply: bool,
    message_id: Optional[str],
    config: RunnableConfig,
) -> str:
    emoji = (emoji or "").strip()
    if not emoji:
        return "[react error] emoji is required (e.g. \"👍\")."
    if len(emoji) > _MAX_EMOJI_CHARS:
        return "[react error] emoji must be a single emoji, not text."

    thread_id = get_thread_id(config)
    user_id = get_user_id(config)

    origin = get_turn_origin(thread_id)
    if origin is None:
        return (
            "[react error] this thread has no chat-platform origin message. "
            "Reactions are only possible on turns that arrived from a chat "
            "platform bot (Discord, Telegram)."
        )

    target_message_id = (message_id or "").strip() or origin["message_id"]
    platform = origin["platform"]

    try:
        _publish_reaction_request(
            thread_id=thread_id,
            user_id=user_id,
            platform=platform,
            channel_id=origin["channel_id"],
            message_id=target_message_id,
            emoji=emoji,
        )
    except Exception as exc:  # noqa: BLE001 - surface failures to the model
        logger.warning("react: failed to publish reaction_request: %s", exc)
        return f"[react error] could not queue the reaction: {exc}"

    if suppress_reply:
        mark_reply_suppressed(thread_id)
        return (
            f"Queued a {emoji} reaction to the {platform} message (the "
            f"{platform} bot posts it best-effort). Reply suppression is "
            "active: end your turn now without writing any other text, so "
            "the user sees only the reaction. "
            f"{REPLY_SUPPRESSED_MARKER}"
        )
    return (
        f"Queued a {emoji} reaction to the {platform} message; the "
        f"{platform} bot posts it best-effort."
    )


@tool
async def react(
    emoji: str,
    suppress_reply: bool = False,
    message_id: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Post an emoji reaction to the chat-platform message behind this turn.

    Only works on turns driven by a chat-platform bot (Discord, Telegram):
    the reaction lands on the message that started the current turn (the
    user's message, or, on a reaction-triggered turn, the message the user
    reacted to). Delivery is asynchronous and best-effort: the request is
    queued for the platform's bot, which must be running to post it.

    With suppress_reply=true the reaction becomes your ENTIRE visible
    response: the bot will not post your reply text, so call this tool and end
    the turn without writing anything else. Use it when a lightweight
    acknowledgement (👍 to "thanks!") feels more natural than a message.

    Pick widely supported emoji. Telegram only allows its standard reaction
    set (examples: 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🎉 🙏 👌 😢 💯 ⚡ 🏆 🫡 😎); an
    unsupported emoji is silently dropped there. Discord accepts any standard
    unicode emoji.

    Args:
        emoji: The emoji to react with (a single unicode emoji, e.g. "👍").
        suppress_reply: When true, hide this turn's reply text so the user
            sees only the reaction. Write no other text after calling.
        message_id: Optional platform-native message id to react to instead
            of the turn's originating message.
    """
    return await _react_impl(emoji, suppress_reply, message_id, config)


def reaction_guidance_block(agent, user_id: str, thread_id: str) -> str:
    """Render the react-tool guidance appended to reaction-triggered prompts.

    Resolves the thread's CURRENT bound tool set through
    ``agent._select_tools_for_graph`` (the graph authority: defaults, enabled,
    temporary incl. kit TTLs, authoritative disabled_tools, and role gates all
    apply identically), so the omit-when-bound rule can never drift from what
    the graph actually binds.

    Unbound: the full deferred-use block (description line, compact
    ``schema_render`` schema, ``tool_invoke`` recipe, bind nudge). Bound: a
    one-line reminder with no schema, until the binding lapses. Refused by
    the deferred gate (``tool_invoke.deferred_gate_reason``, notably the
    thread's ``disabled_tools``): no block at all, since the nudged
    ``tool_invoke`` call would only be refused by the same gate. Fails open
    to the full block when resolution errors: a few redundant tokens beat
    stranding the model without the schema.
    """
    bound = False
    try:
        tools, _tc = agent._select_tools_for_graph(user_id, thread_id)
        bound = any(getattr(t, "name", None) == "react" for t in tools)
    except Exception:  # noqa: BLE001 - fail open to including the guidance
        logger.debug(
            "reaction_guidance_block: bound-state resolution failed", exc_info=True
        )

    if bound:
        return (
            "[React tool] The react tool is bound on this thread: you can "
            "call it directly to respond with an emoji reaction "
            "(suppress_reply=true makes the reaction your only visible "
            "response)."
        )

    try:
        from .tool_invoke import deferred_gate_reason
        from .utils import caller_role

        if deferred_gate_reason(
            agent, "react", user_id, thread_id, caller_role(user_id, agent=agent)
        ):
            # The deferred path would refuse (e.g. react is in the thread's
            # disabled_tools): advertising a tool_invoke recipe that the
            # same gate rejects would only waste a round trip.
            return ""
    except Exception:  # noqa: BLE001 - fail open to including the guidance
        logger.debug(
            "reaction_guidance_block: deferred-gate resolution failed",
            exc_info=True,
        )

    from .schema_render import render_tool_args_schema

    schema = render_tool_args_schema(react)
    schema_line = f"\nSchema: {schema}" if schema else ""
    return (
        "[React tool] You can respond with an emoji reaction instead of "
        "text: call tool_invoke with name=\"react\" and arguments matching "
        "this schema (no binding needed, cache-safe)."
        f"{schema_line}\n"
        "Set suppress_reply=true and write no other text to make the "
        "reaction your entire response. If this user reacts often, bind the "
        "tool first-class with tool_manage(action=\"enable\", "
        "tools=[\"react\"]) so it stays available."
    )


REACT_TOOLS = [react]
