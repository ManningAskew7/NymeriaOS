"""Per-thread notepad tools: persistent notes that survive compaction.

Unlike memories (which are global, cross-thread facts injected into the system
prompt), notepad content is thread-specific context: project state, file paths,
decisions, etc. After compaction, notepad content is automatically re-injected
so critical thread context is never lost.

Storage: data/thread_notes/{thread_id}.md
"""

import logging
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id

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
    # Sanitize thread_id for filesystem safety
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


@tool
def notepad_write(
    content: str,
    mode: str = "append",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Write to the thread's notepad. Content survives context compaction.

    Use this to save thread-specific context: project state, file paths,
    decisions, key findings, or anything you'll need after compaction.

    Args:
        content: Text to write to the notepad
        mode: "append" (default) adds to existing notes, "replace" overwrites entirely
    """
    thread_id = get_thread_id(config)
    path = _notepad_path(thread_id)

    if mode not in ("append", "replace"):
        return "[Error]: mode must be 'append' or 'replace'."

    if mode == "append":
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing:
            new_content = existing.rstrip() + "\n\n" + content
        else:
            new_content = content
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


@tool
def notepad_read(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Read the thread's notepad content.

    Returns:
        Current notepad content, or "[empty]" if nothing saved
    """
    thread_id = get_thread_id(config)
    content = read_notepad(thread_id)
    if content:
        return content
    return "[empty]"


@tool
def notepad_edit(
    old_text: str,
    new_text: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Edit the notepad by replacing specific text. Use this to update, correct,
    or remove sections without rewriting the entire notepad.

    To remove text, set new_text to empty string.
    To update text, provide both old_text and new_text.

    Args:
        old_text: The exact text to find and replace (must match exactly)
        new_text: The replacement text (empty string to delete the matched text)
    """
    thread_id = get_thread_id(config)
    path = _notepad_path(thread_id)

    if not path.exists():
        return "[Error]: Notepad is empty; nothing to edit."

    content = path.read_text(encoding="utf-8")

    if old_text not in content:
        return f"[Error]: Could not find the specified text in notepad. Make sure it matches exactly."

    count = content.count(old_text)
    updated = content.replace(old_text, new_text, 1)

    # Clean up double blank lines from deletions
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

    if new_text:
        action = "replaced"
    else:
        action = "removed"
    extra = f" ({count} occurrences found, first one {action})" if count > 1 else ""
    logger.info(f"Notepad edited for thread {thread_id}: {action} text, {size} bytes")
    return f"[Saved]: Text {action}{extra}. Notepad is now {size} bytes."


@tool
def notepad_clear(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Clear the thread's notepad entirely.

    Returns:
        Confirmation message
    """
    thread_id = get_thread_id(config)
    if delete_notepad(thread_id):
        logger.info(f"Notepad cleared for thread {thread_id}")
        return "[Cleared]: Notepad deleted."
    return "[Info]: Notepad was already empty."


# Export
NOTEPAD_TOOLS = [notepad_write, notepad_read, notepad_edit, notepad_clear]
