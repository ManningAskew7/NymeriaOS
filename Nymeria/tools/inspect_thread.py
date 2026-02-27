#!/usr/bin/env python
"""Inspect the SQLite conversation history for a given thread.

Usage:
    python tools/inspect_thread.py <thread_id>
    python tools/inspect_thread.py <thread_id> --last 5     # only last N messages
    python tools/inspect_thread.py <thread_id> --full        # show full content (no truncation)
    python tools/inspect_thread.py --list                     # list all threads with message counts
"""

import sys
import io
import sqlite3
import argparse
from pathlib import Path

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nymeria.db"


def get_saver():
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    return SqliteSaver(conn), conn


def list_threads():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        SELECT thread_id, COUNT(*) as writes
        FROM writes
        WHERE channel = 'messages'
        GROUP BY thread_id
        ORDER BY thread_id
    """)
    rows = cur.fetchall()
    print(f"{'Thread ID':<45} {'Messages':>8}")
    print("-" * 55)
    for tid, count in rows:
        print(f"{tid:<45} {count:>8}")
    print(f"\n{len(rows)} threads total")
    conn.close()


def inspect_thread(thread_id: str, last_n: int | None = None, full: bool = False):
    saver, conn = get_saver()
    config = {"configurable": {"thread_id": thread_id}}

    checkpoint_tuple = saver.get_tuple(config)
    if not checkpoint_tuple:
        print(f"No checkpoint found for thread: {thread_id}")
        conn.close()
        return

    messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])
    total = len(messages)

    if last_n and last_n < total:
        messages = messages[-last_n:]
        print(f"Showing last {last_n} of {total} messages")
    else:
        print(f"Thread: {thread_id}")
        print(f"Messages: {total}")

    print("=" * 80)

    max_content = 500 if not full else 999999

    for i, msg in enumerate(messages):
        idx = (total - len(messages)) + i
        msg_type = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else str(msg)
        tool_calls = getattr(msg, "tool_calls", [])
        tool_name = getattr(msg, "name", None)
        msg_id = getattr(msg, "id", "")

        # Color-code by type
        label = msg_type
        if tool_name:
            label += f" (tool={tool_name})"

        content_display = content if len(content) <= max_content else content[:max_content] + "..."

        print(f"\n[{idx}] {label}")
        if content_display.strip():
            # Indent content for readability
            for line in content_display.split("\n"):
                print(f"    {line}")
        else:
            print(f"    (empty)")

        if tool_calls:
            for tc in tool_calls:
                args_str = str(tc.get("args", {}))
                if len(args_str) > max_content:
                    args_str = args_str[:max_content] + "..."
                print(f"    -> tool_call: {tc['name']}({args_str})")

    print("\n" + "=" * 80)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Inspect thread conversation history from SQLite")
    parser.add_argument("thread_id", nargs="?", help="Thread ID to inspect")
    parser.add_argument("--last", type=int, help="Show only last N messages")
    parser.add_argument("--full", action="store_true", help="Show full content without truncation")
    parser.add_argument("--list", action="store_true", help="List all threads with message counts")

    args = parser.parse_args()

    if args.list:
        list_threads()
    elif args.thread_id:
        inspect_thread(args.thread_id, last_n=args.last, full=args.full)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
