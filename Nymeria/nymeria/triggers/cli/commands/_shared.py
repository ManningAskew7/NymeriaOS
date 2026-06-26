"""Shared helpers for the CLI command modules.

This module is the canonical home for the cross-cutting command toolkit: the
transport shim (client-method resolution), confirmation helpers, the small
value-coercion / formatting helpers, and the scalar parser. It is imported by
the other command modules (and a couple of CLI callers) instead of reaching
into `system.py`, which now holds only the `/history`, `/settings`, `/redraw`,
and `/verbose` handlers plus their settings-specific formatters.

Finding F2 (relocating the `system.py` helper toolkit here) and finding F10
(folding `tools.py`'s duplicate scalar parser onto the shared `parse_scalar`)
shipped against this module. The earlier F3 partial seeded it with the
byte-identical `mapping_sequence` / `string_list` helpers. Helpers that diverge
across modules (`_csv`, and the per-module `_aligned_rows` copies) are
deliberately left in their own modules: their bodies and signatures differ, so
merging them would change output. `parse_key_values` and `format_settings_view`
stay in `system.py` as settings-command-local helpers (no other command module
imports them).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from . import CommandContext, CommandMessage, CommandResult


CONFIRM_FLAGS = {"--yes", "-y"}


def mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    """Return the mapping items of a non-str/bytes sequence, else an empty list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def string_list(value: Any) -> list[str]:
    """Return the truthy stringified items of a non-str/bytes sequence."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


class CommandClientMethodUnavailable(RuntimeError):
    """Raised when the current command transport cannot perform an operation."""

    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.method_name = method_name


def _method_owner(context: CommandContext, method_name: str) -> Any | None:
    """Return the object that owns a client method for API-first commands."""

    client = context.client
    if client is None:
        return None
    method = getattr(client, method_name, None)
    if callable(method):
        return client
    api = getattr(client, "api", None)
    method = getattr(api, method_name, None)
    if callable(method):
        return api
    return None


def has_client_method(context: CommandContext, method_name: str) -> bool:
    """Return whether the active transport exposes ``method_name``."""

    return _method_owner(context, method_name) is not None


async def call_client_method(
    context: CommandContext,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a method on ``context.client`` or its wrapped API client."""

    owner = _method_owner(context, method_name)
    if owner is None:
        raise CommandClientMethodUnavailable(method_name)
    result = getattr(owner, method_name)(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def call_client_user_scoped(
    context: CommandContext,
    method_name: str,
    *args: Any,
) -> Any:
    """Call a user-scoped client method, retrying ``user_id`` positionally.

    Passes ``user_id`` as a keyword argument; on ``TypeError`` (an older or
    alternate transport signature that takes ``user_id`` positionally) retries
    with ``user_id`` appended as the trailing positional argument. Only
    ``TypeError`` is handled here: ``CommandClientMethodUnavailable`` and every
    other exception propagate, so each caller keeps its own handling (the command
    label differs per call site, and some callers fall back to a legacy method
    instead of returning an unsupported-transport result).
    """

    try:
        return await call_client_method(context, method_name, *args, user_id=context.user_id)
    except TypeError:
        return await call_client_method(context, method_name, *args, context.user_id)


def unsupported_transport_result(
    command: str,
    *,
    method_name: str | None = None,
) -> CommandResult:
    """Return a consistent unsupported-transport error."""

    suffix = f" Missing client method: {method_name}." if method_name else ""
    return CommandResult.failed(
        f"{command} is not available with the current transport. "
        f"Run /login to connect to a Nymeria API if needed.{suffix}",
        error_code="unsupported_transport",
        payload={"method": method_name} if method_name else None,
    )


def strip_confirmation_flags(args: Sequence[str]) -> tuple[list[str], bool]:
    """Remove explicit confirmation flags from a command argument list."""

    remaining: list[str] = []
    confirmed = False
    for arg in args:
        if str(arg).casefold() in CONFIRM_FLAGS:
            confirmed = True
        else:
            remaining.append(arg)
    return remaining, confirmed


async def confirmation_granted(
    context: CommandContext,
    prompt: str,
    *,
    explicitly_confirmed: bool = False,
) -> bool:
    """Return whether a destructive command has user confirmation."""

    if explicitly_confirmed:
        return True
    return await context.confirm(prompt, default=False)


def confirmation_required_result(command: str) -> CommandResult:
    """Return the standard confirmation-required warning."""

    return CommandResult.completed(
        CommandMessage(
            f"Confirmation required for {command}. Re-run with --yes to proceed.",
            level="warning",
        )
    )


def normalize_thread_id(thread: Mapping[str, Any]) -> str:
    """Read a thread ID from either API or local transport payload shapes."""

    return str(thread.get("thread_id") or thread.get("id") or "")


def thread_title(thread: Mapping[str, Any]) -> str:
    """Return a readable thread title."""

    title = str(thread.get("title") or "").strip()
    return title or "New Chat"


def compact_id(value: Any, *, width: int = 8) -> str:
    """Return a short display ID."""

    text = str(value or "")
    return text[:width] if len(text) > width else text


def format_bool(value: Any) -> str:
    """Format booleans for compact command output."""

    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def parse_scalar(value: str) -> Any:
    """Parse simple CLI scalar values for settings patches."""

    raw = str(value).strip()
    lowered = raw.casefold()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        if "." not in raw and "e" not in lowered:
            return int(raw)
    except ValueError:
        pass  # Not an integer; try float parsing below.
    try:
        return float(raw)
    except ValueError:
        return raw


def mapping_get(data: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping-like or object-like payload."""

    if isinstance(data, Mapping):
        return data.get(key, default)
    return getattr(data, key, default)


def one_line(value: Any, *, limit: int = 200) -> str:
    """Collapse a value to one bounded display line."""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."
