"""Session export and import commands: /export, /import."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    call_client_method,
    normalize_thread_id,
    thread_title,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)

SUPPORTED_FORMATS = ("json", "md", "jsonl")
DEFAULT_FORMAT = "json"


async def _handle_export(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    fmt = DEFAULT_FORMAT
    output_path: str | None = None
    sanitize = False
    remaining: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        lowered = arg.casefold()
        if lowered in {"--output", "-o"} and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
            continue
        if lowered.startswith("--output="):
            output_path = arg.split("=", 1)[1]
            i += 1
            continue
        if lowered == "--sanitize":
            sanitize = True
            i += 1
            continue
        remaining.append(arg)
        i += 1

    if remaining:
        candidate = remaining[0].casefold()
        if candidate in SUPPORTED_FORMATS:
            fmt = candidate
        else:
            return CommandResult.failed(
                f"Unknown format '{remaining[0]}'. Supported: {', '.join(SUPPORTED_FORMATS)}",
                error_code="usage_error",
            )

    try:
        history = await call_client_method(
            context,
            "get_history",
            context.thread_id,
            user_id=context.user_id,
            include_internal=True,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/export", method_name=exc.method_name)

    if not isinstance(history, Mapping):
        return CommandResult.failed("History response was not a mapping.")

    messages = _history_messages(history)

    thread_meta = await _thread_meta_or_none(context)
    title = thread_title(thread_meta or {})
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    thread_short = context.thread_id[:8]

    ext = {"json": "json", "md": "md", "jsonl": "jsonl"}[fmt]
    if output_path is None:
        output_path = f"nymeria-export-{thread_short}-{timestamp}.{ext}"

    dest = Path(output_path).expanduser()

    if sanitize:
        messages = _sanitize_messages(messages)

    if fmt == "json":
        content = _render_json(context.thread_id, title, messages, thread_meta)
    elif fmt == "md":
        content = _render_markdown(context.thread_id, title, messages)
    else:
        content = _render_jsonl(messages)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    except OSError as exc:
        return CommandResult.failed(f"Failed to write export: {exc}")

    return CommandResult.completed(
        CommandMessage(
            f"Exported {len(messages)} messages to {dest} ({fmt})",
            level="success",
        ),
        payload={
            "path": str(dest),
            "format": fmt,
            "message_count": len(messages),
        },
    )


async def _handle_import(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /import <file>",
            error_code="usage_error",
        )

    source = Path(args[0]).expanduser()
    if not source.is_file():
        return CommandResult.failed(f"File not found: {source}")

    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        return CommandResult.failed(f"Failed to read file: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CommandResult.failed(f"Invalid JSON: {exc}")

    if not isinstance(data, dict):
        return CommandResult.failed("Expected a JSON object with 'messages' array.")

    messages = data.get("messages")
    if not isinstance(messages, list):
        return CommandResult.failed("Export file does not contain a 'messages' array.")

    title = data.get("title") or data.get("thread_title")
    if not title:
        title = f"Import from {source.name}"

    try:
        created = await call_client_method(
            context,
            "create_thread",
            context.user_id,
            title=title,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/import", method_name=exc.method_name)

    if not isinstance(created, Mapping):
        return CommandResult.failed("Thread create response was not a mapping.")
    new_thread_id = normalize_thread_id(created)
    if not new_thread_id:
        return CommandResult.failed("Thread create response did not include a thread ID.")

    human_messages = [
        msg for msg in messages
        if _message_role(msg) in {"human", "user"}
    ]

    if not human_messages:
        await context.dispatch(
            {"type": "switch_thread", "thread_id": new_thread_id, "thread_label": title}
        )
        return CommandResult.completed(
            CommandMessage(
                f"Created thread {new_thread_id[:8]} from {source.name} (no replayable messages found)",
                level="warning",
            ),
            payload={"thread_id": new_thread_id, "source": str(source)},
        )

    await context.dispatch(
        {"type": "switch_thread", "thread_id": new_thread_id, "thread_label": title}
    )

    return CommandResult.completed(
        CommandMessage(
            f"Imported into new thread {new_thread_id[:8]} \"{title}\" "
            f"({len(messages)} messages, {len(human_messages)} user). "
            f"Note: message history was not replayed — send a message to start the conversation.",
            level="success",
        ),
        payload={
            "thread_id": new_thread_id,
            "title": title,
            "message_count": len(messages),
            "source": str(source),
        },
    )


# ── Format renderers ─────────────────────────────────────────────


def _render_json(
    thread_id: str,
    title: str,
    messages: list[Mapping[str, Any]],
    thread_meta: Mapping[str, Any] | None,
) -> str:
    export = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": thread_id,
        "title": title,
        "message_count": len(messages),
        "messages": list(messages),
    }
    if thread_meta:
        export["thread_metadata"] = dict(thread_meta)
    return json.dumps(export, indent=2, ensure_ascii=False, default=str)


def _render_markdown(
    thread_id: str,
    title: str,
    messages: list[Mapping[str, Any]],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Thread: `{thread_id}`  ",
        f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        f"Messages: {len(messages)}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = _message_role(msg)
        label = _role_label(role)
        content = _message_content_text(msg.get("content"))
        tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []

        lines.append(f"### {label}")
        lines.append("")
        if content:
            lines.append(content)
            lines.append("")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, Mapping):
                    name = tc.get("name") or tc.get("function", {}).get("name", "unknown")
                    lines.append(f"> **Tool call:** `{name}`")
            if tool_calls:
                lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _render_jsonl(messages: list[Mapping[str, Any]]) -> str:
    lines = []
    for msg in messages:
        lines.append(json.dumps(dict(msg), ensure_ascii=False, default=str))
    return "\n".join(lines) + "\n" if lines else ""


# ── Helpers ───────────────────────────────────────────────────────


def _history_messages(history: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = history.get("messages", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _message_role(msg: Mapping[str, Any]) -> str:
    return str(msg.get("role") or msg.get("type") or "unknown").casefold()


def _role_label(role: str) -> str:
    return {
        "human": "You",
        "user": "You",
        "assistant": "Nymeria",
        "ai": "Nymeria",
        "system": "System",
        "tool": "Tool",
    }.get(role, role.title())


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif block is not None:
                parts.append(str(block))
        return " ".join(parts)
    return "" if content is None else str(content)


def _sanitize_messages(
    messages: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    sanitized: list[Mapping[str, Any]] = []
    for msg in messages:
        entry = dict(msg)
        role = _message_role(entry)
        if role == "tool":
            content = entry.get("content")
            if isinstance(content, str) and len(content) > 200:
                entry["content"] = "[sanitized tool output]"
            elif isinstance(content, list):
                entry["content"] = "[sanitized tool output]"
        sanitized.append(entry)
    return sanitized


async def _thread_meta_or_none(
    context: CommandContext,
) -> Mapping[str, Any] | None:
    try:
        threads = await call_client_method(context, "list_threads", context.user_id)
    except CommandClientMethodUnavailable:
        return None
    if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
        return None
    for thread in threads:
        if isinstance(thread, Mapping) and normalize_thread_id(thread) == context.thread_id:
            return thread
    return None


# ── Registration ──────────────────────────────────────────────────


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="export",
        aliases=[],
        description="Export current thread to file",
        usage="/export [json|md|jsonl] [--output PATH] [--sanitize]",
        handler=_handle_export,
        handler_mode="context",
        category="Session",
    ))
    registry.register(Command(
        name="import",
        aliases=[],
        description="Import a thread from an exported JSON file",
        usage="/import <file>",
        handler=_handle_import,
        handler_mode="context",
        category="Session",
    ))
