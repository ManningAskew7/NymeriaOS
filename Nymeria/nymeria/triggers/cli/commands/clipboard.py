"""Clipboard command: /copy."""

from __future__ import annotations

import re
import shutil
import subprocess

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ..state.model import AssistantMessage, CLIUIState, ResponseStep


def _get_ui_state(context: CommandContext) -> CLIUIState | None:
    state = context.metadata.get("ui_state")
    return state if isinstance(state, CLIUIState) else None


def _assistant_messages(ui_state: CLIUIState) -> list[AssistantMessage]:
    return [
        msg for msg in ui_state.messages
        if isinstance(msg, AssistantMessage)
    ]


def _response_text(message: AssistantMessage) -> str:
    parts = [step.content for step in message.steps if isinstance(step, ResponseStep)]
    return "".join(parts)


_FENCED_CODE_RE = re.compile(
    r"```[^\n]*\n(.*?)```",
    re.DOTALL,
)


def _extract_code_blocks(text: str) -> str:
    blocks = _FENCED_CODE_RE.findall(text)
    return "\n\n".join(block.strip() for block in blocks if block.strip())


def _clipboard_command() -> list[str] | None:
    for cmd, args in [
        ("pbcopy", ["pbcopy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
        ("wl-copy", ["wl-copy"]),
    ]:
        if shutil.which(cmd):
            return args
    return None


def _copy_to_clipboard(text: str) -> str | None:
    cmd = _clipboard_command()
    if cmd is None:
        return "No clipboard tool found. Install xclip, xsel, or wl-copy."
    try:
        subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return f"Clipboard copy failed: {exc}"
    return None


async def _handle_copy(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    ui_state = _get_ui_state(context)
    if ui_state is None:
        return CommandResult.failed(
            "Transcript not available. Send a message first.",
        )

    assistant_msgs = _assistant_messages(ui_state)
    if not assistant_msgs:
        return CommandResult.failed("No assistant responses in this thread yet.")

    mode = "full"
    index = 1

    if args:
        token = args[0].casefold()
        if token == "code":
            mode = "code"
        elif token.isdigit():
            index = int(token)
        else:
            return CommandResult.failed(
                "Usage: /copy [N | code]",
                error_code="usage_error",
            )

    if index < 1 or index > len(assistant_msgs):
        return CommandResult.failed(
            f"Only {len(assistant_msgs)} assistant response(s) available.",
        )

    target = assistant_msgs[-index]
    text = _response_text(target)

    if not text.strip():
        return CommandResult.failed("Selected response is empty.")

    if mode == "code":
        text = _extract_code_blocks(text)
        if not text:
            return CommandResult.failed("No fenced code blocks found in the response.")

    error = _copy_to_clipboard(text)
    if error:
        return CommandResult.failed(error)

    label = "code blocks" if mode == "code" else "response"
    return CommandResult.completed(
        CommandMessage(
            f"Copied {label} to clipboard ({len(text)} chars)",
            level="success",
        ),
    )


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="copy",
        aliases=[],
        description="Copy assistant response to clipboard",
        usage="/copy [N | code]",
        handler=_handle_copy,
        handler_mode="context",
        category="Session",
    ))
