"""Activity feed tool for the Smart Watchdog.

Returns a structured summary of all activity across threads since a given
time window. Designed to give the watchdog full situational awareness in a
single tool call, cutting noise and grouping by thread.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Dict, List

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.activity_log import ActivityEntry, ActivityType, get_activity_log
from .utils import get_user_id

logger = logging.getLogger(__name__)

_USER_ACTIVITY_TYPES = {ActivityType.USER_MESSAGE}
_TODO_TYPES = {
    ActivityType.TODO_ADDED,
    ActivityType.TODO_UPDATED,
    ActivityType.TODO_COMPLETED,
    ActivityType.TODO_DELETED,
}
_TASK_TYPES = {
    ActivityType.SELF_INVOKE,
    ActivityType.TASK_COMPLETED,
    ActivityType.TASK_FAILED,
}
_NOTIFICATION_TYPES = {ActivityType.NOTIFICATION_SENT}


def _format_time(dt: datetime) -> str:
    """Format a datetime as short local time."""
    return dt.strftime("%H:%M")


def _build_thread_summary(entries: List[ActivityEntry]) -> str:
    """Build a one-thread summary block from its activity entries."""
    user_msgs = [e for e in entries if e.type in _USER_ACTIVITY_TYPES]
    tasks = [e for e in entries if e.type in _TASK_TYPES]
    todo_events = [e for e in entries if e.type in _TODO_TYPES]
    notifications = [e for e in entries if e.type in _NOTIFICATION_TYPES]
    other = [
        e for e in entries
        if e.type not in _USER_ACTIVITY_TYPES | _TASK_TYPES | _TODO_TYPES | _NOTIFICATION_TYPES
    ]

    lines: List[str] = []

    if user_msgs:
        last = _format_time(user_msgs[-1].timestamp)
        lines.append(f"  User messages: {len(user_msgs)} (last: {last})")
        for e in user_msgs:
            lines.append(f"    [{_format_time(e.timestamp)}] {e.message[:100]}")

    if tasks:
        completed = [e for e in tasks if e.type == ActivityType.TASK_COMPLETED]
        failed = [e for e in tasks if e.type == ActivityType.TASK_FAILED]
        invoked = [e for e in tasks if e.type == ActivityType.SELF_INVOKE]
        parts = []
        if invoked:
            parts.append(f"{len(invoked)} invoked")
        if completed:
            parts.append(f"{len(completed)} completed")
        if failed:
            parts.append(f"{len(failed)} failed")
        lines.append(f"  Autonomous tasks: {', '.join(parts)}")
        for e in completed + failed:
            lines.append(f"    - {e.message[:80]}")

    if todo_events:
        added = [e for e in todo_events if e.type == ActivityType.TODO_ADDED]
        completed = [e for e in todo_events if e.type == ActivityType.TODO_COMPLETED]
        updated = [e for e in todo_events if e.type == ActivityType.TODO_UPDATED]
        deleted = [e for e in todo_events if e.type == ActivityType.TODO_DELETED]
        parts = []
        if added:
            parts.append(f"{len(added)} added")
        if completed:
            parts.append(f"{len(completed)} completed")
        if updated:
            parts.append(f"{len(updated)} updated")
        if deleted:
            parts.append(f"{len(deleted)} deleted")
        lines.append(f"  TODOs: {', '.join(parts)}")

    if notifications:
        platforms = []
        for e in notifications:
            if e.metadata and "platforms" in e.metadata:
                platforms.extend(e.metadata["platforms"])
        if platforms:
            lines.append(f"  Notifications sent: {', '.join(platforms)}")
        else:
            lines.append(f"  Notifications sent: {len(notifications)}")

    if other:
        for e in other:
            lines.append(f"  {e.type.value}: {e.message[:80]}")

    return "\n".join(lines)


@tool
def activity_feed(
    minutes_ago: int = 10,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Get a structured activity summary across all threads.

    Returns a concise report of what has happened since `minutes_ago`,
    grouped by thread: user messages, autonomous tasks, TODO changes,
    and notifications. Use this to understand current system state
    before deciding whether to dispatch work.

    Args:
        minutes_ago: Look-back window in minutes (default 10)
    """
    user_id = get_user_id(config)
    since = datetime.utcnow() - timedelta(minutes=max(1, minutes_ago))

    activity_log = get_activity_log()
    entries = activity_log.get_entries(
        user_id=user_id,
        limit=500,
        since=since,
    )

    if not entries:
        now = _format_time(datetime.utcnow())
        return f"No activity in the last {minutes_ago} minutes (checked at {now})."

    # Group by thread
    by_thread: Dict[str, List[ActivityEntry]] = defaultdict(list)
    no_thread: List[ActivityEntry] = []
    for entry in entries:
        if entry.thread_id:
            by_thread[entry.thread_id].append(entry)
        else:
            no_thread.append(entry)

    # Build output
    now_str = _format_time(datetime.utcnow())
    start_str = _format_time(since)
    header = f"Activity Feed: {start_str} - {now_str} ({minutes_ago} min)\n"

    sections: List[str] = [header]

    for thread_id in sorted(by_thread.keys()):
        thread_entries = by_thread[thread_id]
        summary = _build_thread_summary(thread_entries)
        sections.append(f'Thread "{thread_id}":\n{summary}')

    if no_thread:
        summary = _build_thread_summary(no_thread)
        sections.append(f"Global (no thread):\n{summary}")

    return "\n\n".join(sections)


ACTIVITY_FEED_TOOLS = [activity_feed]
