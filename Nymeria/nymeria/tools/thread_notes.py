"""Per-thread notepad helpers used by the unified memory tools.

Storage layer for the thread notepad: a per-thread markdown file that survives
context compaction and is re-injected after compaction so critical thread
context is never lost. The agent-facing surface (``memory_add``, ``memory_edit``,
``memory_read``) lives in ``nymeria/tools/memory.py`` and calls the helpers here
when ``scope="thread"`` (the agent's own thread, or a dream's parent).

These helpers do NO ownership check. A surface keyed by a caller-supplied
thread id must go through an access-checked door instead: the REST routes
``GET``/``PUT /threads/{id}/notepad``, or the command layer's
``get_thread_notepad`` / ``write_thread_notepad`` client methods (``/notepad``
imported these helpers directly until 2026-09-30 and so read, rewrote and
cleared other users' notepads).

Storage: data/thread_notes/{thread_id}.md
"""

import logging
from pathlib import Path
from typing import Optional

from ..core.keyed_locks import KeyedRLockMap
from ..core.memory_limits import (
    get_effective_thread_memory_char_limit,
    validate_text_memory_write,
)

logger = logging.getLogger(__name__)

# Legacy compatibility constant. Current writes use MEMORY_CHAR_LIMIT plus any
# per-thread override, both measured in characters.
MAX_NOTEPAD_SIZE = 50 * 1024

# From this share of the cap a successful write says the notepad is nearly full
# and names both ways forward, so the cap is plannable instead of discovered by
# a failed write mid-task (backlog #101 entry 24).
NEARLY_FULL_RATIO = 0.8
RAISE_THREAD_LIMIT = "`/memory limit <chars> thread`"


def notepad_occupancy(size: int, limit: int, *, offer_raise: bool = True) -> str:
    """``N / L chars``, plus the ways forward once the notepad is nearly full
    (or already past a cap that was lowered under it). ``offer_raise=False``
    names consolidation only, for a writer that cannot raise the limit."""
    text = f"{size} / {limit} chars"
    if size < limit * NEARLY_FULL_RATIO:
        return text
    # Past the cap only a shrinking write is accepted (validate_text_memory_write).
    state = "over the cap, so only edits that shrink it succeed" if size > limit else "nearly full"
    text += f"; {state}: consolidate older notes"
    if offer_raise:
        text += f", or raise this thread's limit with {RAISE_THREAD_LIMIT}"
    return text


def _thread_full_error(limit_error: str, *, offer_raise: bool = True) -> str:
    """The shared memory-full error plus the remedy only a thread has: its own
    limit (the notes may be worth keeping; shrinking is not the only move).

    Surface-neutral on purpose: the same string reaches the agent, a human's
    ``/notepad write`` and the desktop Memory tab, so it names the command
    and not who runs it (the tool description tells the agent how)."""
    if not offer_raise:
        return limit_error
    return (
        f"{limit_error} If these notes are worth keeping, raise this thread's "
        f"limit instead: {RAISE_THREAD_LIMIT}."
    )


# Per-thread locks so the read-modify-write in write_notepad/edit_notepad can't
# lose data when two writers hit the same notepad at once. This is reachable now
# that a dream shadow writes the PARENT's notepad while the parent thread may be
# mid-turn (the dream's memory tools resolve to the parent). Reentrant, so the
# write/edit paths can call delete_notepad while holding the lock.
_notepad_locks = KeyedRLockMap()

# Lazily resolved data directory
_notes_dir: Optional[Path] = None


def _get_notes_dir() -> Path:
    """Get or create the thread_notes directory."""
    global _notes_dir
    if _notes_dir is None:
        from ..config import get_settings
        settings = get_settings()
        _notes_dir = settings.data_dir / "thread_notes"
    _notes_dir.mkdir(parents=True, exist_ok=True)
    return _notes_dir


def _notepad_path(thread_id: str) -> Path:
    """Get the notepad file path for a thread."""
    safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_")
    if not safe_id:
        safe_id = "default"
    return _get_notes_dir() / f"{safe_id}.md"


def read_notepad(thread_id: str) -> Optional[str]:
    """Read notepad content for a thread. Returns None if empty/missing."""
    with _notepad_locks.get(thread_id):
        path = _notepad_path(thread_id)
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8").strip()
        return content if content else None


def delete_notepad(thread_id: str) -> bool:
    """Delete notepad file for a thread. Returns True if file existed."""
    with _notepad_locks.get(thread_id):
        path = _notepad_path(thread_id)
        if path.exists():
            path.unlink()
            return True
        return False


