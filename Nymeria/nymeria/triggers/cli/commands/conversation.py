"""Conversation commands: /retry, /undo."""

from __future__ import annotations

import httpx

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..state.model import AssistantMessage, CLIUIState, UserMessage
from ._shared import (
    call_client_method,
    confirmation_granted,
    strip_confirmation_flags,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)


def _rewind_http_failure(exc: httpx.HTTPStatusError) -> CommandResult:
    """Surface the backend rewind refusal (409 busy / 404 target gone) cleanly."""
    detail = None
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except Exception:
        detail = None
    if not isinstance(detail, str) or not detail.strip():
        detail = f"Rewind failed (HTTP {exc.response.status_code})."
    return CommandResult.failed(detail)


def _get_ui_state(context: CommandContext) -> CLIUIState | None:
    state = context.metadata.get("ui_state")
    return state if isinstance(state, CLIUIState) else None


def _last_user_message(ui_state: CLIUIState) -> UserMessage | None:
    """Find the last user message in the transcript."""
    for msg in reversed(ui_state.messages):
        if isinstance(msg, UserMessage):
            return msg
    return None


def _has_exchange(ui_state: CLIUIState) -> bool:
    """Check whether the transcript contains at least one user+assistant pair."""
    found_assistant = False
    for msg in reversed(ui_state.messages):
        if isinstance(msg, AssistantMessage):
            found_assistant = True
        elif isinstance(msg, UserMessage) and found_assistant:
            return True
    return False


async def _handle_retry(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    ui_state = _get_ui_state(context)
    if ui_state is None:
        return CommandResult.failed("Transcript not available. Send a message first.")

    if not _has_exchange(ui_state):
        return CommandResult.failed("No user message found to retry.")

    last_user = _last_user_message(ui_state)
    if last_user is None:
        return CommandResult.failed("No user message found to retry.")

    replacement = " ".join(args).strip() if args else ""
    retry_prompt = replacement or last_user.content
    if not retry_prompt.strip():
        return CommandResult.failed("Cannot retry with an empty message.")

    try:
        await call_client_method(
            context,
            "rewind_thread",
            context.thread_id,
            steps=1,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/retry", method_name=exc.method_name)
    except httpx.HTTPStatusError as exc:
        return _rewind_http_failure(exc)

    await context.dispatch({"type": "undo_last_exchange"})

    label = "Retrying with new prompt..." if replacement else "Retrying..."
    return CommandResult.completed(
        CommandMessage(label, level="info"),
        payload={
            "retry_message": retry_prompt,
            "retry_attachments": list(last_user.attachments) if last_user.attachments else [],
        },
    )


async def _handle_undo(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    args, explicit_confirmation = strip_confirmation_flags(args)
    if args:
        return CommandResult.failed("Usage: /undo [--yes]", error_code="usage_error")

    ui_state = _get_ui_state(context)
    if ui_state is None:
        return CommandResult.failed("Transcript not available. Send a message first.")

    if not _has_exchange(ui_state):
        return CommandResult.failed("No exchange to undo.")

    confirmed = await confirmation_granted(
        context,
        "Remove the last user+assistant exchange?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return CommandResult.completed(
            CommandMessage(
                "Confirmation required for /undo. Re-run with --yes to proceed.",
                level="warning",
            )
        )

    try:
        await call_client_method(
            context,
            "rewind_thread",
            context.thread_id,
            steps=1,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/undo", method_name=exc.method_name)
    except httpx.HTTPStatusError as exc:
        return _rewind_http_failure(exc)

    await context.dispatch({"type": "undo_last_exchange"})

    return CommandResult.completed(
        CommandMessage("Undid last exchange.", level="success"),
    )


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="retry",
        aliases=[],
        description="Re-send last message for a new response",
        usage="/retry [new prompt]",
        handler=_handle_retry,
        category="Conversation",
    ))
    registry.register(Command(
        name="undo",
        aliases=[],
        description="Remove last user+assistant exchange",
        usage="/undo [--yes]",
        handler=_handle_undo,
        category="Conversation",
    ))
