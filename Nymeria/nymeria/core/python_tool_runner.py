"""Subprocess entrypoint for Python custom tools.

This module is intentionally stdlib-only. The API process sends a JSON payload
on stdin, the runner executes the configured function, and stdout contains one
JSON result object.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import traceback
from typing import Any


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, bool, int, float)) or value is None:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def main() -> int:
    # Launched by file path, so sys.path[0] is this runner's own directory
    # (nymeria/core), which would shadow stdlib modules a user tool imports
    # (e.g. ``import secrets`` -> nymeria/core/secrets.py). Drop it so stdlib
    # resolves normally; the nymeria package stays unimportable (not on path).
    if sys.path and sys.path[0] == os.path.dirname(os.path.abspath(__file__)):
        sys.path.pop(0)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        source_code = str(payload.get("source_code") or "")
        entrypoint = str(payload.get("entrypoint") or "run")
        params = payload.get("params") or {}
        tool_id = str(payload.get("tool_id") or "python_custom_tool")
        if not isinstance(params, dict):
            raise TypeError("params must be a JSON object")

        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "__name__": f"nymeria_user_tool_{tool_id}",
            "__file__": f"<nymeria_user_tool:{tool_id}>",
        }
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            compiled = compile(source_code, f"<nymeria_user_tool:{tool_id}>", "exec")
            exec(compiled, namespace)
            func = namespace.get(entrypoint)
            if not callable(func):
                raise TypeError(f"entrypoint {entrypoint!r} is not callable")
            result = func(**params)
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)

        _emit({
            "ok": True,
            "result": _stringify(result),
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
        })
        return 0
    except BaseException as exc:  # noqa: BLE001 - isolate all user-code failures.
        _emit({
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            },
        })
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
