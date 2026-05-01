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

logger = logging.getLogger(__name__)

# 50 KB max notepad size
MAX_NOTEPAD_SIZE = 50 * 1024

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
    path = _notepad_path(thread_id)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content if content else None


def delete_notepad(thread_id: str) -> bool:
    """Delete notepad file for a thread. Returns True if file existed."""
    path = _notepad_path(thread_id)
    if path.exists():
        path.unlink()
        return True
    return False


def write_notepad(thread_id: str, content: str, mode: str = "append") -> str:
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

    path = _notepad_path(thread_id)

    if mode == "append":
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        new_content = (existing.rstrip() + "\n\n" + content) if existing else content
    else:
        new_content = content

    if not new_content.strip():
        deleted = delete_notepad(thread_id)
        logger.info(f"Notepad cleared by empty write for thread {thread_id}")
        return "[Saved]: Notepad is empty." if deleted else "[Info]: Notepad was already empty."

    if len(new_content.encode("utf-8")) > MAX_NOTEPAD_SIZE:
        return f"[Error]: Notepad would exceed {MAX_NOTEPAD_SIZE // 1024}KB limit. Use mode='replace' to overwrite, or trim content."

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")

    size = len(new_content.encode("utf-8"))
    logger.info(f"Notepad written for thread {thread_id}: {size} bytes ({mode})")
    return f"[Saved]: Notepad updated ({size} bytes). This content will persist through compaction."


def edit_notepad(thread_id: str, old_text: str, new_text: str = "") -> str:
    """Find/replace within a thread's notepad. Empty new_text deletes the matched text.

    If the notepad becomes empty as a result, the underlying file is deleted.
    """
    path = _notepad_path(thread_id)

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

    if len(updated.encode("utf-8")) > MAX_NOTEPAD_SIZE:
        return f"[Error]: Edit would exceed {MAX_NOTEPAD_SIZE // 1024}KB limit."

    path.write_text(updated, encoding="utf-8")
    size = len(updated.encode("utf-8"))

    action = "replaced" if new_text else "removed"
    extra = f" ({count} occurrences found, first one {action})" if count > 1 else ""
    logger.info(f"Notepad edited for thread {thread_id}: {action} text, {size} bytes")
    return f"[Saved]: Text {action}{extra}. Notepad is now {size} bytes."