def write_notepad(
    thread_id: str,
    content: str,
    mode: str = "append",
    *,
    char_limit: int | None = None,
    offer_raise: bool = True,
) -> str:
    """Write to a thread's notepad.

    Args:
        thread_id: Thread to write to.
        content: New text.
        mode: "append" adds to existing notes, "replace" overwrites entirely.
        offer_raise: name ``/memory limit`` as a remedy (False for a writer
            that cannot raise this notepad's limit, e.g. a dream shadow).

    Returns:
        Status string (mirrors the agent-facing tool result format).
    """
    if mode not in ("append", "replace"):
        return "[Error]: mode must be 'append' or 'replace'."

    with _notepad_locks.get(thread_id):
        path = _notepad_path(thread_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        if mode == "append":
            new_content = (
                (existing.rstrip() + "\n\n" + content) if existing else content
            )
        else:
            new_content = content

        if not new_content.strip():
            deleted = delete_notepad(thread_id)
            logger.info(f"Notepad cleared by empty write for thread {thread_id}")
            return (
                "[Saved]: Notepad is empty."
                if deleted
                else "[Info]: Notepad was already empty."
            )

        limit = char_limit or get_effective_thread_memory_char_limit(thread_id)
        limit_error = validate_text_memory_write(
            label="Thread memory",
            current_text=existing,
            proposed_text=new_content,
            limit=limit,
        )
        if limit_error:
            return _thread_full_error(limit_error, offer_raise=offer_raise)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")

        size = len(new_content)
        logger.info(f"Notepad written for thread {thread_id}: {size} chars ({mode})")
        return (
            f"[Saved]: Notepad updated ({notepad_occupancy(size, limit, offer_raise=offer_raise)}). "
            "This content will persist through compaction."
        )


def edit_notepad(
    thread_id: str,
    old_text: str,
    new_text: str = "",
    *,
    char_limit: int | None = None,
    offer_raise: bool = True,
) -> str:
    """Find/replace within a thread's notepad. Empty new_text deletes the matched text.

    Empty old_text operates on the whole notepad: a non-empty new_text rewrites
    it end-to-end, a blank new_text clears it. If the notepad becomes empty as a
    result, the underlying file is deleted.
    """
    with _notepad_locks.get(thread_id):
        path = _notepad_path(thread_id)

        # Empty old_text = operate on the WHOLE notepad: a one-call rewrite
        # (non-empty new_text) or clear (blank new_text). This is the deliberate,
        # explicit way to replace or clear the notepad now that memory_add only
        # appends; it avoids having to echo the entire notepad back as `find`.
        if old_text == "":
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            new_body = new_text
            while "\n\n\n" in new_body:
                new_body = new_body.replace("\n\n\n", "\n\n")
            new_body = new_body.strip()
            if not new_body:
                # Empty replace = clear the whole notepad.
                if delete_notepad(thread_id):
                    logger.info(f"Notepad cleared via memory_edit for thread {thread_id}")
                    return "[Saved]: Notepad cleared. Notepad is now empty."
                return "[Info]: Notepad is already empty."
            # Non-empty replace = set the whole notepad, creating it if absent so a
            # rewrite always lands (the dream loop uses this for consolidation).
            new_body = new_body + "\n"
            limit = char_limit or get_effective_thread_memory_char_limit(thread_id)
            limit_error = validate_text_memory_write(
                label="Thread memory",
                current_text=existing,
                proposed_text=new_body,
                limit=limit,
            )
            if limit_error:
                return _thread_full_error(limit_error, offer_raise=offer_raise)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_body, encoding="utf-8")
            size = len(new_body)
            logger.info(f"Notepad rewritten via memory_edit for thread {thread_id}: {size} chars")
            return f"[Saved]: Notepad rewritten ({notepad_occupancy(size, limit, offer_raise=offer_raise)})."

        if not path.exists():
            return "[Error]: Notepad is empty; nothing to edit."

        content = path.read_text(encoding="utf-8")

        if old_text not in content:
            return "[Error]: Could not find the specified text in notepad. Make sure it matches exactly."

        count = content.count(old_text)
        updated = content.replace(old_text, new_text, 1)

        while "\n\n\n" in updated:
            updated = updated.replace("\n\n\n", "\n\n")
        updated = updated.strip()

        if not updated:
            delete_notepad(thread_id)
            logger.info(f"Notepad edited for thread {thread_id}: removed all content")
            return "[Saved]: Text removed. Notepad is now empty."

        updated = updated + "\n"

        limit = char_limit or get_effective_thread_memory_char_limit(thread_id)
        limit_error = validate_text_memory_write(
            label="Thread memory",
            current_text=content,
            proposed_text=updated,
            limit=limit,
        )
        if limit_error:
            return _thread_full_error(limit_error, offer_raise=offer_raise)

        path.write_text(updated, encoding="utf-8")
        size = len(updated)

        action = "replaced" if new_text else "removed"
        extra = f" ({count} occurrences found, first one {action})" if count > 1 else ""
        logger.info(f"Notepad edited for thread {thread_id}: {action} text, {size} chars")
        return f"[Saved]: Text {action}{extra}. Notepad is now {notepad_occupancy(size, limit, offer_raise=offer_raise)}."
