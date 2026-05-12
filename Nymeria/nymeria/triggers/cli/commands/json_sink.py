"""JSON output sink for slash-command results."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .base import CommandMessage, CommandResult


class JsonCommandOutputSink:
    """Write machine-readable command output to stdout."""

    def __init__(self, *, stdout: Any = None, stderr: Any = None) -> None:
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def emit(self, message: CommandMessage) -> None:
        content = str(message.content or "")
        if content:
            print(content, file=self.stderr)

    def emit_result(self, result: CommandResult) -> None:
        payload = result.json_payload if result.has_json_payload else _result_payload(result)
        print(
            json.dumps(_json_safe(payload), ensure_ascii=False, default=str),
            file=self.stdout,
        )


def _result_payload(result: CommandResult) -> dict[str, Any]:
    output: dict[str, Any] = {
        "status": result.status,
        "ok": result.ok,
        "handled": result.handled,
        "command": list(result.command_path),
    }
    if result.error_code:
        output["error_code"] = result.error_code
    if result.messages:
        output["messages"] = [_message_payload(message) for message in result.messages]
    if result.payload:
        output["payload"] = dict(result.payload)
    return output


def _message_payload(message: CommandMessage) -> dict[str, Any]:
    output = {
        "level": message.level,
        "content": message.content,
    }
    if message.title:
        output["title"] = message.title
    if message.details:
        output["details"] = dict(message.details)
    return output


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=repr)
    return value


__all__ = ["JsonCommandOutputSink"]
