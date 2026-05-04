"""Sticky note checklist tool. Reads and manages the desktop sticky note widget.

The sticky note widget watches ~/.nymeria/sticky_note/checklist.txt for changes.
This tool lets Nymeria read, add, remove, check, uncheck, and replace items
in that file, which the widget picks up automatically.

File format (one item per line):
    [ ] unchecked item
    [x] checked item
"""

import logging
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Data file location
CHECKLIST_FILE = Path.home() / ".nymeria" / "sticky_note" / "checklist.txt"


def _ensure_file() -> Path:
    """Ensure checklist file and parent dir exist."""
    CHECKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CHECKLIST_FILE.exists():
        CHECKLIST_FILE.write_text("", encoding="utf-8")
    return CHECKLIST_FILE


def _read_items() -> list[tuple[bool, str]]:
    """Parse checklist file into [(checked, text), ...]."""
    f = _ensure_file()
    items = []
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("[x]"):
            items.append((True, line[3:].strip()))
        elif line.startswith("[ ]"):
            items.append((False, line[3:].strip()))
        else:
            items.append((False, line))
    return items


def _write_items(items: list[tuple[bool, str]]) -> None:
    """Write items back to checklist file."""
    f = _ensure_file()
    lines = []
    for checked, text in items:
        prefix = "[x]" if checked else "[ ]"
        lines.append(f"{prefix} {text}")
    f.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _format_list(items: list[tuple[bool, str]]) -> str:
    """Format items for display."""
    if not items:
        return "(empty, no items)"
    lines = []
    for i, (checked, text) in enumerate(items, 1):
        mark = "x" if checked else " "
        lines.append(f"  {i}. [{mark}] {text}")
    return "\n".join(lines)


def _find_item(items: list[tuple[bool, str]], query: str) -> Optional[int]:
    """Find item index by number (1-based) or substring match. Returns 0-based index."""
    # Try as number first
    try:
        n = int(query)
        if 1 <= n <= len(items):
            return n - 1
    except ValueError:
        pass  # not a valid integer, skip
    # Substring match (case-insensitive)
    q = query.lower()
    for i, (_, text) in enumerate(items):
        if q in text.lower():
            return i
    return None


@tool
def sticky_note(
    action: str,
    text: str = "",
    target: str = "",
) -> str:
    """
    Manage the desktop sticky note checklist. The widget auto-refreshes.

    Actions:
      list:    Show all items (no other args needed)
      add:     Add a new unchecked item (text = item text)
      remove:  Remove an item (target = item # or substring)
      check:   Mark item as done (target = item # or substring)
      uncheck: Mark item as not done (target = item # or substring)
      replace: Replace entire list (text = newline-separated items, all unchecked)
      clear:   Remove all items

    Args:
        action: One of: list, add, remove, check, uncheck, replace, clear
        text: Item text for 'add', or newline-separated items for 'replace'
        target: Item number (1-based) or text substring to match for remove/check/uncheck
    """
    action = action.strip().lower()
    logger.info(f"sticky_note called: action={action}")

    try:
        items = _read_items()

        # ── list ────────────────────────────────────────────
        if action == "list":
            return f"[Sticky Note] ({len(items)} items):\n{_format_list(items)}"

        # ── add ─────────────────────────────────────────────
        elif action == "add":
            if not text.strip():
                return "[Error]: 'text' is required for add."
            # Support adding multiple items separated by newlines
            new_items = [t.strip() for t in text.strip().splitlines() if t.strip()]
            for item_text in new_items:
                # Strip any leading checkbox the caller might include
                clean = re.sub(r'^\[[ x]\]\s*', '', item_text, flags=re.IGNORECASE)
                if clean:
                    items.append((False, clean))
            _write_items(items)
            added = len(new_items)
            return f"[Success]: Added {added} item(s). Now {len(items)} total.\n{_format_list(items)}"

        # ── remove ──────────────────────────────────────────
        elif action == "remove":
            if not target:
                return "[Error]: 'target' is required for remove (item # or substring)."
            idx = _find_item(items, target)
            if idx is None:
                return f"[Error]: No item matching '{target}'.\n{_format_list(items)}"
            removed = items.pop(idx)
            _write_items(items)
            return f"[Success]: Removed '{removed[1]}'. {len(items)} items remain.\n{_format_list(items)}"

        # ── check ───────────────────────────────────────────
        elif action == "check":
            if not target:
                return "[Error]: 'target' is required for check (item # or substring)."
            idx = _find_item(items, target)
            if idx is None:
                return f"[Error]: No item matching '{target}'.\n{_format_list(items)}"
            items[idx] = (True, items[idx][1])
            _write_items(items)
            return f"[Success]: Checked '{items[idx][1]}'.\n{_format_list(items)}"

        # ── uncheck ─────────────────────────────────────────
        elif action == "uncheck":
            if not target:
                return "[Error]: 'target' is required for uncheck (item # or substring)."
            idx = _find_item(items, target)
            if idx is None:
                return f"[Error]: No item matching '{target}'.\n{_format_list(items)}"
            items[idx] = (False, items[idx][1])
            _write_items(items)
            return f"[Success]: Unchecked '{items[idx][1]}'.\n{_format_list(items)}"

        # ── replace ─────────────────────────────────────────
        elif action == "replace":
            if not text.strip():
                return "[Error]: 'text' is required for replace (newline-separated items)."
            new_items = []
            for line in text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                # Preserve checkbox state if provided
                if line.lower().startswith("[x]"):
                    new_items.append((True, line[3:].strip()))
                elif line.startswith("[ ]"):
                    new_items.append((False, line[3:].strip()))
                else:
                    # Strip leading numbers/bullets
                    clean = re.sub(r'^[\d]+[.)]\s*', '', line)
                    clean = re.sub(r'^[-*•]\s*', '', clean)
                    new_items.append((False, clean.strip()))
            items = new_items
            _write_items(items)
            return f"[Success]: Replaced list with {len(items)} item(s).\n{_format_list(items)}"

        # ── clear ───────────────────────────────────────────
        elif action == "clear":
            _write_items([])
            return "[Success]: Sticky note cleared."

        else:
            return f"[Error]: Unknown action '{action}'. Use: list, add, remove, check, uncheck, replace, clear."

    except Exception as e:
        logger.error(f"sticky_note failed: {e}")
        return f"[Error]: {str(e)}"


# Export list for __init__.py
STICKY_NOTE_TOOLS = [sticky_note]
