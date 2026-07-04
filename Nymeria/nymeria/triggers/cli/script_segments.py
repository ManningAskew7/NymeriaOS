"""Script-driven status segments for the Rich REPL.

A ``script:<command>`` segment ref runs a user command periodically, off the
render loop, following the Claude Code statusline contract: the command
receives a JSON snapshot of CLI state on stdin and its first stdout line
becomes the segment text. The renderer only ever reads the cached
``(value, fetched_at)`` result via :meth:`ScriptSegmentRunner.lookup`, so a
slow or hung script can never block a frame.

Script segments are USER-INSTALLED ONLY (the local ``/statusbar`` command or
a hand edit of ``cli.json``); the agent-pushed ``cli_config`` path rejects
``script:`` refs on both the backend tool and the client apply branch, so a
prompt-injected agent cannot plant a periodically-executing command here.

Failure posture: a failing or timed-out run keeps the previous cached value
(stale text beats flicker); a command that has never succeeded renders
nothing. stdout is read with a hard byte bound (never buffered unbounded),
each script runs in its own process group (POSIX) so a backgrounded
grandchild dies with it, and the group is killed on timeout and shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

SCRIPT_REFRESH_INTERVAL_SECONDS = 5.0
SCRIPT_TIMEOUT_SECONDS = 4.0
_MAX_OUTPUT_BYTES = 16_384
_MAX_SEGMENT_CHARS = 200


@dataclass(slots=True)
class _CachedResult:
    text: str
    fetched_at: float


class ScriptSegmentRunner:
    """Run ``script:`` segment commands on a background watcher task.

    The watcher starts lazily on the first :meth:`lookup` (render happens
    inside the running event loop) or when :meth:`set_commands` is called
    from a coroutine, and stops itself while no commands are configured.
    """

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], dict[str, Any]],
        on_update: Callable[[], None] | None = None,
        interval_seconds: float = SCRIPT_REFRESH_INTERVAL_SECONDS,
        timeout_seconds: float = SCRIPT_TIMEOUT_SECONDS,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._on_update = on_update
        self.interval_seconds = max(0.5, interval_seconds)
        self.timeout_seconds = max(0.1, timeout_seconds)
        self._commands: tuple[str, ...] = ()
        self._cache: dict[str, _CachedResult] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def commands(self) -> tuple[str, ...]:
        return self._commands

    def set_commands(self, commands: tuple[str, ...]) -> None:
        """Replace the command set; prunes cache entries for removed ones."""

        self._commands = tuple(dict.fromkeys(c for c in commands if c))
        for stale in set(self._cache) - set(self._commands):
            self._cache.pop(stale, None)
        if not self._commands:
            self.stop()
            return
        self._wake.set()
        self._ensure_watcher()

    def lookup(self, command: str) -> str | None:
        """Return the cached segment text for ``command`` without blocking."""

        self._ensure_watcher()
        cached = self._cache.get(command)
        return cached.text if cached is not None else None

    def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    async def stop_async(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _ensure_watcher(self) -> None:
        if not self._commands:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet (e.g. layout applied at startup). The first
            # in-loop lookup or set_commands call starts the watcher.
            return
        self._task = asyncio.create_task(
            self._run_watcher(),
            name="NymeriaCLIStatusScriptWatcher",
        )

    async def _run_watcher(self) -> None:
        while True:
            self._wake.clear()
            commands = self._commands
            if not commands:
                return
            changed = False
            for command in commands:
                if command not in self._commands:
                    continue  # /statusbar changed the layout mid-sweep
                try:
                    changed |= await self._refresh_command(command)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a script must never crash the REPL.
                    continue
            if changed and self._on_update is not None:
                with suppress(Exception):
                    self._on_update()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.interval_seconds,
                )

    async def _refresh_command(self, command: str) -> bool:
        """Run one script; True when its cached text changed."""

        try:
            snapshot = self._snapshot_provider()
        except Exception:  # noqa: BLE001 - snapshot failures degrade to {}.
            snapshot = {}
        try:
            payload = json.dumps(snapshot, default=str).encode("utf-8")
        except (TypeError, ValueError):
            payload = b"{}"

        kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True  # own group for group-kill
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        try:
            stdout, returncode = await asyncio.wait_for(
                self._communicate_bounded(process, payload),
                timeout=self.timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._kill_process_group(process)
            await self._reap(process)
            raise
        if returncode != 0:
            return False

        text = self._first_line(stdout)
        if not text:
            return False
        previous = self._cache.get(command)
        self._cache[command] = _CachedResult(text=text, fetched_at=time.monotonic())
        return previous is None or previous.text != text

    @staticmethod
    async def _communicate_bounded(
        process: Any,
        payload: bytes,
    ) -> tuple[bytes, int | None]:
        """Feed stdin and read AT MOST the output bound, then await exit.

        Deliberately not ``process.communicate()``: that buffers the entire
        stdout in memory, so a chatty script would spike RSS every sweep.
        A script that emits more than the bound blocks on the full pipe,
        fails the outer timeout, and is group-killed.
        """

        stdin = process.stdin
        if stdin is not None:
            try:
                stdin.write(payload)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # script exited early or never read stdin; fine
            with suppress(Exception):
                stdin.close()
        # Read until the first newline, EOF, or the byte bound; only the
        # first line is ever shown, so anything past it stays in the OS
        # pipe buffer (a script spewing far beyond it blocks, fails the
        # outer timeout, and is group-killed).
        buffer = bytearray()
        if process.stdout is not None:
            while len(buffer) < _MAX_OUTPUT_BYTES and b"\n" not in buffer:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
        returncode = await process.wait()
        return bytes(buffer[:_MAX_OUTPUT_BYTES]), returncode

    @staticmethod
    def _kill_process_group(process: Any) -> None:
        """SIGKILL the script's whole group (falls back to the child)."""

        try:
            if sys.platform != "win32" and hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone

    @staticmethod
    async def _reap(process: Any) -> None:
        """Bounded post-kill cleanup that never awaits ``process.wait()``.

        On CPython 3.12 a cancelled in-flight read/communicate can leave the
        transport's ``wait()`` hung forever even after the child is reaped;
        poll ``returncode`` instead, then close the pipe transport while the
        loop is alive (GC-time close warns if the loop closed first).
        """

        for _ in range(20):
            if process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()

    @staticmethod
    def _first_line(stdout: bytes | None) -> str:
        raw = (stdout or b"")[:_MAX_OUTPUT_BYTES]
        decoded = raw.decode("utf-8", errors="replace")
        first_line = decoded.strip().splitlines()[0].strip() if decoded.strip() else ""
        return first_line[:_MAX_SEGMENT_CHARS]


__all__ = [
    "SCRIPT_REFRESH_INTERVAL_SECONDS",
    "SCRIPT_TIMEOUT_SECONDS",
    "ScriptSegmentRunner",
]
