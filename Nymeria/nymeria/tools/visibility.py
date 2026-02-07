"""Visibility control tools for autonomous responses.

Provides tools for the LLM to control how its responses are displayed:
- mute_response: Move response to activity log instead of chat
- send_notification: (Future) Send push notification to user

Muted messages stay in the LangGraph checkpoint (model retains full context)
but are hidden from the UI when conversation history is loaded.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Annotated, Optional, Set

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id

logger = logging.getLogger(__name__)

# Thread-local storage for mute flag (set during tool execution, read after streaming)
# Maps thread_id -> {"muted": bool, "reason": str}
_mute_flags: dict[str, dict] = {}

# --- Persistent muted turn store ---
# Stores the HumanMessage ID that starts each muted turn. On history load,
# get_conversation_history() uses skip_until_next_human to hide the entire
# turn (prompt + AI responses + tool calls) from the UI while keeping
# everything in the checkpoint for the model.

_muted_store_lock = threading.Lock()
_MUTED_TURNS_FILE = "muted_turns.json"


def _get_muted_store_path(data_dir: Path) -> Path:
    return data_dir / _MUTED_TURNS_FILE


def _load_muted_store(data_dir: Path) -> dict:
    path = _get_muted_store_path(data_dir)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load muted turns store: {e}")
    return {}


def _save_muted_store(data_dir: Path, store: dict) -> None:
    path = _get_muted_store_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store))
    except OSError as e:
        logger.error(f"Failed to save muted turns store: {e}")


def persist_muted_turn(data_dir: Path, thread_id: str, human_message_id: str) -> None:
    """Persist the HumanMessage ID that starts a muted turn."""
    with _muted_store_lock:
        store = _load_muted_store(data_dir)
        turns = store.get(thread_id, [])
        turns.append(human_message_id)
        store[thread_id] = turns
        _save_muted_store(data_dir, store)
    logger.info(f"Persisted muted turn for thread {thread_id} (msg_id={human_message_id})")


def get_muted_turn_ids(data_dir: Path, thread_id: str) -> Set[str]:
    """Get the set of HumanMessage IDs that start muted turns (for history filtering)."""
    with _muted_store_lock:
        store = _load_muted_store(data_dir)
    return set(store.get(thread_id, []))


def clear_muted_turns_for_thread(data_dir: Path, thread_id: str) -> None:
    """Clear all muted turn records for a thread."""
    with _muted_store_lock:
        store = _load_muted_store(data_dir)
        if thread_id in store:
            del store[thread_id]
            _save_muted_store(data_dir, store)


def set_mute_flag(thread_id: str, reason: str = "") -> None:
    """Set the mute flag for the current thread."""
    _mute_flags[thread_id] = {"muted": True, "reason": reason}
    logger.debug(f"Mute flag set for thread {thread_id}: {reason}")


def get_and_clear_mute_flag(thread_id: str) -> dict | None:
    """
    Get and clear the mute flag for a thread.

    Returns None if no mute flag was set.
    The flag is cleared after retrieval to prevent stale state.
    """
    return _mute_flags.pop(thread_id, None)


def clear_mute_flag(thread_id: str) -> None:
    """Clear the mute flag for a thread without returning it."""
    _mute_flags.pop(thread_id, None)


@tool
def mute_response(
    reason: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Move response to activity log instead of chat. Use for routine/uneventful updates.

    Args:
        reason: Brief explanation (e.g., "routine check, no changes")
    """
    thread_id = get_thread_id(config)
    logger.info(f"mute_response called for thread {thread_id}: {reason}")
    set_mute_flag(thread_id, reason)
    return f"Response will be logged to activity feed only. Reason: {reason or 'none provided'}"


# Future: send_notification tool
# @tool
# def send_notification(
#     summary: str,
#     urgency: str = "normal",
#     *,
#     config: Annotated[RunnableConfig, InjectedToolArg],
# ) -> str:
#     """
#     Send a push notification to the user's mobile device.
#
#     Use for time-sensitive information or important alerts.
#
#     Args:
#         summary: Notification text (max 100 chars, shown on lock screen)
#         urgency: "normal" or "urgent" (urgent uses sound/vibration)
#
#     Returns:
#         Confirmation that the notification was sent
#     """
#     # TODO: Implement when mobile app is ready
#     pass
