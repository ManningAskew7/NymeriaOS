"""Prompt management for Nymeria agent.

Contains mode-specific rules and system prompt building utilities.
"""

from datetime import datetime


# Rules for interactive mode (responding to user messages)
# Kept minimal — soul.md carries all behavioral guidance
INTERACTIVE_MODE_RULES = ''

# Rules for autonomous mode (self_invoke scheduled tasks)
# Kept minimal — soul.md carries all behavioral guidance
AUTONOMOUS_MODE_RULES = ''


def get_time_context(is_autonomous: bool = False, trigger_override: str = None) -> str:
    """
    Get current time context in the user's configured timezone.

    Args:
        is_autonomous: If True, this is an autonomous scheduled wake-up
        trigger_override: If provided, use this as the trigger label instead of
                          the default "User Message" / "Scheduled TODO"

    Returns:
        Formatted time context string to prepend to messages
    """
    from .time_utils import get_user_tz

    user_tz = get_user_tz()
    now = datetime.now(user_tz)

    if trigger_override:
        trigger = trigger_override
    elif is_autonomous:
        trigger = "Scheduled TODO"
    else:
        trigger = "User Message"

    return (
        f"[Time: {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({user_tz.key})]\n"
        f"[Trigger: {trigger}]"
    )


def get_full_context_metadata(
    is_autonomous: bool = False,
    rag_context: list = None,
    trigger_override: str = None,
) -> str:
    """
    Build full hidden metadata including time, trigger, and RAG context.

    This extends the basic time context with relevant past conversation
    context retrieved via RAG (Retrieval Augmented Generation).

    Args:
        is_autonomous: If True, this is an autonomous scheduled wake-up
        rag_context: Optional list of ChunkResult objects from RAG search
        trigger_override: If provided, use this as the trigger label

    Returns:
        Full context metadata string to prepend to messages
    """
    from typing import List
    parts = []

    # Existing time context
    parts.append(get_time_context(is_autonomous, trigger_override=trigger_override))

    # RAG context (if enabled and results found)
    if rag_context:
        parts.append("\n[Relevant Context from Previous Conversations:]")
        for chunk in rag_context:
            # Format: brief summary with source hint
            chunk_type = getattr(chunk, 'chunk_type', 'unknown')
            content = getattr(chunk, 'content', str(chunk))

            # Truncate long chunks for context injection
            if len(content) > 300:
                content = content[:297] + "..."

            # Add source type prefix for clarity
            type_prefix = {
                'conversation': '💬',
                'memory': '🧠',
                'todo': '✅',
            }.get(chunk_type, '📝')

            parts.append(f"- {type_prefix} ({chunk_type}) {content}")

    return "\n".join(parts)
