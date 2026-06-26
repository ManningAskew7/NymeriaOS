"""Per-thread notepad helpers used by the unified memory tools.

Storage layer for the thread notepad — a per-thread markdown file that survives
context compaction and is re-injected after compaction so critical thread
context is never lost. The agent-facing surface (``memory_add``, ``memory_edit``,
``memory_read``) lives in ``nymeria/tools/memory.py`` and calls the helpers here
when ``scope="thread"``. User-facing bot slash commands (``/notepad_read`` in
Discord/Telegram, slash dispatcher) also import these helpers directly.

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
) -> str:
    """Write to a thread's notepad.

    Args:
        thread_id: Thread to write to.
        content: New text.
        mode: "append" adds to existing notes, "replace" overwrites entirely.

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
            return limit_error

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")

        size = len(new_content)
        logger.info(f"Notepad written for thread {thread_id}: {size} chars ({mode})")
        return f"[Saved]: Notepad updated ({size} chars). This content will persist through compaction."


def edit_notepad(
    thread_id: str,
    old_text: str,
    new_text: str = "",
    *,
    char_limit: int | None = None,
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
                return limit_error
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_body, encoding="utf-8")
            size = len(new_body)
            logger.info(f"Notepad rewritten via memory_edit for thread {thread_id}: {size} chars")
            return f"[Saved]: Notepad rewritten ({size} chars)."

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
            return limit_error

        path.write_text(updated, encoding="utf-8")
        size = len(updated)

        action = "replaced" if new_text else "removed"
        extra = f" ({count} occurrences found, first one {action})" if count > 1 else ""
        logger.info(f"Notepad edited for thread {thread_id}: {action} text, {size} chars")
        return f"[Saved]: Text {action}{extra}. Notepad is now {size} chars."
