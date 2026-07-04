"""Script-driven status segments for the Rich REPL.

A ``script:<command>`` segment ref runs a user command periodically, off the
render loop, following the Claude Code statusline contract: the command
receives a JSON snapshot of CLI state on stdin and its first stdout line
becomes the segment text. The renderer only ever reads the cached
``(value, fetched_at)`` result via :meth:`ScriptSegmentRunner.lookup`, so a
slow or hung script can never block a frame.

Failure posture: a failing or timed-out run keeps the previous cached value
(stale text beats flicker); a command that has never succeeded renders
nothing. The subprocess is killed on timeout and on runner shutdown.
"""

from __future__ import annotations

import asyncio
import json
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

        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self.timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            with suppress(ProcessLookupError):
                process.kill()
            # Do NOT await process.wait() here: on CPython 3.12 a cancelled
            # communicate() leaves the transport's wait() hung forever even
            # after the child is reaped. The child watcher still reaps the
            # kill; poll returncode with a bounded budget instead.
            for _ in range(20):
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            # Close the pipe transport now, while the loop is alive, instead
            # of leaving it to GC (which warns if the loop closed first).
            transport = getattr(process, "_transport", None)
            if transport is not None:
                with suppress(Exception):
                    transport.close()
            raise
        if process.returncode != 0:
            return False

        text = self._first_line(stdout)
        if not text:
            return False
        previous = self._cache.get(command)
        self._cache[command] = _CachedResult(text=text, fetched_at=time.monotonic())
        return previous is None or previous.text != text

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
